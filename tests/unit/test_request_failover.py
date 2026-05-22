# -*- coding: utf-8 -*-

"""
Unit tests for monthly quota failover request helper.
"""

import json
from pathlib import Path

import httpx
import pytest

from kiro.account_pool import CockpitKiroAccountPool
from kiro.account_switcher import KiroAccountSwitcher
from kiro.request_failover import send_kiro_request_with_monthly_quota_failover
from kiro.auth import KiroAuthManager


def _write_account_file(
    accounts_dir: Path,
    account_id: str,
    *,
    credits_total: float,
    credits_used: float,
    last_used: int,
) -> Path:
    accounts_dir.mkdir(parents=True, exist_ok=True)
    path = accounts_dir / f"{account_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": account_id,
                "email": f"{account_id}@example.com",
                "access_token": f"access-{account_id}",
                "refresh_token": f"refresh-{account_id}",
                "token_type": "Bearer",
                "expires_at": 9999999999,
                "credits_total": credits_total,
                "credits_used": credits_used,
                "bonus_total": 500,
                "bonus_used": 500,
                "created_at": 1,
                "last_used": last_used,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_retries_with_next_account_after_monthly_quota_error(tmp_path, monkeypatch):
    """
    What it does: Verifies the helper retries once after monthly quota exhaustion.
    Purpose: Ensure the first exhausted account is swapped out before the retry.
    """
    print("Setup: Building a two-account Cockpit pool...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=50, last_used=200)
    second_path = _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=10, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")
    auth_manager = switcher.current_auth_manager

    monthly_error = httpx.Response(
        402,
        content=json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Monthly request limit exceeded. Account has reached its monthly quota.",
                },
            }
        ).encode("utf-8"),
    )
    success_response = httpx.Response(200, content=b'{"ok":true}')

    call_count = 0

    async def fake_request_with_retry(self, method, url, json_data, stream=False):
        nonlocal call_count
        call_count += 1
        return monthly_error if call_count == 1 else success_response

    monkeypatch.setattr("kiro.request_failover.KiroHttpClient.request_with_retry", fake_request_with_retry)

    print("Action: Sending request through the failover helper...")
    response, active_manager, switched, http_client = await send_kiro_request_with_monthly_quota_failover(
        auth_manager=auth_manager,
        account_switcher=switcher,
        url="https://example.invalid/generateAssistantResponse",
        payload={"prompt": "hello"},
        shared_client=None,
    )

    print("Verification: Second account is used for the retry...")
    assert switched is True
    assert response.status_code == 200
    assert active_manager.creds_file == str(second_path)
    assert switcher.current_account_id == "kiro_b"
    assert http_client.auth_manager.creds_file == str(second_path)
    assert call_count == 2


@pytest.mark.asyncio
async def test_does_not_switch_on_non_monthly_error(tmp_path, monkeypatch):
    """
    What it does: Verifies unrelated errors do not trigger failover.
    Purpose: Avoid account rotation on ordinary Kiro failures.
    """
    print("Setup: Building a two-account Cockpit pool...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=50, last_used=200)
    _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=10, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")
    auth_manager = switcher.current_auth_manager

    non_monthly_error = httpx.Response(
        500,
        content=json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Something else happened.",
                },
            }
        ).encode("utf-8"),
    )

    async def fake_request_with_retry(self, method, url, json_data, stream=False):
        return non_monthly_error

    monkeypatch.setattr("kiro.request_failover.KiroHttpClient.request_with_retry", fake_request_with_retry)

    print("Action: Sending request through the failover helper...")
    response, active_manager, switched, http_client = await send_kiro_request_with_monthly_quota_failover(
        auth_manager=auth_manager,
        account_switcher=switcher,
        url="https://example.invalid/generateAssistantResponse",
        payload={"prompt": "hello"},
        shared_client=None,
    )

    print("Verification: Non-monthly errors do not rotate accounts...")
    assert switched is False
    assert response.status_code == 500
    assert active_manager.creds_file == auth_manager.creds_file
    assert switcher.current_account_id == "kiro_a"
    assert http_client.auth_manager.creds_file == auth_manager.creds_file
