"""Memory store: save, update, archive, hybrid search, task context.

Every write goes to Postgres (index) and S3 (source of truth) in one transaction:
if the S3 write fails, the database change is rolled back.
"""

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import numpy as np
from psycopg_pool import AsyncConnectionPool

from app.embeddings import Embedder
from app.pseudonym import TOKEN_RE
from app.storage import ObjectStore
from app.vault import Vault

MEMORY_KINDS = ("episodic", "semantic", "procedural")
MAX_BODY_CHARS = 20_000
SNIPPET_CHARS = 600
FTS_WEIGHT = 0.5
VECTOR_MAX_DISTANCE = 0.85
DUPLICATE_DISTANCE = 0.08
SIMILAR_DISTANCE = 0.18
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the this to was were with what who how "
    "do does did done get gets got can could should would will must may i we you our my me us any all about "
    "which where when why there their them they its into than then so not no yes if also just "
    "va bu u bilan uchun ham edi nima qanday kim qachon".split()
)

COLUMNS = "id, kind, title, body, ticket, tags, source, importance, occurred_at, updated_at, status, archive_note, superseded_by, revision"


def embedding_text(title: str, tags: list[str], body: str) -> str:
    """Text used for the vector: tokens are replaced by their kind word so meaning survives masking."""
    text = f"{title}\n{' '.join(tags)}\n{body}"
    return TOKEN_RE.sub(lambda m: m.group(0).split("_")[0].lower(), text)


def build_tsquery(masked_query: str) -> str | None:
    """OR-query of the meaningful words (prefix match for longer words); None if nothing is left."""
    words = []
    for word in re.findall(r"\w+", masked_query.lower()):
        for part in word.split("_"):
            if len(part) >= 2 and part not in STOPWORDS and part not in words:
                words.append(part)
    if not words:
        return None
    return " | ".join(f"{w}:*" if len(w) >= 4 else w for w in words[:24])


def fuse_scores(fts: list[tuple[Any, float]], vec: list[tuple[Any, float]]) -> dict[Any, float]:
    """Blend keyword and meaning scores: each is scaled to its best hit (0..1), then weighted.

    Unlike rank fusion this keeps how strong a hit is, which matters with few, short memories.
    fts: (id, ts_rank) pairs. vec: (id, cosine distance) pairs, already cut at the distance limit.
    """
    scores: dict[Any, float] = {}
    best_fts = max((rank for _, rank in fts), default=0) or 1
    for key, rank in fts:
        scores[key] = FTS_WEIGHT * rank / best_fts
    best_sim = max((1 - d for _, d in vec), default=0) or 1
    for key, distance in vec:
        scores[key] = scores.get(key, 0) + (1 - FTS_WEIGHT) * (1 - distance) / best_sim
    return scores


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Lower-case, trimmed, unique tags in input order."""
    seen: list[str] = []
    for tag in tags or []:
        clean = tag.strip().lower()
        if clean and clean not in seen:
            seen.append(clean)
    return seen


def _normalize_ticket(ticket: str | None) -> str | None:
    """Tickets are stored upper-case without spaces."""
    return ticket.strip().upper() if ticket and ticket.strip() else None


def _parse_time(value: str | None) -> datetime | None:
    """Accept ISO dates or datetimes; naive values are treated as UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def render_markdown(row: dict) -> str:
    """S3 file format: JSON front matter between --- lines, then the (masked) body."""
    meta = {
        "id": str(row["id"]), "kind": row["kind"], "title": row["title"], "ticket": row["ticket"],
        "tags": row["tags"], "source": row["source"], "importance": row["importance"],
        "occurred_at": row["occurred_at"].isoformat(), "status": row["status"],
        "archive_note": row.get("archive_note"), "revision": row["revision"],
        "superseded_by": str(row["superseded_by"]) if row.get("superseded_by") else None,
    }
    return f"---\n{json.dumps(meta, ensure_ascii=False)}\n---\n{row['body']}"


def parse_markdown(text: str) -> tuple[dict, str]:
    """Inverse of render_markdown."""
    _, meta, body = text.split("---\n", 2)
    return json.loads(meta), body


class MemoryService:
    """All memory operations used by the MCP tools and the REST API."""

    def __init__(self, pool: AsyncConnectionPool, store: ObjectStore, vault: Vault, embedder: Embedder) -> None:
        self.pool = pool
        self.store = store
        self.vault = vault
        self.embedder = embedder

    # ---- output shaping ------------------------------------------------------------------

    def _out(self, row: dict, reveal: bool, full: bool = True) -> dict[str, Any]:
        """Turn a DB row into tool output; long bodies are cut unless full is requested."""
        body = self.vault.reveal(row["body"]) if reveal else row["body"]
        if not full and len(body) > SNIPPET_CHARS:
            body = body[:SNIPPET_CHARS].rstrip() + " …[truncated, use memory_get]"
        item = {
            "id": str(row["id"]),
            "kind": row["kind"],
            "title": self.vault.reveal(row["title"]) if reveal else row["title"],
            "ticket": row["ticket"],
            "tags": [self.vault.reveal(t) for t in row["tags"]] if reveal else row["tags"],
            "importance": row["importance"],
            "date": row["occurred_at"].date().isoformat(),
            "body": body,
        }
        if row.get("status") == "archived":
            item["status"] = "archived"
            item["archive_note"] = row.get("archive_note")
            if row.get("superseded_by"):
                item["superseded_by"] = str(row["superseded_by"])
        if full:
            item["source"] = row.get("source")
            item["revision"] = row.get("revision")
        if "score" in row:
            item["score"] = round(row["score"], 4)
        return item

    # ---- writes --------------------------------------------------------------------------

    async def save(
        self,
        kind: str,
        title: str,
        body: str,
        ticket: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        importance: int = 3,
        occurred_at: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Save one memory; exact duplicates are not saved twice, near duplicates are reported."""
        kind = kind.strip().lower()
        if kind not in MEMORY_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(MEMORY_KINDS)}")
        if not title.strip() or not body.strip():
            raise ValueError("title and body are required")
        if len(body) > MAX_BODY_CHARS:
            raise ValueError(f"body is {len(body)} chars; keep one memory under {MAX_BODY_CHARS} (split it into several)")
        if not 1 <= importance <= 5:
            raise ValueError("importance must be 1..5")

        # Mask everything that is stored as text
        tags = _normalize_tags(tags)
        m_title, m_body = self.vault.mask(title.strip()), self.vault.mask(body.replace("\x00", "").strip())
        m_tags = [self.vault.mask(t) for t in tags]
        m_source = self.vault.mask(source.strip()) if source else None
        masked = [m_title, m_body, *m_tags] + ([m_source] if m_source else [])
        tag_values = [t.text for t in m_tags]
        content_hash = hashlib.sha256(f"{kind}\n{m_title.text}\n{m_body.text}".encode()).hexdigest()

        # Exact duplicate: return the existing memory instead of a second copy
        async with self.pool.connection() as conn:
            dup = await (await conn.execute(
                "SELECT id FROM memories WHERE content_hash = %s AND status = 'active' LIMIT 1", (content_hash,)
            )).fetchone()
        if dup:
            return {"status": "duplicate", "id": str(dup["id"]), "note": "Identical memory already exists; nothing saved."}

        vector = await self.embedder.embed_one(embedding_text(m_title.text, tag_values, m_body.text))
        similar = await self._similar(vector, kind) if vector is not None else []

        memory_id = uuid.uuid4()
        s3_key = f"memories/{kind}/{memory_id}.md"
        when = _parse_time(occurred_at) or datetime.now(timezone.utc)
        supersedes_id = uuid.UUID(supersedes) if supersedes else None

        async with self.pool.connection() as conn, conn.transaction():
            tokens = await self.vault.persist_tokens(conn, masked)
            row = await (await conn.execute(
                f"""INSERT INTO memories (id, kind, title, body, ticket, tags, source, importance, occurred_at,
                                          content_hash, s3_key, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}""",
                (memory_id, kind, m_title.text, m_body.text, _normalize_ticket(ticket), tag_values,
                 m_source.text if m_source else None, importance, when, content_hash, s3_key, vector),
            )).fetchone()

            # Replacing an older memory archives it and points it to the new one
            if supersedes_id:
                old = await (await conn.execute(
                    f"""UPDATE memories SET status = 'archived', superseded_by = %s, archive_note = 'superseded',
                               updated_at = now() WHERE id = %s RETURNING {COLUMNS}""",
                    (memory_id, supersedes_id),
                )).fetchone()
                if old is None:
                    raise ValueError(f"supersedes: memory {supersedes} not found")
                await self.store.put_text(f"memories/{old['kind']}/{old['id']}.md", render_markdown(old))

            await self.store.put_text(s3_key, render_markdown(row))

        self.vault.remember(tokens)
        result: dict[str, Any] = {"status": "saved", "id": str(memory_id)}
        redactions = sum((m.redactions for m in masked), start=Counter())
        if redactions:
            result["secrets_removed"] = dict(redactions)
        if similar:
            result["similar_existing"] = similar
            result["hint"] = "Similar memories exist. If one is outdated, call memory_archive or save with supersedes."
        return result

    async def _similar(self, vector: np.ndarray, kind: str, exclude: uuid.UUID | None = None) -> list[dict]:
        """Active memories of the same kind that are very close in meaning."""
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                """SELECT id, title, embedding <=> %s::vector AS distance FROM memories
                   WHERE kind = %s AND status = 'active' AND embedding IS NOT NULL AND id IS DISTINCT FROM %s::uuid
                   ORDER BY embedding <=> %s::vector LIMIT 3""",
                (vector, kind, exclude, vector),
            )).fetchall()
        return [
            {"id": str(r["id"]), "title": self.vault.reveal(r["title"]),
             "match": "near_duplicate" if r["distance"] < DUPLICATE_DISTANCE else "similar"}
            for r in rows if r["distance"] < SIMILAR_DISTANCE
        ]

    async def update(
        self,
        memory_id: str,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        """Edit a memory in place; the previous version is kept in memory_revisions."""
        if importance is not None and not 1 <= importance <= 5:
            raise ValueError("importance must be 1..5")
        if body is not None and len(body) > MAX_BODY_CHARS:
            raise ValueError(f"body is too long (max {MAX_BODY_CHARS} chars)")

        async with self.pool.connection() as conn, conn.transaction():
            current = await (await conn.execute(
                f"SELECT {COLUMNS} FROM memories WHERE id = %s FOR UPDATE", (uuid.UUID(memory_id),)
            )).fetchone()
            if current is None:
                raise ValueError(f"memory {memory_id} not found")

            # Re-mask whatever changed (unchanged fields keep their stored, already masked text)
            masked = []
            new_title, new_body, new_tags = current["title"], current["body"], current["tags"]
            if title is not None:
                masked.append(self.vault.mask(title.strip()))
                new_title = masked[-1].text
            if body is not None:
                masked.append(self.vault.mask(body.strip()))
                new_body = masked[-1].text
            if tags is not None:
                tag_results = [self.vault.mask(t) for t in _normalize_tags(tags)]
                masked.extend(tag_results)
                new_tags = [t.text for t in tag_results]

            vector = await self.embedder.embed_one(embedding_text(new_title, new_tags, new_body))
            tokens = await self.vault.persist_tokens(conn, masked)
            await conn.execute(
                "INSERT INTO memory_revisions (memory_id, revision, title, body, tags) VALUES (%s, %s, %s, %s, %s)",
                (current["id"], current["revision"], current["title"], current["body"], current["tags"]),
            )
            row = await (await conn.execute(
                f"""UPDATE memories SET title = %s, body = %s, tags = %s, importance = %s, ticket = %s,
                           embedding = COALESCE(%s, embedding), revision = revision + 1, updated_at = now(),
                           content_hash = %s
                    WHERE id = %s RETURNING {COLUMNS}""",
                (new_title, new_body, new_tags, importance or current["importance"],
                 _normalize_ticket(ticket) if ticket is not None else current["ticket"], vector,
                 hashlib.sha256(f"{current['kind']}\n{new_title}\n{new_body}".encode()).hexdigest(), current["id"]),
            )).fetchone()
            await self.store.put_text(f"memories/{row['kind']}/{row['id']}.md", render_markdown(row))

        self.vault.remember(tokens)
        return {"status": "updated", "id": memory_id, "revision": row["revision"]}

    async def archive(self, memory_id: str, note: str) -> dict[str, Any]:
        """Hide a memory from normal search (wrong, outdated or merged); it is not deleted."""
        masked_note = self.vault.mask(note.strip() or "archived")
        async with self.pool.connection() as conn, conn.transaction():
            tokens = await self.vault.persist_tokens(conn, [masked_note])
            row = await (await conn.execute(
                f"""UPDATE memories SET status = 'archived', archive_note = %s, updated_at = now()
                    WHERE id = %s RETURNING {COLUMNS}""",
                (masked_note.text, uuid.UUID(memory_id)),
            )).fetchone()
            if row is None:
                raise ValueError(f"memory {memory_id} not found")
            await self.store.put_text(f"memories/{row['kind']}/{row['id']}.md", render_markdown(row))
        self.vault.remember(tokens)
        return {"status": "archived", "id": memory_id}

    # ---- reads ---------------------------------------------------------------------------

    async def get(self, memory_ids: list[str], reveal: bool = True) -> list[dict[str, Any]]:
        """Full memories by id (archived ones too)."""
        ids = [uuid.UUID(i) for i in memory_ids]
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(f"SELECT {COLUMNS} FROM memories WHERE id = ANY(%s)", (ids,))).fetchall()
        found = {str(r["id"]): r for r in rows}
        return [self._out(found[i], reveal) if i in found else {"id": i, "error": "not found"} for i in memory_ids]

    async def recent(
        self, kind: str | None = None, ticket: str | None = None, limit: int = 20, offset: int = 0, reveal: bool = True
    ) -> list[dict[str, Any]]:
        """Newest active memories, optionally filtered."""
        where, params = self._filters(kind, _normalize_ticket(ticket), None, None, False)
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                f"SELECT {COLUMNS} FROM memories WHERE {where} ORDER BY occurred_at DESC LIMIT %s OFFSET %s",
                (*params, min(limit, 100), offset),
            )).fetchall()
        return [self._out(r, reveal, full=False) for r in rows]

    def _filters(
        self, kind: str | None, ticket: str | None, tags: list[str] | None, since: str | None, include_archived: bool
    ) -> tuple[str, list[Any]]:
        """SQL WHERE clause and parameters shared by search and listing."""
        clauses, params = ["TRUE"], []
        if not include_archived:
            clauses.append("status = 'active'")
        if kind:
            if kind not in MEMORY_KINDS:
                raise ValueError(f"kind must be one of: {', '.join(MEMORY_KINDS)}")
            clauses.append("kind = %s")
            params.append(kind)
        if ticket:
            clauses.append("ticket = %s")
            params.append(ticket)
        if tags:
            clauses.append("tags && %s")
            params.append([self.vault.mask(t).text for t in _normalize_tags(tags)])
        if since:
            clauses.append("occurred_at >= %s")
            params.append(_parse_time(since))
        return " AND ".join(clauses), params

    async def search(
        self,
        query: str,
        kind: str | None = None,
        ticket: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        limit: int = 8,
        include_archived: bool = False,
        exclude_ticket: str | None = None,
        reveal: bool = True,
    ) -> list[dict[str, Any]]:
        """Hybrid search: full-text and vector results fused with reciprocal rank fusion."""
        limit = max(1, min(limit, 50))
        masked_query = self.vault.mask(query).text
        tsquery = build_tsquery(masked_query)
        vector = await self.embedder.embed_one(embedding_text("", [], masked_query))
        where, params = self._filters(kind, _normalize_ticket(ticket), tags, since, include_archived)
        if exclude_ticket:
            where += " AND ticket IS DISTINCT FROM %s"
            params.append(_normalize_ticket(exclude_ticket))
        pool_size = max(limit * 4, 20)

        # Keyword and meaning candidates, then blended scores
        fts: list[tuple[uuid.UUID, float]] = []
        vec: list[tuple[uuid.UUID, float]] = []
        async with self.pool.connection() as conn:
            if tsquery:
                rows = await (await conn.execute(
                    f"""SELECT id, ts_rank_cd(tsv, to_tsquery('simple', %s)) AS rank FROM memories
                        WHERE {where} AND tsv @@ to_tsquery('simple', %s) ORDER BY rank DESC LIMIT %s""",
                    (tsquery, *params, tsquery, pool_size),
                )).fetchall()
                fts = [(r["id"], r["rank"]) for r in rows]
            if vector is not None:
                rows = await (await conn.execute(
                    f"""SELECT id, embedding <=> %s::vector AS distance FROM memories WHERE {where} AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector LIMIT %s""",
                    (vector, *params, vector, pool_size),
                )).fetchall()
                # Far-away neighbours are noise, not matches
                vec = [(r["id"], r["distance"]) for r in rows if r["distance"] < VECTOR_MAX_DISTANCE]

            scores = fuse_scores(fts, vec)
            if not scores:
                return []
            rows = await (await conn.execute(
                f"SELECT {COLUMNS} FROM memories WHERE id = ANY(%s)", (list(scores),)
            )).fetchall()

        # Importance only breaks near-ties
        for r in rows:
            r["score"] = scores[r["id"]] * (1 + 0.02 * (r["importance"] - 3))
        rows.sort(key=lambda r: r["score"], reverse=True)
        return [self._out(r, reveal, full=False) for r in rows[:limit]]

    async def task_context(self, ticket: str, query: str | None = None, reveal: bool = True) -> dict[str, Any]:
        """Everything worth knowing before working on a ticket: its history, rules, related past work."""
        ticket = _normalize_ticket(ticket)
        async with self.pool.connection() as conn:
            history = await (await conn.execute(
                f"SELECT {COLUMNS} FROM memories WHERE ticket = %s AND status = 'active' ORDER BY occurred_at LIMIT 60",
                (ticket,),
            )).fetchall()
            rules = await (await conn.execute(
                f"""SELECT {COLUMNS} FROM memories WHERE kind = 'procedural' AND status = 'active' AND importance >= 4
                    ORDER BY importance DESC, updated_at DESC LIMIT 15"""
            )).fetchall()

        # Related work from other tickets, found by the ticket's own titles (or the given query)
        probe = query or " ".join(self.vault.reveal(r["title"]) for r in history[-10:]) or ticket
        related = await self.search(probe, limit=6, exclude_ticket=ticket, reveal=reveal)
        rule_ids = {str(r["id"]) for r in rules}
        relevant_rules = [r for r in await self.search(probe, kind="procedural", limit=5, reveal=reveal) if r["id"] not in rule_ids]

        return {
            "ticket": ticket,
            "history": [self._out(r, reveal, full=False) for r in history],
            "standing_rules": [self._out(r, reveal, full=False) for r in rules] + relevant_rules,
            "related_from_other_tickets": [r for r in related if r["kind"] != "procedural"],
            "note": "No memories for this ticket yet." if not history else None,
        }

    async def stats(self) -> dict[str, Any]:
        """Counts by kind and status."""
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT kind, status, count(*) AS n, max(updated_at) AS last FROM memories GROUP BY kind, status ORDER BY 1, 2"
            )).fetchall()
            tickets = await (await conn.execute(
                "SELECT ticket, count(*) AS n, max(occurred_at) AS last FROM memories WHERE ticket IS NOT NULL AND status = 'active' "
                "GROUP BY ticket ORDER BY max(occurred_at) DESC LIMIT 30"
            )).fetchall()
        return {
            "by_kind": [{"kind": r["kind"], "status": r["status"], "count": r["n"], "last_update": r["last"].isoformat()} for r in rows],
            "recent_tickets": [{"ticket": r["ticket"], "memories": r["n"], "last": r["last"].date().isoformat()} for r in tickets],
            "embeddings": self.embedder.ready,
        }

    async def remask(self, value: str, match_mode: str) -> int:
        """Re-mask stored memories that still hold a term in plain text (saved before the term was added)."""
        if match_mode == "regex":
            condition, param = "(title || ' ' || body || ' ' || array_to_string(tags, ' ')) ~ %s", value
        else:
            escaped = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            condition, param = "(title || ' ' || body || ' ' || array_to_string(tags, ' ')) ILIKE %s", f"%{escaped}%"

        async with self.pool.connection() as conn:
            rows = await (await conn.execute(f"SELECT {COLUMNS} FROM memories WHERE {condition}", (param,))).fetchall()
        return sum([await self._remask_row(row) for row in rows])

    async def renormalize_all(self) -> int:
        """Re-mask every memory with the current dictionary (also un-masks words of removed terms)."""
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(f"SELECT {COLUMNS} FROM memories")).fetchall()
        return sum([await self._remask_row(row) for row in rows])

    async def _remask_row(self, row: dict) -> int:
        """Reveal and mask one memory again; store it only if the masked text changed. Returns 1 if changed."""
        m_title = self.vault.mask(self.vault.reveal(row["title"]))
        m_body = self.vault.mask(self.vault.reveal(row["body"]))
        m_tags = [self.vault.mask(self.vault.reveal(t)) for t in row["tags"]]
        if m_title.text == row["title"] and m_body.text == row["body"] and [t.text for t in m_tags] == row["tags"]:
            return 0

        vector = await self.embedder.embed_one(embedding_text(m_title.text, [t.text for t in m_tags], m_body.text))
        async with self.pool.connection() as conn, conn.transaction():
            tokens = await self.vault.persist_tokens(conn, [m_title, m_body, *m_tags])
            updated = await (await conn.execute(
                f"""UPDATE memories SET title = %s, body = %s, tags = %s, embedding = COALESCE(%s, embedding),
                           content_hash = %s WHERE id = %s RETURNING {COLUMNS}""",
                (m_title.text, m_body.text, [t.text for t in m_tags], vector,
                 hashlib.sha256(f"{row['kind']}\n{m_title.text}\n{m_body.text}".encode()).hexdigest(), row["id"]),
            )).fetchone()
            await self.store.put_text(f"memories/{updated['kind']}/{updated['id']}.md", render_markdown(updated))
        self.vault.remember(tokens)
        return 1

    # ---- recovery ------------------------------------------------------------------------

    async def restore_from_store(self) -> int:
        """Rebuild the memories table from S3 files (after the database was lost)."""
        restored = 0
        for key in await self.store.list_keys("memories/"):
            meta, body = parse_markdown(await self.store.get_text(key))
            vector = await self.embedder.embed_one(embedding_text(meta["title"], meta["tags"], body))
            content_hash = hashlib.sha256(f"{meta['kind']}\n{meta['title']}\n{body}".encode()).hexdigest()
            async with self.pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO memories (id, kind, title, body, ticket, tags, source, importance, occurred_at, status,
                                             archive_note, revision, content_hash, s3_key, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (uuid.UUID(meta["id"]), meta["kind"], meta["title"], body, meta["ticket"], meta["tags"], meta["source"],
                     meta["importance"], _parse_time(meta["occurred_at"]), meta["status"], meta.get("archive_note"),
                     meta["revision"], content_hash, key, vector),
                )
            restored += 1

        # superseded_by links are set after all rows exist
        for key in await self.store.list_keys("memories/"):
            meta, _ = parse_markdown(await self.store.get_text(key))
            if meta.get("superseded_by"):
                async with self.pool.connection() as conn:
                    await conn.execute("UPDATE memories SET superseded_by = %s WHERE id = %s",
                                       (uuid.UUID(meta["superseded_by"]), uuid.UUID(meta["id"])))
        return restored
