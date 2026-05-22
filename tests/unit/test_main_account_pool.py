# -*- coding: utf-8 -*-

"""
Unit tests for application-level Cockpit account pool detection.
"""

import main


def test_resolve_cockpit_pool_root_detects_default_cockpit_directory(tmp_path, monkeypatch):
    """
    What it does: Verifies the default Cockpit root is detected without env config.
    Purpose: Ensure `python main.py` enables account failover on machines with Cockpit installed.
    """
    print("Setup: Creating default Cockpit account pool under a fake home directory...")
    default_root = tmp_path / ".antigravity_cockpit"
    (default_root / "kiro_accounts").mkdir(parents=True)
    monkeypatch.setattr(main.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(main, "KIRO_ACCOUNT_POOL_ROOT", "")
    monkeypatch.setattr(main, "KIRO_CREDS_FILE", "~/.aws/sso/cache/kiro-auth-token.json")

    print("Action: Resolving Cockpit account pool root...")
    resolved_root = main._resolve_cockpit_pool_root()

    print("Verification: Default Cockpit root is selected...")
    assert resolved_root == default_root
