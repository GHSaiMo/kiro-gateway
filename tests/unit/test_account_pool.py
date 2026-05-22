# -*- coding: utf-8 -*-

"""
Unit tests for Cockpit Kiro account pool loading and ranking.
"""

import json
from pathlib import Path

from kiro.account_pool import CockpitKiroAccount, CockpitKiroAccountPool


def _write_account_file(
    accounts_dir: Path,
    account_id: str,
    *,
    credits_total: float,
    credits_used: float,
    bonus_total: float = 0.0,
    bonus_used: float = 0.0,
    last_used: int = 1,
    created_at: int = 1,
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
                "bonus_total": bonus_total,
                "bonus_used": bonus_used,
                "created_at": created_at,
                "last_used": last_used,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_prompt_remaining_ignores_bonus_fields(tmp_path):
    """
    What it does: Verifies prompt remaining is calculated from prompt credits only.
    Purpose: Ensure bonus quota never affects auto-switch ranking.
    """
    print("Setup: Creating Cockpit account snapshot with bonus credits...")
    account = CockpitKiroAccount.from_mapping(
        {
            "id": "kiro_1",
            "email": "a@example.com",
            "access_token": "token",
            "refresh_token": "refresh",
            "credits_total": 50,
            "credits_used": 12,
            "bonus_total": 500,
            "bonus_used": 500,
            "created_at": 1,
            "last_used": 2,
        },
        source_path=tmp_path / "kiro_accounts" / "kiro_1.json",
    )

    print("Verification: Only credits_total - credits_used is counted...")
    assert account.prompt_remaining == 38


def test_float_timestamps_are_normalized_without_error(tmp_path):
    """
    What it does: Verifies float Cockpit timestamps are normalized to integers.
    Purpose: Ensure account snapshots with decimal epoch values do not crash loading.
    """
    print("Setup: Creating Cockpit account snapshot with float timestamps...")
    account = CockpitKiroAccount.from_mapping(
        {
            "id": "kiro_float_time",
            "email": "float@example.com",
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": 9999999999.9,
            "credits_total": 10,
            "credits_used": 1,
            "created_at": 100.5,
            "last_used": 200.5,
        },
        source_path=tmp_path / "kiro_accounts" / "kiro_float_time.json",
    )

    print("Verification: Float timestamps are converted with int semantics...")
    assert account.expires_at == 9999999999
    assert account.created_at == 100
    assert account.last_used == 200


def test_pool_prefers_higher_prompt_remaining_then_older_last_used(tmp_path):
    """
    What it does: Verifies candidate selection follows prompt remaining first.
    Purpose: Ensure the next account is chosen by usable prompt quota, not bonus.
    """
    print("Setup: Writing Cockpit account snapshots...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=200)
    _write_account_file(accounts_dir, "kiro_b", credits_total=40, credits_used=0, last_used=300)
    _write_account_file(accounts_dir, "kiro_c", credits_total=50, credits_used=10, last_used=100)

    print("Action: Loading pool and selecting next candidate...")
    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    picked = pool.pick_next_account(exclude_ids={"kiro_a"})

    print("Verification: Highest prompt remaining wins, then older last_used breaks ties...")
    assert picked is not None
    assert picked.account_id == "kiro_c"


def test_pool_reads_current_account_mapping(tmp_path):
    """
    What it does: Verifies provider_current_accounts.json is read when present.
    Purpose: Ensure Gateway can start from Cockpit's current Kiro account.
    """
    print("Setup: Writing current account mapping and snapshot files...")
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=200)
    _write_account_file(accounts_dir, "kiro_b", credits_total=40, credits_used=0, last_used=300)
    (root_dir / "provider_current_accounts.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "current_accounts": {"kiro": "kiro_b"},
            }
        ),
        encoding="utf-8",
    )

    print("Action: Loading pool...")
    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()

    print("Verification: Current account mapping is exposed...")
    assert pool.current_account_id == "kiro_b"
