"""Async MCP stdio client wrapper.

Spawns the existing `polymarket` MCP server as a subprocess and exposes
the call_tool() interface. Used by the bot runner to fetch market data
and place orders.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(_REPO_ROOT, "venv", "bin", "python")
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))


@asynccontextmanager
async def mcp_session():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_VENV_PY,
        args=["-m", "polymarket_mcp.server"],
        env={**os.environ, "POLYMARKET_ENV_FILE":
             os.path.join(_REPO_ROOT, ".env")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(name: str, arguments: Optional[dict] = None) -> Any:
    async with mcp_session() as session:
        result = await session.call_tool(name, arguments or {})
        if hasattr(result, "content") and result.content:
            text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result
