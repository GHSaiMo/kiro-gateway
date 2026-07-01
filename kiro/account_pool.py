# -*- coding: utf-8 -*-

"""
Cockpit Kiro account pool loader and ranking helpers.

This module reads the local Cockpit account snapshots and computes prompt
remaining quota using only ``credits_total - credits_used``. Bonus quota is
intentionally ignored because it may expire and should not drive failover.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from loguru import logger

from kiro.config import TOKEN_REFRESH_THRESHOLD

MONTHLY_POOL_FILE = "provider_current_accounts.json"
COCKPIT_ACCOUNTS_DIR = "kiro_accounts"
COCKPIT_ACCOUNTS_INDEX_FILE = "kiro_accounts.json"


def _to_float(value: Any) -> Optional[float]:
    """
    Converts a raw JSON value to float when possible.

    Args:
        value: Raw JSON value.

    Returns:
        Float value or ``None`` when conversion is not possible.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        if not math.isfinite(parsed):
            return None
        return parsed
    return None


def _to_int(value: Any) -> Optional[int]:
    """
    Converts a raw JSON value to integer when possible.

    Args:
        value: Raw JSON value.

    Returns:
        Integer value or ``None`` when conversion is not possible.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if "." in text:
                return int(float(text))
            return int(text)
        except ValueError:
            try:
                return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return None
    return None


def _first_non_empty_string(*values: Any) -> Optional[str]:
    """
    Returns the first non-empty string from the provided values.

    Args:
        values: Candidate values.

    Returns:
        First non-empty trimmed string or ``None``.
    """
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _is_banned_status(value: Optional[str]) -> bool:
    """
    Checks whether a status indicates the account is banned.

    Args:
        value: Raw status string.

    Returns:
        True when the status looks banned or forbidden.
    """
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized in {"banned", "ban", "forbidden"}


def _is_banned_reason(value: Optional[str]) -> bool:
    """
    Checks whether a status reason indicates the account is banned.

    Args:
        value: Raw status reason string.

    Returns:
        True when the reason contains banned/forbidden/suspended language.
    """
    if not value:
        return False
    normalized = value.strip().lower()
    return any(
        token in normalized
        for token in ("banned", "forbidden", "suspended", "disabled", "封禁", "禁用")
    )


def _pick_mapping_value(data: Mapping[str, Any], *keys: str) -> Any:
    """
    Returns the first present value for the given key list.

    Args:
        data: Source mapping.
        keys: Candidate key names in priority order.

    Returns:
        First matching value or ``None``.
    """
    for key in keys:
        if key in data:
            return data[key]
    return None


@dataclass(frozen=True)
class CockpitKiroAccount:
    """
    Normalized snapshot of one Cockpit-managed Kiro account.

    Attributes:
        account_id: Stable account identifier.
        email: Login email address.
        source_path: Path to the backing JSON snapshot file.
        access_token: Current access token.
        refresh_token: Refresh token used to obtain a new access token.
        token_type: Authorization token type.
        expires_at: Token expiration epoch seconds.
        credits_total: Prompt quota total.
        credits_used: Prompt quota used.
        bonus_total: Bonus quota total, preserved but ignored for ranking.
        bonus_used: Bonus quota used, preserved but ignored for ranking.
        created_at: Creation timestamp.
        last_used: Last-used timestamp.
        status: Optional account status.
        status_reason: Optional status reason.
        profile_arn: Optional Kiro profile ARN.
    """

    account_id: str
    email: str
    source_path: Path
    access_token: str
    refresh_token: Optional[str]
    token_type: Optional[str]
    expires_at: Optional[int]
    credits_total: Optional[float]
    credits_used: Optional[float]
    bonus_total: Optional[float]
    bonus_used: Optional[float]
    created_at: int
    last_used: int
    status: Optional[str] = None
    status_reason: Optional[str] = None
    profile_arn: Optional[str] = None

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        source_path: Path,
    ) -> "CockpitKiroAccount":
        """
        Builds a normalized account snapshot from a Cockpit JSON payload.

        Args:
            data: Raw account JSON mapping.
            source_path: File path used to load the snapshot.

        Returns:
            Normalized account snapshot.

        Raises:
            ValueError: If the payload is missing the essential fields.
        """
        auth_raw = data.get("kiro_auth_token_raw")
        auth_map = auth_raw if isinstance(auth_raw, Mapping) else {}
        profile_raw = data.get("kiro_profile_raw")
        profile_map = profile_raw if isinstance(profile_raw, Mapping) else {}

        account_id = _first_non_empty_string(data.get("id"), source_path.stem)
        email = _first_non_empty_string(data.get("email")) or account_id
        access_token = _first_non_empty_string(
            _pick_mapping_value(data, "access_token", "accessToken"),
            _pick_mapping_value(auth_map, "accessToken", "access_token"),
        )
        if not access_token:
            raise ValueError(f"账号 {account_id} 缺少 access_token")

        refresh_token = _first_non_empty_string(
            _pick_mapping_value(data, "refresh_token", "refreshToken"),
            _pick_mapping_value(auth_map, "refreshToken", "refresh_token"),
        )
        token_type = _first_non_empty_string(
            _pick_mapping_value(data, "token_type", "tokenType"),
            _pick_mapping_value(auth_map, "tokenType", "token_type"),
        )
        expires_at = _to_int(
            _pick_mapping_value(data, "expires_at", "expiresAt")
            or _pick_mapping_value(auth_map, "expiresAt", "expires_at")
        )
        profile_arn = _first_non_empty_string(
            _pick_mapping_value(data, "profile_arn", "profileArn"),
            _pick_mapping_value(auth_map, "profileArn", "profile_arn"),
            _pick_mapping_value(profile_map, "arn"),
        )

        credits_total = _to_float(_pick_mapping_value(data, "credits_total", "creditsTotal"))
        credits_used = _to_float(_pick_mapping_value(data, "credits_used", "creditsUsed"))
        bonus_total = _to_float(_pick_mapping_value(data, "bonus_total", "bonusTotal"))
        bonus_used = _to_float(_pick_mapping_value(data, "bonus_used", "bonusUsed"))
        created_at = _to_int(_pick_mapping_value(data, "created_at", "createdAt")) or 0
        last_used = _to_int(_pick_mapping_value(data, "last_used", "lastUsed")) or 0
        status = _first_non_empty_string(_pick_mapping_value(data, "status"))
        status_reason = _first_non_empty_string(
            _pick_mapping_value(data, "status_reason", "statusReason")
        )

        return cls(
            account_id=account_id,
            email=email,
            source_path=source_path,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            credits_total=credits_total,
            credits_used=credits_used,
            bonus_total=bonus_total,
            bonus_used=bonus_used,
            created_at=created_at,
            last_used=last_used,
            status=status,
            status_reason=status_reason,
            profile_arn=profile_arn,
        )

    @property
    def prompt_remaining(self) -> Optional[float]:
        """
        Returns the prompt quota remaining for this account.

        Bonus quota is intentionally ignored.

        Returns:
            Remaining prompt quota, clamped at zero, or ``None`` when unknown.
        """
        if self.credits_total is None or self.credits_used is None:
            return None
        remaining = self.credits_total - self.credits_used
        if not math.isfinite(remaining):
            return None
        return max(remaining, 0.0)

    @property
    def has_refresh_token(self) -> bool:
        """
        Returns whether the account snapshot can be refreshed.

        Returns:
            True when a refresh token is present.
        """
        return bool(self.refresh_token and self.refresh_token.strip())

    @property
    def has_fresh_access_token(self) -> bool:
        """
        Returns whether the account snapshot has a currently usable access token.

        Returns:
            True when the access token expiry is known and outside the refresh window.
        """
        if self.expires_at is None:
            return False
        threshold = int(datetime.now(timezone.utc).timestamp()) + TOKEN_REFRESH_THRESHOLD
        return self.expires_at > threshold

    @property
    def is_banned(self) -> bool:
        """
        Returns whether the account is marked as banned.

        Returns:
            True when status or status_reason indicates a ban.
        """
        return _is_banned_status(self.status) or _is_banned_reason(self.status_reason)


class CockpitKiroAccountPool:
    """
    Loads and ranks Cockpit-managed Kiro accounts from disk.

    The pool reads ``kiro_accounts/*.json`` and the optional
    ``provider_current_accounts.json`` mapping from a Cockpit root directory.
    """

    def __init__(self, root_dir: str | Path):
        """
        Args:
            root_dir: Path to ``.antigravity_cockpit`` or equivalent Cockpit root.
        """
        self.root_dir = Path(root_dir).expanduser()
        self.accounts_dir = self.root_dir / COCKPIT_ACCOUNTS_DIR
        self.index_path = self.root_dir / COCKPIT_ACCOUNTS_INDEX_FILE
        self.current_state_path = self.root_dir / MONTHLY_POOL_FILE
        self._accounts: List[CockpitKiroAccount] = []
        self._current_account_id: Optional[str] = None
        self._last_refreshed_at: Optional[datetime] = None

    @property
    def accounts(self) -> List[CockpitKiroAccount]:
        """
        Returns a copy of the currently loaded account snapshots.

        Returns:
            Loaded account snapshots.
        """
        return list(self._accounts)

    @property
    def current_account_id(self) -> Optional[str]:
        """
        Returns the current Cockpit-selected Kiro account id, if any.

        Returns:
            Current account id or ``None``.
        """
        return self._current_account_id

    @property
    def last_refreshed_at(self) -> Optional[datetime]:
        """
        Returns when the pool was last refreshed.

        Returns:
            Refresh timestamp or ``None`` before the first refresh.
        """
        return self._last_refreshed_at

    def _load_current_account_id(self) -> Optional[str]:
        """
        Reads provider_current_accounts.json if it exists.

        Returns:
            Current Kiro account id or ``None``.
        """
        if not self.current_state_path.exists():
            return None

        try:
            payload = json.loads(self.current_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                f"Failed to read Cockpit current account state: path={self.current_state_path}, error={exc}"
            )
            return None

        current_accounts = payload.get("current_accounts")
        if not isinstance(current_accounts, Mapping):
            return None

        current_id = current_accounts.get("kiro")
        if not isinstance(current_id, str):
            return None

        normalized = current_id.strip()
        return normalized or None

    def _load_accounts_from_disk(self) -> List[CockpitKiroAccount]:
        """
        Reads and normalizes all account snapshots from the Cockpit directory.

        Returns:
            Loaded account snapshots.
        """
        if not self.accounts_dir.exists():
            logger.warning(f"Cockpit accounts directory not found: {self.accounts_dir}")
            return []

        snapshots: List[CockpitKiroAccount] = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(f"Skipping unreadable Cockpit account file: path={path}, error={exc}")
                continue

            if not isinstance(payload, Mapping):
                logger.warning(f"Skipping non-object Cockpit account file: path={path}")
                continue

            try:
                snapshots.append(CockpitKiroAccount.from_mapping(payload, source_path=path))
            except ValueError as exc:
                logger.warning(f"Skipping invalid Cockpit account file: path={path}, error={exc}")

        return snapshots

    def refresh(self) -> None:
        """
        Reloads the account pool from disk.

        Returns:
            None.
        """
        self._current_account_id = self._load_current_account_id()
        self._accounts = self._load_accounts_from_disk()
        self._last_refreshed_at = datetime.now(timezone.utc)
        logger.debug(
            "Cockpit account pool refreshed: "
            f"root={self.root_dir}, accounts={len(self._accounts)}, current={self._current_account_id}"
        )

    def get_account(self, account_id: str) -> Optional[CockpitKiroAccount]:
        """
        Finds an account snapshot by id.

        Args:
            account_id: Account id to look up.

        Returns:
            Matching account snapshot or ``None``.
        """
        target = account_id.strip()
        if not target:
            return None
        for account in self._accounts:
            if account.account_id == target:
                return account
        return None

    def pick_next_account(
        self,
        exclude_ids: Iterable[str] = (),
        *,
        require_fresh_access_token: bool = True,
    ) -> Optional[CockpitKiroAccount]:
        """
        Picks the next usable account from the pool.

        Candidates are ranked by:
        1. prompt_remaining descending
        2. last_used ascending
        3. account_id ascending for deterministic ties

        Args:
            exclude_ids: Account ids that must not be selected.
            require_fresh_access_token: Whether candidates must have an access
                token outside the refresh window. Quota failover uses the
                conservative default; auth failover can disable this so a
                candidate with a refresh token gets one bounded refresh attempt.

        Returns:
            Best matching account or ``None`` when no usable account remains.
        """
        excluded = {value.strip() for value in exclude_ids if value and value.strip()}
        candidates = [
            account
            for account in self._accounts
            if account.account_id not in excluded
            and not account.is_banned
            and account.has_refresh_token
            and (account.has_fresh_access_token or not require_fresh_access_token)
        ]
        ranked = [
            account
            for account in candidates
            if account.prompt_remaining is not None and account.prompt_remaining > 0.0
        ]
        ranked.sort(
            key=lambda account: (
                -(account.prompt_remaining or 0.0),
                account.last_used,
                account.account_id,
            )
        )
        return ranked[0] if ranked else None
