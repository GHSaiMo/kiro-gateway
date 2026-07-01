# -*- coding: utf-8 -*-

"""
Account failover helper for Kiro requests.

The helper performs bounded retries for account-specific failures:
1. Send the request with the active auth manager.
2. If token refresh fails with an account-auth status, switch accounts and
   retry once.
3. If the response reports monthly quota exhaustion, switch accounts and retry
   once.
4. If the retry account also fails, rotate the active manager for future
   requests where possible, but do not loop in the same request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx
from loguru import logger

from kiro.account_switcher import KiroAccountSwitcher
from kiro.auth import KiroAuthManager
from kiro.http_client import KiroHttpClient
from kiro.kiro_errors import enhance_kiro_error

AUTH_REFRESH_FAILOVER_STATUS_CODES = {400, 401, 403, 429}


def _extract_account_id(auth_manager: KiroAuthManager) -> Optional[str]:
    """
    Extracts a stable account id from the auth manager's source file path.

    Args:
        auth_manager: Active Kiro auth manager.

    Returns:
        Account id or ``None`` when the auth source is not a Cockpit snapshot.
    """
    creds_file = auth_manager.creds_file
    if not creds_file:
        return None

    path = Path(creds_file)
    if not path.stem:
        return None
    return path.stem


def _is_auth_refresh_failover_error(exc: httpx.HTTPStatusError) -> bool:
    """
    Checks whether a token refresh failure should trigger account failover.

    Args:
        exc: HTTP status error raised while obtaining or refreshing a token.

    Returns:
        True when the status is account-specific enough to try another account.
    """
    return exc.response.status_code in AUTH_REFRESH_FAILOVER_STATUS_CODES


async def _switch_after_auth_refresh_failure(
    *,
    account_switcher: KiroAccountSwitcher,
    auth_manager: KiroAuthManager,
    status_code: int,
) -> Optional[KiroAuthManager]:
    """
    Rotates to another account after refresh credentials fail.

    Args:
        account_switcher: Cockpit account switch controller.
        auth_manager: Auth manager whose refresh failed.
        status_code: HTTP status returned by the token refresh endpoint.

    Returns:
        New active auth manager or ``None`` when no fallback account exists.
    """
    failed_account_id = _extract_account_id(auth_manager)
    logger.warning(
        "Kiro token refresh failed; switching Kiro account: "
        f"status={status_code}, "
        f"account_id={failed_account_id or account_switcher.current_account_id}"
    )
    return await account_switcher.switch_to_next_account(
        exhausted_account_id=failed_account_id,
        require_fresh_access_token=False,
        reason="auth refresh failure",
    )


async def _read_json_response(response: httpx.Response) -> Optional[dict[str, Any]]:
    """
    Reads a JSON error payload from an HTTP response.

    Args:
        response: HTTP response object.

    Returns:
        Parsed JSON object or ``None`` when parsing fails.
    """
    try:
        body = await response.aread()
    except Exception as exc:
        logger.warning(f"Failed to read upstream error response body: {exc}")
        return None

    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


async def send_kiro_request_with_monthly_quota_failover(
    *,
    auth_manager: KiroAuthManager,
    account_switcher: Optional[KiroAccountSwitcher],
    url: str,
    payload: dict[str, Any],
    shared_client: Optional[httpx.AsyncClient],
    client_factory: type[KiroHttpClient] = KiroHttpClient,
) -> Tuple[httpx.Response, KiroAuthManager, bool, KiroHttpClient]:
    """
    Sends one Kiro request with bounded account failover.

    Args:
        auth_manager: Current auth manager used for the first attempt.
        account_switcher: Optional Cockpit account switch controller.
        url: Kiro API URL.
        payload: Request payload.
        shared_client: Shared httpx client for non-streaming requests.
        client_factory: HTTP client wrapper factory.

    Returns:
        Tuple of ``(response, future_active_auth_manager, switched, http_client)``.
        The returned ``http_client`` is the client that produced ``response`` and
        should be used by the caller for any downstream stream consumption.
    """
    future_auth_manager = auth_manager
    response_auth_manager = auth_manager
    switched = False
    http_client = client_factory(auth_manager, shared_client=shared_client)

    try:
        response = await http_client.request_with_retry("POST", url, payload, stream=True)
    except httpx.HTTPStatusError as exc:
        if account_switcher is None or not _is_auth_refresh_failover_error(exc):
            await http_client.close()
            raise

        new_auth_manager = await _switch_after_auth_refresh_failure(
            account_switcher=account_switcher,
            auth_manager=auth_manager,
            status_code=exc.response.status_code,
        )
        if new_auth_manager is None:
            await http_client.close()
            raise

        switched = True
        future_auth_manager = new_auth_manager
        await http_client.close()

        retry_http_client = client_factory(new_auth_manager, shared_client=shared_client)
        try:
            response = await retry_http_client.request_with_retry("POST", url, payload, stream=True)
        except httpx.HTTPStatusError as retry_exc:
            if _is_auth_refresh_failover_error(retry_exc):
                logger.warning(
                    "Fallback Kiro account token refresh also failed; rotating future "
                    "active account without retrying again in the same request"
                )
                post_retry_manager = await _switch_after_auth_refresh_failure(
                    account_switcher=account_switcher,
                    auth_manager=new_auth_manager,
                    status_code=retry_exc.response.status_code,
                )
                if post_retry_manager is not None:
                    future_auth_manager = post_retry_manager
            await retry_http_client.close()
            raise

        http_client = retry_http_client
        response_auth_manager = new_auth_manager

    if response.status_code == 200 or account_switcher is None:
        return response, future_auth_manager, switched, http_client

    error_json = await _read_json_response(response)
    if not error_json:
        return response, future_auth_manager, switched, http_client

    error_info = enhance_kiro_error(error_json)
    if not error_info.is_monthly_quota_exceeded:
        return response, future_auth_manager, switched, http_client

    exhausted_account_id = _extract_account_id(response_auth_manager)
    logger.warning(
        "Monthly quota exhausted; switching Kiro account: "
        f"account_id={exhausted_account_id or account_switcher.current_account_id}"
    )

    new_auth_manager = await account_switcher.switch_to_next_account(
        exhausted_account_id=exhausted_account_id,
        reason="monthly quota exhaustion",
    )
    if new_auth_manager is None:
        return response, future_auth_manager, switched, http_client

    switched = True
    future_auth_manager = new_auth_manager
    await http_client.close()

    retry_http_client = client_factory(new_auth_manager, shared_client=shared_client)
    retry_response = await retry_http_client.request_with_retry("POST", url, payload, stream=True)
    response = retry_response
    http_client = retry_http_client

    if retry_response.status_code != 200:
        retry_error_json = await _read_json_response(retry_response)
        if retry_error_json:
            retry_error_info = enhance_kiro_error(retry_error_json)
            if retry_error_info.is_monthly_quota_exceeded:
                logger.warning(
                    "Fallback Kiro account also exhausted; rotating future active account "
                    "without retrying again in the same request"
                )
                post_retry_manager = await account_switcher.switch_to_next_account(
                    exhausted_account_id=_extract_account_id(new_auth_manager),
                    reason="monthly quota exhaustion",
                )
                if post_retry_manager is not None:
                    future_auth_manager = post_retry_manager

    return response, future_auth_manager, switched, http_client
