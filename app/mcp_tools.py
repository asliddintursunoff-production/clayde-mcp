"""MCP tools exposed to AI clients (Claude Code, the Telegram bot)."""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.state import services

INSTRUCTIONS = """\
Long-term work memory shared by every Claude session and the Telegram bot.

When to use it
- Start of any task or question about a ticket: call task_context(ticket) first, and memory_search for people,
  tables or topics involved. Say what you found ("Earlier we decided ...") before asking the user anything that
  may already be answered.
- Write memories, titles and search queries in English (vector search understands English best); keep names,
  table and column names exactly as they are.
- Save as you go, not only at the end: decisions, answers from people, requirements, numbers that were validated,
  mistakes and their fix. One memory = one fact/event, short and self-contained (who, what, when, why).
- End of a task: close_task(ticket, summary, lessons, decisions).

Kinds
- episodic: something that happened (meeting, message, run, review comment), with occurred_at.
- semantic: a durable fact or decision (how a table works, who owns what, an agreed design).
- procedural: a rule or lesson for future work ("validate in dev before a PR"). importance 4-5 = always shown.

Keep it correct
- If a memory is outdated, save the new one with supersedes=<old id> (the old one is archived, not deleted).
- memory_save reports similar_existing; update or supersede instead of piling up near duplicates.

Privacy
- Stored text is masked: secrets are removed for good, names/company words/emails become tokens. Results are
  shown with real values. When a new person, company or sensitive id appears, call pseudo_add_term BEFORE saving.
- After importing notes or every few sessions, run pseudo_suggest_terms and add the real names it finds.
- Never save passwords, keys or tokens; they are stripped anyway.

Architecture
- Code is not embedded; it is found by name with common_grep / common_search. Meaning lives in memories:
  after you work out how something fits together (which notebook feeds which table, keys, schedule, why it is
  built that way), save a semantic memory with tag "architecture" plus the object names as tags. Next time it is
  found by meaning, and common_grep takes you to the code.

Common (read-only snapshot of shared repos/code)
- common_list / common_search / common_grep / common_read. It is a snapshot uploaded by the admin; never run
  git pull, commit or push on it or because of it. If it looks outdated, tell the user to re-upload.
"""

mcp = MCPServer(name="work-memory", instructions=INSTRUCTIONS, version="1.0.0")

def readable_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Expected errors (bad input, not found) reach the model as text instead of a generic failure."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, LookupError) as error:
            raise ToolError(str(error)) from error

    return wrapper


Kind = Literal["episodic", "semantic", "procedural"]
TermKind = Literal["PERSON", "ORG", "EMAIL", "ID", "PLACE", "PROJECT", "TERM"]


# ---- memory ------------------------------------------------------------------------------

@mcp.tool()
@readable_errors
async def memory_search(
    query: Annotated[str, Field(description="Natural language or keywords; names and table names work.")],
    kind: Annotated[Kind | None, Field(description="Only this kind.")] = None,
    ticket: Annotated[str | None, Field(description="Only memories of this ticket, e.g. TICKET-123.")] = None,
    tags: Annotated[list[str] | None, Field(description="Any of these tags.")] = None,
    since: Annotated[str | None, Field(description="ISO date; only memories that happened on/after it.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 8,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Find memories by meaning and keywords (hybrid search). Bodies are cut to 600 chars; use memory_get for full text."""
    return await services().memories.search(query, kind, ticket, tags, since, limit, include_archived)


@mcp.tool()
@readable_errors
async def memory_save(
    kind: Kind,
    title: Annotated[str, Field(description="One line that says what this is, searchable on its own.")],
    body: Annotated[str, Field(description="The fact/event/rule. Who, what, when, why. Under ~2000 chars is best.")],
    ticket: Annotated[str | None, Field(description="Ticket number if it belongs to one.")] = None,
    tags: Annotated[list[str] | None, Field(description="Topics: table names, systems, people roles.")] = None,
    source: Annotated[str | None, Field(description="Where it came from: 'Teams, 2026-11-05', 'PR #12 review'.")] = None,
    importance: Annotated[int, Field(ge=1, le=5, description="3 normal; 4-5 must not be forgotten.")] = 3,
    occurred_at: Annotated[str | None, Field(description="ISO date/time when it happened (default now).")] = None,
    supersedes: Annotated[str | None, Field(description="Id of an older memory this one replaces.")] = None,
) -> dict[str, Any]:
    """Save one memory. Returns its id, removed secrets, and similar existing memories to merge with."""
    return await services().memories.save(kind, title, body, ticket, tags, source, importance, occurred_at, supersedes)


@mcp.tool()
@readable_errors
async def memory_get(ids: Annotated[list[str], Field(min_length=1, max_length=20)]) -> list[dict[str, Any]]:
    """Full text of memories by id (archived ones included)."""
    return await services().memories.get(ids)


@mcp.tool()
@readable_errors
async def memory_update(
    id: str,
    title: str | None = None,
    body: Annotated[str | None, Field(description="Replaces the whole body.")] = None,
    tags: Annotated[list[str] | None, Field(description="Replaces all tags.")] = None,
    importance: Annotated[int | None, Field(ge=1, le=5)] = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Correct or extend a memory in place (previous version is kept). For a changed decision prefer memory_save with supersedes."""
    return await services().memories.update(id, title, body, tags, importance, ticket)


@mcp.tool()
@readable_errors
async def memory_archive(id: str, reason: Annotated[str, Field(description="Why: wrong, outdated, merged into <id>.")]) -> dict[str, Any]:
    """Hide a wrong or outdated memory from search. Nothing is deleted."""
    return await services().memories.archive(id, reason)


@mcp.tool()
@readable_errors
async def memory_recent(
    kind: Kind | None = None,
    ticket: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> list[dict[str, Any]]:
    """Newest memories first, optionally for one ticket or kind."""
    return await services().memories.recent(kind, ticket, limit, offset)


@mcp.tool()
@readable_errors
async def task_context(
    ticket: str,
    query: Annotated[str | None, Field(description="What the task is about, to find related past work.")] = None,
) -> dict[str, Any]:
    """Call at the start of work on a ticket: its history, standing rules, and related memories from other tickets."""
    svc = services()
    context = await svc.memories.task_context(ticket, query)
    context["notes_in_common"] = await svc.common.ticket_files(context["ticket"])
    if context["notes_in_common"] and not context["history"]:
        context["note"] = "No memories yet, but the common snapshot has notes for this ticket: read them, then save the key facts as memories."
    return context


@mcp.tool()
@readable_errors
async def close_task(
    ticket: str,
    summary: Annotated[str, Field(description="What was done, final state, where things are.")],
    lessons: Annotated[list[str] | None, Field(description="Each a rule for next time (saved as procedural).")] = None,
    decisions: Annotated[list[str] | None, Field(description="Each a decision with who/why (saved as semantic).")] = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap up a ticket: saves the summary, each lesson and each decision as separate memories."""
    mem = services().memories
    saved = {"summary": await mem.save("episodic", f"{ticket.upper()} closed: summary", summary, ticket, tags, "close_task", 3)}
    saved["lessons"] = [await mem.save("procedural", f"Lesson ({ticket.upper()}): {text[:80]}", text, ticket, tags, "close_task", 4)
                        for text in lessons or []]
    saved["decisions"] = [await mem.save("semantic", f"Decision ({ticket.upper()}): {text[:80]}", text, ticket, tags, "close_task", 4)
                          for text in decisions or []]
    return saved


@mcp.tool()
@readable_errors
async def memory_stats() -> dict[str, Any]:
    """Counts by kind, recently active tickets, whether vector search is on."""
    return await services().memories.stats()


# ---- pseudonymization ----------------------------------------------------------------------

@mcp.tool()
@readable_errors
async def pseudo_add_term(
    kind: TermKind,
    value: Annotated[str, Field(description="Exact word/name/id to mask, e.g. a person's name or company word.")],
    match_mode: Annotated[Literal["word", "substring", "regex"], Field(
        description="word: whole word (names). substring: also inside other words (company name in identifiers). regex: pattern for ids.")] = "word",
    case_insensitive: bool = True,
) -> dict[str, Any]:
    """Add a sensitive term; it is masked from now on and in memories already stored."""
    svc = services()
    result = await svc.vault.add_term(kind, value, match_mode, case_insensitive)
    if result["status"] == "added":
        result["memories_remasked"] = await svc.memories.remask(result["value"], match_mode)
        result["common_files_remasked"] = await svc.common.remask(result["value"], match_mode)
    return result


@mcp.tool()
@readable_errors
async def pseudo_list_terms() -> list[dict[str, Any]]:
    """Terms currently masked."""
    return services().vault.list_terms()


# ---- common snapshot (read-only) ---------------------------------------------------------

@mcp.tool()
@readable_errors
async def common_list(
    collection: Annotated[str | None, Field(description="Empty = list collections.")] = None,
    prefix: Annotated[str | None, Field(description="Path prefix, e.g. 'notebook/clean_'.")] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """List collections in the common snapshot, or files in one collection."""
    return await services().common.list_files(collection, prefix, limit)


@mcp.tool()
@readable_errors
async def common_search(
    query: str,
    collection: str | None = None,
    path_prefix: str | None = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> list[dict[str, Any]]:
    """Keyword search over the whole snapshot, plus meaning search over docs (.md/.txt); returns file, line range, preview."""
    return await services().common.search(query, collection, path_prefix, limit)


@mcp.tool()
@readable_errors
async def common_grep(
    pattern: str,
    collection: str | None = None,
    path_prefix: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Exact text (or regex) search line by line, like grep. Best for identifiers: table, column, function names."""
    return await services().common.grep(pattern, collection, path_prefix, regex, case_sensitive, max_results)


@mcp.tool()
@readable_errors
async def common_read(
    collection: str,
    path: str,
    start_line: Annotated[int, Field(ge=1)] = 1,
    max_lines: Annotated[int, Field(ge=1, le=1000)] = 300,
) -> dict[str, Any]:
    """Read numbered lines of a file in the common snapshot (notebooks appear as rendered code cells)."""
    return await services().common.read(collection, path, start_line, max_lines)


@mcp.tool()
@readable_errors
async def pseudo_suggest_terms(limit: Annotated[int, Field(ge=1, le=100)] = 40) -> list[dict[str, Any]]:
    """Name-like words stored unmasked (from memories and doc notes). Review them; add real people/companies with pseudo_add_term."""
    from app.privacy import suggest_names

    svc = services()
    known = {t.value.lower() for t in svc.vault.pseudo.terms}
    return await suggest_names(svc.pool, known, limit)
