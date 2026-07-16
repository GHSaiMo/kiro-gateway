# -*- coding: utf-8 -*-

"""
Cockpit secure account document compatibility.

Cockpit Tools 1.3.5 encrypts per-account detail files with AES-256-GCM.
This module mirrors its ``secure_account_storage.rs`` format while preserving
support for legacy plaintext JSON account files.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

from Crypto.Cipher import AES


COCKPIT_ACCOUNTS_DIR = "kiro_accounts"
COCKPIT_SECURE_KEY_FILE = "secure-account-storage.key"
COCKPIT_SECURE_VERSION = 1
COCKPIT_SECURE_KIND = "kiro"
COCKPIT_SECURE_ALGORITHM = "AES-256-GCM"
COCKPIT_SECURE_KEY_ID = "local-secure-account-storage-v1"
AES_KEY_BYTES = 32
GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16

_ENVELOPE_MARKER_KEYS = frozenset({"algorithm", "ciphertext", "key_id", "nonce"})


class CockpitStorageError(ValueError):
    """Raised when a Cockpit account document cannot be validated or decrypted."""


@dataclass(frozen=True)
class CockpitAccountDocument:
    """
    Parsed Cockpit account document.

    Attributes:
        payload: Decrypted account fields.
        encrypted: Whether the source file used Cockpit's secure envelope.
    """

    payload: Dict[str, Any]
    encrypted: bool


def _is_secure_envelope(value: Mapping[str, Any]) -> bool:
    """
    Checks whether a JSON object is intended to be a secure account envelope.

    Args:
        value: Parsed JSON object.

    Returns:
        True when the object contains any secure-envelope marker.
    """
    return bool(_ENVELOPE_MARKER_KEYS.intersection(value))


def _require_envelope_string(envelope: Mapping[str, Any], key: str) -> str:
    """
    Reads one required non-empty string from an encrypted envelope.

    Args:
        envelope: Parsed encrypted envelope.
        key: Required field name.

    Returns:
        Trimmed field value.

    Raises:
        CockpitStorageError: If the field is missing or empty.
    """
    value = envelope.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CockpitStorageError(f"Cockpit 加密账号文件缺少有效的 {key} 字段")
    return value.strip()


def _decode_base64(value: str, field_name: str) -> bytes:
    """
    Decodes one strict standard Base64 field.

    Args:
        value: Base64 text.
        field_name: Field name used in actionable errors.

    Returns:
        Decoded bytes.

    Raises:
        CockpitStorageError: If the value is not valid standard Base64.
    """
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CockpitStorageError(
            f"Cockpit 加密账号文件的 {field_name} 不是有效 Base64"
        ) from exc


def _secure_key_path(account_path: Path) -> Path:
    """
    Resolves Cockpit's secure-account master key for an account file.

    Args:
        account_path: Path under ``<cockpit-root>/kiro_accounts``.

    Returns:
        Path to ``secure-account-storage.key``.

    Raises:
        CockpitStorageError: If the account is outside the expected directory.
    """
    if account_path.parent.name != COCKPIT_ACCOUNTS_DIR:
        raise CockpitStorageError(
            "Cockpit 加密账号文件必须位于 kiro_accounts 目录中，无法定位解密密钥"
        )
    return account_path.parent.parent / COCKPIT_SECURE_KEY_FILE


def _read_secure_key(account_path: Path) -> bytes:
    """
    Loads and validates Cockpit's AES-256 master key.

    Args:
        account_path: Encrypted account file path.

    Returns:
        Raw 32-byte AES key.

    Raises:
        OSError: If the key file cannot be read.
        CockpitStorageError: If the key is malformed or has the wrong length.
    """
    key_path = _secure_key_path(account_path)
    if not key_path.exists():
        raise CockpitStorageError(
            f"Cockpit 解密密钥不存在: {key_path}。请确认账号池根目录配置正确"
        )
    encoded = key_path.read_text(encoding="utf-8").strip()
    key = _decode_base64(encoded, COCKPIT_SECURE_KEY_FILE)
    if len(key) != AES_KEY_BYTES:
        raise CockpitStorageError(
            f"Cockpit 解密密钥长度无效: 需要 {AES_KEY_BYTES} 字节，实际 {len(key)} 字节"
        )
    return key


def _validate_envelope(envelope: Mapping[str, Any]) -> tuple[bytes, bytes]:
    """
    Validates an encrypted envelope and decodes its binary fields.

    Args:
        envelope: Parsed encrypted envelope.

    Returns:
        Tuple of ``(nonce, ciphertext_with_tag)``.

    Raises:
        CockpitStorageError: If any protocol field is unsupported or malformed.
    """
    version = envelope.get("version")
    if isinstance(version, bool) or version != COCKPIT_SECURE_VERSION:
        raise CockpitStorageError(
            f"不支持的 Cockpit 加密账号版本: {version!r}，当前仅支持 {COCKPIT_SECURE_VERSION}"
        )

    kind = _require_envelope_string(envelope, "kind")
    if kind != COCKPIT_SECURE_KIND:
        raise CockpitStorageError(f"Cockpit 账号类型不是 Kiro: {kind!r}")

    algorithm = _require_envelope_string(envelope, "algorithm")
    if algorithm != COCKPIT_SECURE_ALGORITHM:
        raise CockpitStorageError(f"不支持的 Cockpit 账号加密算法: {algorithm!r}")

    key_id = _require_envelope_string(envelope, "key_id")
    if key_id != COCKPIT_SECURE_KEY_ID:
        raise CockpitStorageError(f"不支持的 Cockpit 账号加密密钥类型: {key_id!r}")

    nonce = _decode_base64(_require_envelope_string(envelope, "nonce"), "nonce")
    if len(nonce) != GCM_NONCE_BYTES:
        raise CockpitStorageError(
            f"Cockpit 加密 nonce 长度无效: 需要 {GCM_NONCE_BYTES} 字节，实际 {len(nonce)} 字节"
        )

    ciphertext = _decode_base64(
        _require_envelope_string(envelope, "ciphertext"), "ciphertext"
    )
    if len(ciphertext) < GCM_TAG_BYTES:
        raise CockpitStorageError("Cockpit 加密密文过短，缺少 GCM 认证标签")
    return nonce, ciphertext


def _decrypt_payload(account_path: Path, envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Decrypts one Cockpit AES-256-GCM account envelope.

    Args:
        account_path: Encrypted account file path.
        envelope: Parsed encrypted envelope.

    Returns:
        Decrypted account JSON object.

    Raises:
        OSError: If the key file cannot be read.
        CockpitStorageError: If validation, authentication, or JSON parsing fails.
    """
    nonce, ciphertext_with_tag = _validate_envelope(envelope)
    key = _read_secure_key(account_path)
    ciphertext = ciphertext_with_tag[:-GCM_TAG_BYTES]
    tag = ciphertext_with_tag[-GCM_TAG_BYTES:]

    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=GCM_TAG_BYTES)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as exc:
        raise CockpitStorageError(
            "Cockpit 账号解密失败：密钥不匹配或文件已损坏"
        ) from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CockpitStorageError("Cockpit 账号解密结果不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise CockpitStorageError("Cockpit 账号解密结果不是 JSON 对象")
    return payload


def read_account_document(account_path: str | Path) -> CockpitAccountDocument:
    """
    Reads a plaintext or encrypted Cockpit account document.

    Args:
        account_path: Account JSON file path.

    Returns:
        Parsed document and its original storage mode.

    Raises:
        OSError: If the account or key file cannot be read.
        json.JSONDecodeError: If a plaintext file is malformed JSON.
        CockpitStorageError: If the document shape or encryption is invalid.
    """
    path = Path(account_path).expanduser()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise CockpitStorageError("Cockpit 账号文件不是 JSON 对象")
    if not _is_secure_envelope(parsed):
        return CockpitAccountDocument(payload=parsed, encrypted=False)
    return CockpitAccountDocument(payload=_decrypt_payload(path, parsed), encrypted=True)


def _serialize_encrypted_payload(account_path: Path, payload: Mapping[str, Any]) -> str:
    """
    Serializes account fields using Cockpit's AES-256-GCM envelope.

    Args:
        account_path: Destination account file path.
        payload: Plain account fields.

    Returns:
        Pretty-printed encrypted envelope JSON.

    Raises:
        OSError: If the key file cannot be read.
        CockpitStorageError: If the key is invalid.
        TypeError: If the payload is not JSON serializable.
    """
    plaintext = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    key = _read_secure_key(account_path)
    nonce = secrets.token_bytes(GCM_NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=GCM_TAG_BYTES)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    envelope = {
        "version": COCKPIT_SECURE_VERSION,
        "kind": COCKPIT_SECURE_KIND,
        "algorithm": COCKPIT_SECURE_ALGORITHM,
        "key_id": COCKPIT_SECURE_KEY_ID,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext + tag).decode("ascii"),
        "encrypted_at": int(time.time()),
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False)


def write_account_document(
    account_path: str | Path,
    payload: Mapping[str, Any],
    *,
    encrypted: bool,
) -> None:
    """
    Atomically writes a plaintext or encrypted Cockpit account document.

    Args:
        account_path: Destination account JSON file path.
        payload: Account fields to persist.
        encrypted: Whether to retain Cockpit's secure envelope format.

    Returns:
        None.

    Raises:
        OSError: If the file or key cannot be read or written.
        CockpitStorageError: If encrypted storage cannot be prepared.
        TypeError: If the payload is not JSON serializable.
    """
    path = Path(account_path).expanduser()
    if encrypted:
        content = _serialize_encrypted_payload(path, payload)
    else:
        content = json.dumps(dict(payload), indent=2, ensure_ascii=False)

    existing_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
