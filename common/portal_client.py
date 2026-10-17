# -*- coding: utf-8 -*-
"""
Portal client for Sewa Setu MCP servers.
Communicates strictly with the internal agent API (/internal/agent/*) using
internal API keys and verified subject/client headers.
Never forwards client tokens. Enforces 5s timeouts and safe connection retries.
"""

import logging
from typing import Any, Tuple
import httpx

logger = logging.getLogger("sewasetu.portal_client")


class PortalClient:
    def __init__(self, portal_internal_url: str, agent_internal_key: str):
        self.base_url = portal_internal_url.rstrip("/")
        self.internal_key = agent_internal_key
        # Timeout: 5.0 seconds total, 5.0s connect
        self._timeout = httpx.Timeout(5.0, connect=5.0, read=5.0)

    async def call(
        self,
        method: str,
        endpoint: str,
        subject: str,
        client_id: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Tuple[int, dict[str, Any]]:
        """
        Execute an internal agent API request.
        Returns:
            (status_code: int, response_dict: dict)
        """
        clean_endpoint = endpoint.lstrip("/")
        if not clean_endpoint.startswith("internal/agent"):
            clean_endpoint = f"internal/agent/{clean_endpoint}"
        url = f"{self.base_url}/{clean_endpoint}"

        headers = {
            "X-Internal-Key": self.internal_key,
            "X-Subject": subject,
            "X-Client": client_id,
            "Accept": "application/json",
        }

        method_upper = method.upper()
        # Retry connect errors once. Never retry non-connect errors on mutating methods (POST/PUT).
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method=method_upper,
                        url=url,
                        headers=headers,
                        json=json_data,
                        params=params,
                    )

                status = resp.status_code
                try:
                    data = resp.json()
                except Exception:
                    data = {"error": resp.text[:200] if resp.text else f"HTTP {status}"}

                # Sanitize response to ensure internal URLs never leak
                return status, self._sanitize_data(data)

            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.warning(
                    "Connect error connecting to internal portal (attempt %d/2): %s",
                    attempt + 1,
                    type(e).__name__,
                )
                if attempt == 0:
                    continue
                return 503, {
                    "ok": False,
                    "error": "The pension service is currently unreachable. Please try again shortly.",
                }
            except httpx.TimeoutException:
                # Timed out during read or write. Do NOT retry POST/PUT.
                logger.warning("Timeout during portal request: %s %s", method_upper, clean_endpoint)
                return 504, {
                    "ok": False,
                    "error": "The pension service took too long to respond. Please try again in a few moments.",
                }
            except Exception as e:
                logger.error("Unexpected portal request error: %s", type(e).__name__)
                return 500, {
                    "ok": False,
                    "error": "An unexpected error occurred while communicating with the pension service.",
                }

        return 503, {
            "ok": False,
            "error": "Unable to connect to the pension portal. Please try again shortly.",
        }

    def _sanitize_data(self, data: Any) -> Any:
        """Strip internal hostnames and URLs from responses."""
        if isinstance(data, dict):
            return {k: self._sanitize_data(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._sanitize_data(item) for item in data]
        elif isinstance(data, str):
            # Replace internal docker URLs with public base if present
            return data.replace("http://app:8000", "").replace("http://localhost:8000", "")
        return data
