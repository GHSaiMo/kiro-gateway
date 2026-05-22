# -*- coding: utf-8 -*-

"""
Monthly quota failover helper for Kiro requests.

The helper performs a single retry on monthly quota exhaustion:
1. Send the request with the active auth manager.
2. If the response reports monthly quota exhaustion, switch to the next account.
3. Retry once with the new account.
4. If the retry also reports monthly quota exhaustion, rotate the active manager
   for future requests but do not retry again in the same request.
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
    Sends one Kiro request and retries once on monthly quota exhaustion.

    Args:
        auth_manager: Current auth manager used for the first attempt.
        account_switcher: Optional Cockpit account switch controller.
        url: Kiro API URL.
        payload: Request payload.
        shared_client: Shared httpx client for non-streaming requests.

    Returns:
        Tuple of ``(response, future_active_auth_manager, switched, http_client)``.
        The returned ``http_client`` is the client that produced ``response`` and
        should be used by the caller for any downstream stream consumption.
    """
    http_client = client_factory(auth_manager, shared_client=shared_client)
    response = await http_client.request_with_retry("POST", url, payload, stream=True)
    future_auth_manager = auth_manager
    switched = False

    if response.status_code == 200 or account_switcher is None:
        return response, future_auth_manager, switched, http_client

    error_json = await _read_json_response(response)
    if not error_json:
        return response, future_auth_manager, switched, http_client

    error_info = enhance_kiro_error(error_json)
    if not error_info.is_monthly_quota_exceeded:
        return response, future_auth_manager, switched, http_client

    exhausted_account_id = _extract_account_id(auth_manager)
    logger.warning(
        "Monthly quota exhausted; switching Kiro account: "
        f"account_id={exhausted_account_id or account_switcher.current_account_id}"
    )

    new_auth_manager = await account_switcher.switch_to_next_account(
        exhausted_account_id=exhausted_account_id
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
                    exhausted_account_id=_extract_account_id(new_auth_manager)
                )
                if post_retry_manager is not None:
                    future_auth_manager = post_retry_manager

    return response, future_auth_manager, switched, http_client
