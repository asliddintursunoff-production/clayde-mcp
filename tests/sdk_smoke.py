"""Smoke test with the official MCP Python client (python -m tests.sdk_smoke)."""

import asyncio
import os

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    """Connect, list tools, run one search."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"}, timeout=60)
    async with streamable_http_client("http://localhost:8000/mcp", http_client=http) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.server_info.name, "| instructions:", len(init.instructions or ""), "chars")
            tools = await session.list_tools()
            print("tools:", len(tools.tools), sorted(t.name for t in tools.tools)[:5], "...")
            result = await session.call_tool("memory_search", {"query": "backfill window"})
            print("search ok, isError =", result.is_error, "| hits:", str(result.structured_content)[:120])


asyncio.run(main())
