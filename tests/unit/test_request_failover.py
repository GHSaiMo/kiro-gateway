# -*- coding: utf-8 -*-

"""
Unit tests for account failover request helper.
"""

import json
from pathlib import Path
from typing import Optional

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


class _FakeAuthRefreshFailingClient:
    """
    Test client that fails the first auth refresh attempt and then succeeds.
    """

    instances = []
    call_count = 0

    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Args:
            auth_manager: Auth manager attached to this request attempt.
            shared_client: Ignored shared client placeholder.
        """
        self.auth_manager = auth_manager
        self.closed = False
        self.instances.append(self)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: dict,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Simulates a token refresh failure before the first upstream request.

        Args:
            method: HTTP method.
            url: Request URL.
            json_data: JSON request body.
            stream: Whether streaming mode was requested.

        Returns:
            Successful HTTP response after failover.

        Raises:
            httpx.HTTPStatusError: First call only, matching auth refresh failures.
        """
        type(self).call_count += 1
        request = httpx.Request(method, url)
        if type(self).call_count == 1:
            response = httpx.Response(401, content=b"Unauthorized", request=request)
            raise httpx.HTTPStatusError(
                "Client error '401 Unauthorized' for auth refresh",
                request=request,
                response=response,
            )
        return httpx.Response(200, content=b'{"ok":true}', request=request)

    async def close(self) -> None:
        """
        Marks this fake client as closed.

        Returns:
            None.
        """
        self.closed = True


class _FakeAuthRefreshStatusSequenceClient:
    """
    Test client that raises configured auth refresh statuses in sequence.
    """

    instances = []
    statuses = []
    call_count = 0

    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Args:
            auth_manager: Auth manager attached to this request attempt.
            shared_client: Ignored shared client placeholder.
        """
        self.auth_manager = auth_manager
        self.closed = False
        self.instances.append(self)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: dict,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Raises the next configured status or returns success.

        Args:
            method: HTTP method.
            url: Request URL.
            json_data: JSON request body.
            stream: Whether streaming mode was requested.

        Returns:
            Successful HTTP response when the next status is 200.

        Raises:
            httpx.HTTPStatusError: For configured non-200 statuses.
        """
        type(self).call_count += 1
        request = httpx.Request(method, url)
        status = type(self).statuses.pop(0)
        if status == 200:
            return httpx.Response(200, content=b'{"ok":true}', request=request)
        response = httpx.Response(status, content=b"refresh failed", request=request)
        raise httpx.HTTPStatusError(
            f"Client error '{status}' for auth refresh",
            request=request,
            response=response,
        )

    async def close(self) -> None:
        """
        Marks this fake client as closed.

        Returns:
            None.
        """
        self.closed = True


class _FakeMixedOutcomeClient:
    """
    Test client that returns or raises configured outcomes in order.
    """

    instances = []
    outcomes = []
    call_count = 0

    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Args:
            auth_manager: Auth manager attached to this request attempt.
            shared_client: Ignored shared client placeholder.
        """
        self.auth_manager = auth_manager
        self.closed = False
        self.instances.append(self)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: dict,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Applies the next configured outcome.

        Args:
            method: HTTP method.
            url: Request URL.
            json_data: JSON request body.
            stream: Whether streaming mode was requested.

        Returns:
            Configured HTTP response.

        Raises:
            httpx.HTTPStatusError: When the configured outcome is an integer status.
        """
        type(self).call_count += 1
        request = httpx.Request(method, url)
        outcome = type(self).outcomes.pop(0)
        if isinstance(outcome, int):
            response = httpx.Response(outcome, content=b"refresh failed", request=request)
            raise httpx.HTTPStatusError(
                f"Client error '{outcome}' for auth refresh",
                request=request,
                response=response,
            )
        return outcome

    async def close(self) -> None:
        """
        Marks this fake client as closed.

        Returns:
            None.
        """
        self.closed = True


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


@pytest.mark.asyncio
async def test_retries_with_next_account_after_auth_refresh_unauthorized(tmp_path):
    """
    What it does: Verifies auth refresh 401 switches accounts before retrying.
    Purpose: Avoid surfacing expired Cockpit refresh tokens as request-level 500s.
    """
    print("Setup: Building a pool where the fallback access token is stale but refreshable...")
    _FakeAuthRefreshFailingClient.instances = []
    _FakeAuthRefreshFailingClient.call_count = 0
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=200)
    fallback_path = _write_account_file(
        accounts_dir,
        "kiro_b",
        credits_total=50,
        credits_used=5,
        last_used=100,
        expires_at=1,
    )

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")
    auth_manager = switcher.current_auth_manager

    print("Action: Sending request through failover after auth refresh rejects the first account...")
    response, active_manager, switched, http_client = await send_kiro_request_with_monthly_quota_failover(
        auth_manager=auth_manager,
        account_switcher=switcher,
        url="https://example.invalid/generateAssistantResponse",
        payload={"prompt": "hello"},
        shared_client=None,
        client_factory=_FakeAuthRefreshFailingClient,
    )

    print("Verification: The stale-access fallback account is tried and succeeds...")
    assert switched is True
    assert response.status_code == 200
    assert active_manager.creds_file == str(fallback_path)
    assert switcher.current_account_id == "kiro_b"
    assert http_client.auth_manager.creds_file == str(fallback_path)
    assert _FakeAuthRefreshFailingClient.call_count == 2
    assert _FakeAuthRefreshFailingClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_does_not_switch_on_auth_refresh_server_error(tmp_path):
    """
    What it does: Verifies auth-provider 5xx errors are not treated as account failures.
    Purpose: Avoid rotating accounts when the refresh service itself is unhealthy.
    """
    print("Setup: Building a two-account pool and a 500 refresh failure...")
    _FakeAuthRefreshStatusSequenceClient.instances = []
    _FakeAuthRefreshStatusSequenceClient.statuses = [500]
    _FakeAuthRefreshStatusSequenceClient.call_count = 0
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=200)
    _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=5, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")

    print("Action: Sending request through failover with auth service 500...")
    with pytest.raises(httpx.HTTPStatusError):
        await send_kiro_request_with_monthly_quota_failover(
            auth_manager=switcher.current_auth_manager,
            account_switcher=switcher,
            url="https://example.invalid/generateAssistantResponse",
            payload={"prompt": "hello"},
            shared_client=None,
            client_factory=_FakeAuthRefreshStatusSequenceClient,
        )

    print("Verification: The active account is unchanged and the failed client is closed...")
    assert switcher.current_account_id == "kiro_a"
    assert _FakeAuthRefreshStatusSequenceClient.call_count == 1
    assert _FakeAuthRefreshStatusSequenceClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_rotates_future_account_when_retry_auth_refresh_also_fails(tmp_path):
    """
    What it does: Verifies a failing fallback auth refresh rotates future active account once.
    Purpose: Keep subsequent requests from repeatedly starting on the same bad fallback.
    """
    print("Setup: Building a three-account pool with two auth refresh failures...")
    _FakeAuthRefreshStatusSequenceClient.instances = []
    _FakeAuthRefreshStatusSequenceClient.statuses = [401, 429]
    _FakeAuthRefreshStatusSequenceClient.call_count = 0
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=300)
    _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=3, last_used=200)
    third_path = _write_account_file(accounts_dir, "kiro_c", credits_total=45, credits_used=0, last_used=100)

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")

    print("Action: Sending request where both active and fallback refresh attempts fail...")
    with pytest.raises(httpx.HTTPStatusError):
        await send_kiro_request_with_monthly_quota_failover(
            auth_manager=switcher.current_auth_manager,
            account_switcher=switcher,
            url="https://example.invalid/generateAssistantResponse",
            payload={"prompt": "hello"},
            shared_client=None,
            client_factory=_FakeAuthRefreshStatusSequenceClient,
        )

    print("Verification: The next future request starts on the third account...")
    assert switcher.current_account_id == "kiro_c"
    assert switcher.current_auth_manager.creds_file == str(third_path)
    assert _FakeAuthRefreshStatusSequenceClient.call_count == 2
    assert all(instance.closed for instance in _FakeAuthRefreshStatusSequenceClient.instances)


@pytest.mark.asyncio
async def test_monthly_quota_after_auth_failover_switches_from_retry_account(tmp_path):
    """
    What it does: Verifies monthly quota after auth failover excludes the retry account.
    Purpose: Ensure combined auth and quota failures do not retry an exhausted fallback.
    """
    print("Setup: Building a three-account pool and mixed auth/quota outcomes...")
    _FakeMixedOutcomeClient.instances = []
    _FakeMixedOutcomeClient.call_count = 0
    root_dir = tmp_path / ".antigravity_cockpit"
    accounts_dir = root_dir / "kiro_accounts"
    _write_account_file(accounts_dir, "kiro_a", credits_total=50, credits_used=10, last_used=300)
    _write_account_file(accounts_dir, "kiro_b", credits_total=50, credits_used=3, last_used=200)
    third_path = _write_account_file(accounts_dir, "kiro_c", credits_total=45, credits_used=0, last_used=100)

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
        request=httpx.Request("POST", "https://example.invalid/generateAssistantResponse"),
    )
    success_response = httpx.Response(
        200,
        content=b'{"ok":true}',
        request=httpx.Request("POST", "https://example.invalid/generateAssistantResponse"),
    )
    _FakeMixedOutcomeClient.outcomes = [401, monthly_error, success_response]

    pool = CockpitKiroAccountPool(root_dir=root_dir)
    pool.refresh()
    switcher = KiroAccountSwitcher(pool=pool, current_account_id="kiro_a")

    print("Action: Sending request where auth failover succeeds but the retry account is quota-exhausted...")
    response, active_manager, switched, http_client = await send_kiro_request_with_monthly_quota_failover(
        auth_manager=switcher.current_auth_manager,
        account_switcher=switcher,
        url="https://example.invalid/generateAssistantResponse",
        payload={"prompt": "hello"},
        shared_client=None,
        client_factory=_FakeMixedOutcomeClient,
    )

    print("Verification: Monthly quota rotation excludes the retry account B and uses account C...")
    assert switched is True
    assert response.status_code == 200
    assert active_manager.creds_file == str(third_path)
    assert switcher.current_account_id == "kiro_c"
    assert http_client.auth_manager.creds_file == str(third_path)
    assert _FakeMixedOutcomeClient.call_count == 3
    assert _FakeMixedOutcomeClient.instances[0].closed is True
    assert _FakeMixedOutcomeClient.instances[1].closed is True
