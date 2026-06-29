# -*- coding: utf-8 -*-

"""
Cockpit Kiro account switch controller.

This controller owns the active Kiro auth manager and rotates through the
Cockpit account pool when a monthly quota exhaustion event is detected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Set

from loguru import logger

from kiro.account_pool import CockpitKiroAccount, CockpitKiroAccountPool
from kiro.auth import KiroAuthManager


class KiroAccountSwitcher:
    """
    Manages the active Kiro account and rotates to the next one on exhaustion.
    """

    def __init__(
        self,
        pool: CockpitKiroAccountPool,
        current_account_id: Optional[str] = None,
    ):
        """
        Args:
            pool: Loaded Cockpit account pool.
            current_account_id: Optional explicit starting account id.

        Raises:
            ValueError: If the pool does not contain any usable account.
        """
        self._pool = pool
        self._lock = asyncio.Lock()
        self._exhausted_account_ids: Set[str] = set()

        self._current_account: Optional[CockpitKiroAccount] = self._select_initial_account(
            current_account_id
        )
        if self._current_account is None:
            raise ValueError("Cockpit 账号池中没有可用账号")

        self._current_auth_manager = self._build_auth_manager(self._current_account)
        logger.info(
            "Kiro account switcher initialized: "
            f"current_account={self._current_account.account_id}, "
            f"prompt_remaining={self._current_account.prompt_remaining}"
        )

    @staticmethod
    def _build_auth_manager(account: CockpitKiroAccount) -> KiroAuthManager:
        """
        Builds a Kiro auth manager from a Cockpit account snapshot.

        Args:
            account: Cockpit account snapshot.

        Returns:
            Auth manager bound to the account snapshot file.
        """
        return KiroAuthManager(creds_file=str(account.source_path))

    @staticmethod
    def _is_usable_account(account: CockpitKiroAccount) -> bool:
        """
        Checks whether an account can be used for future Kiro requests.

        Args:
            account: Cockpit account snapshot.

        Returns:
            True when the account is not banned, has refresh credentials, and
            reports remaining prompt quota.
        """
        remaining = account.prompt_remaining
        return (
            not account.is_banned
            and account.has_refresh_token
            and account.has_fresh_access_token
            and remaining is not None
            and remaining > 0.0
        )

    def _select_initial_account(
        self,
        current_account_id: Optional[str],
    ) -> Optional[CockpitKiroAccount]:
        """
        Selects the initial active account from the pool.

        Args:
            current_account_id: Explicit active account id, if provided.

        Returns:
            Initial active account snapshot or ``None``.
        """
        preferred_ids = [current_account_id, self._pool.current_account_id]
        for candidate_id in preferred_ids:
            if not candidate_id:
                continue
            account = self._pool.get_account(candidate_id)
            if (
                account
                and not account.is_banned
                and account.has_refresh_token
                and account.has_fresh_access_token
            ):
                return account

        return self._pool.pick_next_account()

    @property
    def current_account_id(self) -> str:
        """
        Returns the active Cockpit account id.

        Returns:
            Active account id.
        """
        return self._current_account.account_id

    @property
    def current_auth_manager(self) -> KiroAuthManager:
        """
        Returns the active auth manager.

        Returns:
            Active Kiro auth manager.
        """
        return self._current_auth_manager

    @property
    def current_account(self) -> CockpitKiroAccount:
        """
        Returns the active account snapshot.

        Returns:
            Active Cockpit account snapshot.
        """
        return self._current_account

    async def switch_to_next_account(
        self,
        exhausted_account_id: Optional[str] = None,
    ) -> Optional[KiroAuthManager]:
        """
        Marks the exhausted account and rotates to the next usable account.

        Args:
            exhausted_account_id: Account id that just hit monthly quota.

        Returns:
            New active auth manager, or ``None`` if no alternate account exists.
        """
        async with self._lock:
            self._pool.refresh()

            exhausted_id = (
                exhausted_account_id.strip()
                if exhausted_account_id and exhausted_account_id.strip()
                else self._current_account.account_id
            )
            self._exhausted_account_ids.add(exhausted_id)

            current_id = self._current_account.account_id
            refreshed_current = self._pool.get_account(current_id)
            if exhausted_id != current_id and refreshed_current is not None:
                if self._is_usable_account(refreshed_current):
                    self._current_account = refreshed_current
                    logger.info(
                        "Kiro account was already switched by another request; "
                        f"reusing current_account={current_id}"
                    )
                    return self._current_auth_manager

            candidates = set(self._exhausted_account_ids)
            if current_id == exhausted_id or current_id in self._exhausted_account_ids:
                candidates.add(current_id)
            next_account = self._pool.pick_next_account(exclude_ids=candidates)
            if next_account is None:
                logger.warning(
                    "No alternate Cockpit account available after monthly quota exhaustion: "
                    f"current_account={current_id}"
                )
                return None

            self._current_account = next_account
            self._current_auth_manager = self._build_auth_manager(next_account)
            logger.info(
                "Switched Kiro account after monthly quota exhaustion: "
                f"account_id={next_account.account_id}, "
                f"email={next_account.email}, "
                f"prompt_remaining={next_account.prompt_remaining}"
            )
            return self._current_auth_manager

    async def refresh_pool(self) -> None:
        """
        Refreshes the account pool snapshot without switching accounts.

        Returns:
            None.
        """
        async with self._lock:
            self._pool.refresh()
