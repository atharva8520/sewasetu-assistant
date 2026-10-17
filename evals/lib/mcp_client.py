# -*- coding: utf-8 -*-
"""
MCP client bridge for Sewa Setu evaluation harness.
Connects to streamable HTTP MCP server and converts tools to Anthropic format.
"""

import json
from typing import Any
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


class EvalMcpClient:
    def __init__(self, endpoint_url: str, access_token: str):
        self.endpoint_url = endpoint_url
        self.access_token = access_token
        self._http_client = None
        self._cm_transport = None
        self._cm_session = None
        self.session: ClientSession | None = None
        self.tools: list[Any] = []

    async def connect(self):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        self._http_client = httpx2.AsyncClient(headers=headers, timeout=15.0)
        self._cm_transport = streamable_http_client(self.endpoint_url, http_client=self._http_client)
        r_stream, w_stream = await self._cm_transport.__aenter__()
        self._cm_session = ClientSession(r_stream, w_stream)
        self.session = await self._cm_session.__aenter__()
        await self.session.initialize()

        tool_list_res = await self.session.list_tools()
        self.tools = tool_list_res.tools

    async def close(self):
        if self._cm_session:
            try:
                await self._cm_session.__aexit__(None, None, None)
            except Exception:
                pass
        if self._cm_transport:
            try:
                await self._cm_transport.__aexit__(None, None, None)
            except Exception:
                pass
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass

    async def call_tool(self, name: str, arguments: dict) -> str:
        if not self.session:
            raise RuntimeError("MCP client is not connected.")
        res = await self.session.call_tool(name, arguments)
        parts = []
        for item in res.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts)

    def get_anthropic_tools(self) -> list[dict]:
        """Convert MCP tools to Anthropic Messages API tool format."""
        anthropic_tools = []
        for t in self.tools:
            schema = t.inputSchema if hasattr(t, "inputSchema") and t.inputSchema else {"type": "object", "properties": {}}
            anthropic_tools.append({
                "name": t.name,
                "description": t.description or "",
                "input_schema": schema,
            })
        return anthropic_tools
