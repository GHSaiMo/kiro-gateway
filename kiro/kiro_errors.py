# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Kiro API error enhancement and user-friendly message formatting.

This module provides a centralized system for enhancing cryptic Kiro API errors
with clear, actionable, user-friendly messages.

Architecture:
- KiroErrorReason: Enum of known error reasons from Kiro API
- KiroErrorInfo: Structured information about an enhanced error
- enhance_kiro_error(): Analyzes error JSON and returns enhanced message

Example:
    >>> error_json = {"message": "Input is too long.", "reason": "CONTENT_LENGTH_EXCEEDS_THRESHOLD"}
    >>> error_info = enhance_kiro_error(error_json)
    >>> print(error_info.user_message)
    "Model context limit reached. Conversation size exceeds model capacity."
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Tuple

from loguru import logger


MONTHLY_QUOTA_MESSAGE = "Monthly request limit exceeded. Account has reached its monthly quota."


@dataclass
class KiroErrorInfo:
    """
    Structured information about a Kiro API error.
    
    Contains both the enhanced user-friendly message and the original
    error details for logging and debugging.
    
    Attributes:
        reason: Error reason code from Kiro API (as string, e.g. "CONTENT_LENGTH_EXCEEDS_THRESHOLD")
        user_message: Enhanced, user-friendly message for end users
        original_message: Original message from Kiro API (for logging)
    """
    reason: str
    user_message: str
    original_message: str
    is_monthly_quota_exceeded: bool = False


def _extract_error_payload(error_json: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Extracts the most relevant error payload from a raw Kiro error body.

    Args:
        error_json: Parsed JSON from Kiro or Anthropic-style error response.

    Returns:
        A tuple of (payload, source) where payload contains the normalized
        message/reason fields and source is the original nested payload used
        for fallbacks.
    """
    if not isinstance(error_json, Mapping):
        return {}, {}

    nested_error = error_json.get("error")
    if isinstance(nested_error, Mapping):
        payload = dict(nested_error)
        payload.setdefault("message", error_json.get("message"))
        payload.setdefault("reason", error_json.get("reason"))
        payload.setdefault("type", error_json.get("type"))
        return payload, dict(nested_error)

    return dict(error_json), dict(error_json)


def _is_monthly_quota_message(message: str) -> bool:
    """
    Checks whether a message matches the hard-coded monthly quota error text.

    Args:
        message: Error message string.

    Returns:
        True when the message matches the known monthly quota exhaustion text.
    """
    normalized = message.strip()
    return normalized == MONTHLY_QUOTA_MESSAGE


def enhance_kiro_error(error_json: Dict[str, Any]) -> KiroErrorInfo:
    """
    Enhances Kiro API error with user-friendly message.
    
    Takes raw error JSON from Kiro API and returns structured information
    with enhanced, user-friendly messages that help users understand what
    went wrong without technical jargon.
    
    Args:
        error_json: Parsed JSON from Kiro API error response
                   Expected format: {"message": "...", "reason": "..."}
                   The "reason" field is optional.
    
    Returns:
        KiroErrorInfo with enhanced message and original details
    
    Example:
        >>> error_json = {"message": "Input is too long.", "reason": "CONTENT_LENGTH_EXCEEDS_THRESHOLD"}
        >>> error_info = enhance_kiro_error(error_json)
        >>> print(error_info.user_message)
        "Model context limit reached. Conversation size exceeds model capacity."
        >>> print(error_info.original_message)
        "Input is too long."
    
    Example (unknown error):
        >>> error_json = {"message": "Something went wrong.", "reason": "UNKNOWN_REASON"}
        >>> error_info = enhance_kiro_error(error_json)
        >>> print(error_info.user_message)
        "Something went wrong. (reason: UNKNOWN_REASON)"
    """
    payload, source = _extract_error_payload(error_json)

    # Extract original message and reason from Kiro API response
    # Handle None values explicitly (preserve empty strings)
    original_message = payload.get("message")
    if original_message is None:
        original_message = "Unknown error"

    reason = payload.get("reason")
    if reason is None:
        if _is_monthly_quota_message(str(original_message)):
            reason = "MONTHLY_REQUEST_COUNT"
        else:
            reason = "UNKNOWN"

    is_monthly_quota_exceeded = reason == "MONTHLY_REQUEST_COUNT" or _is_monthly_quota_message(
        str(original_message)
    )

    # Map known reasons to user-friendly messages
    if reason == "CONTENT_LENGTH_EXCEEDS_THRESHOLD":
        # Context limit exceeded - conversation is too long
        user_message = "Model context limit reached. Conversation size exceeds model capacity."
    
    elif is_monthly_quota_exceeded:
        # Monthly request limit exceeded - account quota exhausted
        user_message = MONTHLY_QUOTA_MESSAGE
    
    # Future error enhancements can be added here:
    # elif reason == "RATE_LIMIT_EXCEEDED":
    #     user_message = "Rate limit exceeded. Too many requests in a short time."
    # elif reason == "INVALID_MODEL":
    #     user_message = "Invalid model specified. The requested model is not available."
    
    else:
        # Unknown error or no enhancement available
        # Keep original message and append reason if present
        if "reason" in source and reason != "UNKNOWN":
            user_message = f"{original_message} (reason: {reason})"
        else:
            user_message = original_message

    return KiroErrorInfo(
        reason=reason,
        user_message=user_message,
        original_message=original_message,
        is_monthly_quota_exceeded=is_monthly_quota_exceeded,
    )
