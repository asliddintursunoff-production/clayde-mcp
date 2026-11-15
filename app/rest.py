"""Read-only REST API for people: browse what the memory server holds (same data the MCP tools use).

Open /docs, click Authorize, paste the MCP token (or admin token).
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer

from app.mcp_tools import mcp
from app.state import services

# The token itself is checked by the BearerAuth middleware; this only adds the Authorize button to /docs
bearer = HTTPBearer(description="MCP token (read) or admin token (read + admin)")
router = APIRouter(prefix="/api", dependencies=[Depends(bearer)])

Kind = Literal["episodic", "semantic", "procedural"]


def _bad_request(error: ValueError) -> HTTPException:
    """Turn an expected input error into a 400 with its message."""
    return HTTPException(400, str(error))


@router.get("/stats", tags=["memories"], summary="Counts by kind, recent tickets, vector search status")
async def stats() -> dict[str, Any]:
    return await services().memories.stats()


@router.get("/memories", tags=["memories"], summary="Newest memories (optionally one ticket or kind)")
async def recent(
    kind: Kind | None = None,
    ticket: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return await services().memories.recent(kind, ticket, limit, offset)


@router.get("/memories/search", tags=["memories"], summary="Hybrid search (keywords + meaning)")
async def search(
    q: str,
    kind: Kind | None = None,
    ticket: str | None = None,
    limit: int = Query(8, ge=1, le=50),
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    try:
        return await services().memories.search(q, kind, ticket, None, None, limit, include_archived)
    except ValueError as error:
        raise _bad_request(error) from error


@router.get("/memories/{memory_id}", tags=["memories"], summary="One memory, full text")
async def get_memory(memory_id: str) -> dict[str, Any]:
    try:
        item = (await services().memories.get([memory_id]))[0]
    except ValueError as error:
        raise _bad_request(error) from error
    if item.get("error"):
        raise HTTPException(404, "memory not found")
    return item


@router.get("/tickets/{ticket}", tags=["memories"], summary="Task context: history, standing rules, related work, notes")
async def ticket_context(ticket: str, query: str | None = None) -> dict[str, Any]:
    svc = services()
    context = await svc.memories.task_context(ticket, query)
    context["notes_in_common"] = await svc.common.ticket_files(context["ticket"])
    return context


@router.get("/common", tags=["common"], summary="Collections, or files of one collection")
async def common_list(
    collection: str | None = None, prefix: str | None = None, limit: int = Query(200, ge=1, le=1000)
) -> dict[str, Any]:
    return await services().common.list_files(collection, prefix, limit)


@router.get("/common/read", tags=["common"], summary="Numbered lines of one file")
async def common_read(
    collection: str, path: str, start_line: int = Query(1, ge=1), max_lines: int = Query(300, ge=1, le=1000)
) -> dict[str, Any]:
    try:
        return await services().common.read(collection, path, start_line, max_lines)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/common/search", tags=["common"], summary="Keyword search (all files) + meaning search (docs)")
async def common_search(
    q: str, collection: str | None = None, path_prefix: str | None = None, limit: int = Query(10, ge=1, le=30)
) -> list[dict[str, Any]]:
    return await services().common.search(q, collection, path_prefix, limit)


@router.get("/common/grep", tags=["common"], summary="Exact text or regex, line by line")
async def common_grep(
    pattern: str,
    collection: str | None = None,
    path_prefix: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return await services().common.grep(pattern, collection, path_prefix, regex, case_sensitive, max_results)
    except ValueError as error:
        raise _bad_request(error) from error


@router.get("/terms", tags=["privacy"], summary="Masked terms (names, company words, id patterns)")
async def terms() -> list[dict[str, Any]]:
    return services().vault.list_terms()


@router.get("/tools", tags=["mcp"], summary="MCP tools the AI sees, with descriptions and parameters")
async def tools() -> dict[str, Any]:
    listed = await mcp.list_tools()
    return {
        "endpoint": "/mcp (streamable HTTP, Bearer token)",
        "instructions": mcp.instructions,
        "tools": [{"name": t.name, "description": t.description, "parameters": t.input_schema} for t in listed],
    }
