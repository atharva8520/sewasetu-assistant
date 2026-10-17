# -*- coding: utf-8 -*-
"""
Response shaping for Sewa Setu MCP tools.
Ensures every tool response contains 'message', 'next_step', and structured fields.
"""

from typing import Any


def tool_response(message: str, next_step: str, **kwargs: Any) -> dict[str, Any]:
    """
    Format standard tool response with message, next_step, and structured fields.
    """
    resp: dict[str, Any] = {
        "message": message.strip(),
        "next_step": next_step.strip(),
    }
    resp.update(kwargs)
    return resp


def error_response(
    message: str,
    next_step: str = "Please correct the details and try again.",
    **kwargs: Any
) -> dict[str, Any]:
    """
    Format standard error response in plain language.
    """
    resp: dict[str, Any] = {
        "message": message.strip(),
        "next_step": next_step.strip(),
        "ok": False,
    }
    resp.update(kwargs)
    return resp
