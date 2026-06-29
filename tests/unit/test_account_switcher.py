# -*- coding: utf-8 -*-

"""
Unit tests for Kiro account switch controller.
"""

import json
from pathlib import Path

import pytest

from kiro.account_pool import CockpitKiroAccountPool
from kiro.account_switcher import KiroAccountSwitcher


def _write_account_file(
    accounts_dir: Path,
    account_id: str,
    *,
    credits_total: float,
    credits_used: float,
    last_used: int,
    expires_at: int = 9999999999,
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
                "expires_at": expires_at,
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
async def test_switches_to_next_account_by_prompt_remaining(tmp_path):
    """
    What it does: Verifies the controller can rotate to the next account.
    Purpose: Ensure monthly quota exhaustion triggers a real account switch.
    """
    print("Setup: Building Cockpit account pool with two usable accounts...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    first_path = _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=50, last_used=200)
    second_path = _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=10, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()

    print("Action: Switching away from the exhausted account...")
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")
    new_manager = await switcher.switch_to_next_account(exhausted_account_id="kiro_a")

    print("Verification: Next account is selected and activated...")
    assert new_manager is not None
    assert switcher.current_account_id == "kiro_b"
    assert switcher.current_auth_manager is not None
    assert switcher.current_auth_manager.creds_file == str(second_path)
    assert switcher.current_auth_manager.creds_file != str(first_path)


def test_initial_account_skips_expired_preferred_account(tmp_path):
    """
    What it does: Verifies startup skips a preferred account with an expired access token.
    Purpose: Avoid binding Gateway to stale Cockpit snapshots that will fail refresh immediately.
    """
    print("Setup: Building Cockpit account pool with expired preferred account...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    expired_path = _write_account_file(
        accounts_dir,
        "kiro_expired",
        credits_total=50,
        credits_used=0,
        last_used=1,
        expires_at=1,
    )
    fresh_path = _write_account_file(
        accounts_dir,
        "kiro_fresh",
        credits_total=40,
        credits_used=0,
        last_used=100,
        expires_at=9999999999,
    )

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()

    print("Action: Creating switcher with expired account as preferred current...")
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_expired")

    print("Verification: Fresh fallback is selected instead of expired preferred account...")
    assert switcher.current_account_id == "kiro_fresh"
    assert switcher.current_auth_manager.creds_file == str(fresh_path)
    assert switcher.current_auth_manager.creds_file != str(expired_path)


@pytest.mark.asyncio
async def test_stale_exhausted_account_reuses_already_switched_current_account(tmp_path):
    """
    What it does: Simulates two concurrent requests reporting the same old account.
    Purpose: Ensure stale quota errors do not skip a valid current fallback account.
    """
    print("Setup: Building Cockpit account pool with three usable fallback accounts...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=50, last_used=300)
    second_path = _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=10, last_used=200)
    _write_account_file(accounts_dir, "kiro_c", credits_total=30, credits_used=0, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")

    print("Action: First request switches from exhausted account A to account B...")
    first_manager = await switcher.switch_to_next_account(exhausted_account_id="kiro_a")
    assert first_manager is not None
    assert switcher.current_account_id == "kiro_b"

    print("Action: A stale second request also reports account A as exhausted...")
    second_manager = await switcher.switch_to_next_account(exhausted_account_id="kiro_a")

    print("Verification: The switcher reuses B instead of skipping to C...")
    assert second_manager is first_manager
    assert switcher.current_account_id == "kiro_b"
    assert switcher.current_auth_manager.creds_file == str(second_path)


@pytest.mark.asyncio
async def test_switch_returns_none_when_no_alternate_account_exists(tmp_path):
    """
    What it does: Verifies switch gracefully stops when no fallback account exists.
    Purpose: Avoid looping forever when the pool is exhausted.
    """
    print("Setup: Building a pool with only one account...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=50, last_used=200)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()

    print("Action: Attempting to switch with no fallback available...")
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")
    new_manager = await switcher.switch_to_next_account(exhausted_account_id="kiro_a")

    print("Verification: No alternate manager is returned...")
    assert new_manager is None
    assert switcher.current_account_id == "kiro_a"
