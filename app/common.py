"""Read-only "common" snapshot: shared repos and code, uploaded by an admin and searched by the AI.

Rules: the server never runs git (no clone, pull, commit, push). A snapshot is a tar.gz uploaded by an
admin; secrets are removed and sensitive words masked before anything is stored. The AI can only read.
"""

import hashlib
import io
import json
import re
import tarfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.embeddings import Embedder
from app.memories import build_tsquery, embedding_text, fuse_scores
from app.storage import ObjectStore
from app.vault import Vault

CHUNK_LINES = 60
VECTOR_MAX_DISTANCE = 0.7

# Only prose carries meaning worth a vector (design notes, READMEs). Code is found by name: full-text and grep.
MEANING_EXT = (".md", ".markdown", ".txt", ".rst", ".adoc")


def wants_vector(path: str) -> bool:
    """True for documentation files; code, SQL, JSON and notebooks are not embedded."""
    return path.lower().endswith(MEANING_EXT)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build", ".pytest_cache"}
SKIP_NAMES = re.compile(r"(?i)(^\.env(\..*)?$|\.(pem|key|pfx|p12|crt|cer|jks|keystore)$|^id_(rsa|ed25519|ecdsa)|credentials|secrets?\.(json|ya?ml|txt)$)")
BINARY_EXT = re.compile(r"(?i)\.(png|jpe?g|gif|bmp|ico|pdf|zip|gz|tar|7z|exe|dll|so|parquet|xlsx?|docx?|pptx?|pbix|whl|jar|class|pyc|db|sqlite)$")
COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def render_notebook(path: str, raw: str) -> str | None:
    """Synapse notebook JSON (properties.cells) or Jupyter .ipynb (cells) as readable code; None if not one."""
    if not path.lower().endswith((".json", ".ipynb")):
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    cells = doc.get("cells") or (doc.get("properties") or {}).get("cells")
    if not isinstance(cells, list) or not cells:
        return None

    lines = [f"# notebook: {doc.get('name') or path} ({len(cells)} cells; rendered from JSON, outputs dropped)"]
    for number, cell in enumerate(cells, start=1):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        lines.append(f"\n# ---- cell {number} [{cell.get('cell_type', 'code')}] ----")
        lines.append(text.rstrip())
    return "\n".join(lines) + "\n"


def decode_text(raw: bytes) -> str | None:
    """Text from bytes: UTF-8 or UTF-16 (BOM); None for binary. Stray NUL bytes are dropped (Postgres rejects them)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw[:8192]:
        return None
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\x00", "")


def split_chunks(text: str) -> list[tuple[int, int, str]]:
    """Fixed line chunks: (start_line, end_line, text), 1-based and inclusive."""
    lines = text.splitlines()
    return [
        (start + 1, min(start + CHUNK_LINES, len(lines)), "\n".join(lines[start:start + CHUNK_LINES]))
        for start in range(0, len(lines), CHUNK_LINES)
    ]


class CommonService:
    """Upload (admin) and read/search (AI) of the common snapshot."""

    def __init__(self, pool: AsyncConnectionPool, store: ObjectStore, vault: Vault, embedder: Embedder, max_file_kb: int) -> None:
        self.pool = pool
        self.store = store
        self.vault = vault
        self.embedder = embedder
        self.max_file_bytes = max_file_kb * 1024

    # ---- upload --------------------------------------------------------------------------

    def _read_archive(self, data: bytes) -> tuple[dict[str, str], Counter]:
        """Text files from the archive, with skip reasons counted (never extracted to disk)."""
        files: dict[str, str] = {}
        skipped: Counter = Counter()

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                path = member.name
                while path.startswith("./"):
                    path = path[2:]
                path = path.lstrip("/")
                parts = path.split("/")
                if ".." in parts or not path:
                    skipped["unsafe_path"] += 1
                elif any(p in SKIP_DIRS for p in parts[:-1]):
                    skipped["ignored_dir"] += 1
                elif SKIP_NAMES.search(parts[-1]):
                    skipped["secret_like_file"] += 1
                elif BINARY_EXT.search(path):
                    skipped["binary"] += 1
                elif member.size > self.max_file_bytes:
                    skipped["too_large"] += 1
                else:
                    text = decode_text(archive.extractfile(member).read())
                    if text is None:
                        skipped["binary"] += 1
                        continue
                    files[path] = text
        return files, skipped

    async def upload(self, collection: str, data: bytes) -> dict[str, Any]:
        """Replace a collection with the archive contents; unchanged files are skipped, missing ones removed."""
        collection = collection.strip().lower()
        if not COLLECTION_RE.match(collection):
            raise ValueError("collection: 2-63 chars, lower-case letters, digits, . _ -")

        files, skipped = self._read_archive(data)
        counts: Counter = Counter()
        secrets: Counter = Counter()
        failed: list[dict[str, str]] = []

        async with self.pool.connection() as conn:
            existing = {r["path"]: r for r in await (await conn.execute(
                "SELECT id, path, sha256, s3_key FROM common_files WHERE collection = %s", (collection,)
            )).fetchall()}

        seen_paths: set[str] = set()
        for path, raw in sorted(files.items()):
            stored_path = self.vault.mask(path).text
            seen_paths.add(stored_path)

            # One bad file is reported, it does not stop the rest
            try:
                outcome, found = await self._store_file(collection, path, raw, existing.get(stored_path))
            except Exception as error:
                failed.append({"path": path, "error": str(error)[:200]})
                continue
            counts[outcome] += 1
            secrets.update(found)

        # Files that are no longer in the snapshot
        removed = [r for p, r in existing.items() if p not in seen_paths]
        if removed:
            async with self.pool.connection() as conn:
                await conn.execute("DELETE FROM common_files WHERE id = ANY(%s)", ([r["id"] for r in removed],))
            await self.store.delete([r["s3_key"] for r in removed])
            counts["removed"] = len(removed)

        result = {"collection": collection, "files": len(files), **counts, "skipped": dict(skipped), "secrets_removed": dict(secrets)}
        if failed:
            result["failed"] = failed
        return result

    async def _store_file(self, collection: str, path: str, raw: str, old: dict | None) -> tuple[str, Counter]:
        """Mask and store one file (S3 + index); returns ('added'|'updated'|'unchanged', secrets removed)."""
        m_path = self.vault.mask(path)
        m_body = self.vault.mask(render_notebook(path, raw) or raw)
        stored_path = m_path.text
        digest = hashlib.sha256(m_body.text.encode()).hexdigest()
        if old and old["sha256"] == digest:
            return "unchanged", m_body.redactions

        chunks = split_chunks(m_body.text)
        vectors = None
        if wants_vector(stored_path):
            vectors = await self.embedder.embed([embedding_text(stored_path, [], c[2]) for c in chunks])
        vectors = vectors or [None] * len(chunks)
        s3_key = f"common/{collection}/{stored_path}"

        async with self.pool.connection() as conn, conn.transaction():
            tokens = await self.vault.persist_tokens(conn, [m_path, m_body])
            # A path that changed its masked form (new term) replaces the old row
            if old and old["path"] != stored_path:
                await conn.execute("DELETE FROM common_files WHERE id = %s", (old["id"],))
            row = await (await conn.execute(
                """INSERT INTO common_files (collection, path, sha256, size_bytes, line_count, s3_key, uploaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (collection, path) DO UPDATE SET sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes,
                       line_count = EXCLUDED.line_count, uploaded_at = now()
                   RETURNING id""",
                (collection, stored_path, digest, len(raw.encode()), len(m_body.text.splitlines()), s3_key),
            )).fetchone()
            await conn.execute("DELETE FROM common_chunks WHERE file_id = %s", (row["id"],))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO common_chunks (file_id, chunk_no, start_line, end_line, content, embedding) VALUES (%s, %s, %s, %s, %s, %s)",
                    [(row["id"], n, s, e, text, v) for n, ((s, e, text), v) in enumerate(zip(chunks, vectors))],
                )
            await self.store.put_text(s3_key, m_body.text, "text/plain; charset=utf-8")
            if old and old["s3_key"] != s3_key:
                await self.store.delete([old["s3_key"]])
        self.vault.remember(tokens)
        return ("updated" if old else "added"), m_body.redactions

    async def remask(self, value: str, match_mode: str) -> int:
        """Re-store files that still hold a newly added term in plain text."""
        if match_mode == "regex":
            condition, param = "c.content ~ %s OR f.path ~ %s", (value, value)
        else:
            like = "%" + value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            condition, param = "c.content ILIKE %s OR f.path ILIKE %s", (like, like)
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                f"""SELECT DISTINCT f.id, f.collection, f.path, f.sha256, f.s3_key FROM common_files f
                    JOIN common_chunks c ON c.file_id = f.id WHERE {condition}""", param,
            )).fetchall()

        changed = 0
        for row in rows:
            # Stored text is masked except for the new term; reveal gives the original back
            plain = self.vault.reveal(await self.store.get_text(row["s3_key"]))
            outcome, _ = await self._store_file(row["collection"], self.vault.reveal(row["path"]), plain, row)
            changed += outcome != "unchanged"
        return changed

    async def ticket_files(self, ticket: str, limit: int = 10) -> list[dict[str, Any]]:
        """Files in the snapshot that belong to or mention a ticket (path first, then mentions)."""
        like = f"%{ticket}%"
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                """SELECT f.collection, f.path, f.line_count, count(c.id) FILTER (WHERE c.content ILIKE %s) AS mentions,
                          (f.path ILIKE %s) AS in_path
                   FROM common_files f JOIN common_chunks c ON c.file_id = f.id
                   GROUP BY f.id HAVING bool_or(c.content ILIKE %s) OR f.path ILIKE %s
                   ORDER BY (f.path ILIKE %s) DESC, count(c.id) FILTER (WHERE c.content ILIKE %s) DESC LIMIT %s""",
                (like, like, like, like, like, like, limit),
            )).fetchall()
        return [{"collection": r["collection"], "path": self.vault.reveal(r["path"]), "lines": r["line_count"],
                 "why": "ticket folder" if r["in_path"] else f"mentioned in {r['mentions']} chunk(s)"} for r in rows]

    async def delete_collection(self, collection: str) -> dict[str, Any]:
        """Drop a whole collection from the index and S3."""
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "DELETE FROM common_files WHERE collection = %s RETURNING s3_key", (collection,)
            )).fetchall()
        await self.store.delete([r["s3_key"] for r in rows])
        return {"collection": collection, "removed": len(rows)}

    # ---- reads (AI) ----------------------------------------------------------------------

    async def list_files(self, collection: str | None = None, prefix: str | None = None, limit: int = 200) -> dict[str, Any]:
        """Collections, or file paths in one collection (optionally under a path prefix)."""
        async with self.pool.connection() as conn:
            if not collection:
                rows = await (await conn.execute(
                    """SELECT collection, count(*) AS files, sum(line_count) AS lines, max(uploaded_at) AS uploaded
                       FROM common_files GROUP BY collection ORDER BY collection"""
                )).fetchall()
                return {"collections": [
                    {"collection": r["collection"], "files": r["files"], "lines": r["lines"], "last_upload": r["uploaded"].isoformat()}
                    for r in rows
                ]}

            like = (self.vault.mask(prefix).text if prefix else "") + "%"
            rows = await (await conn.execute(
                """SELECT path, line_count FROM common_files WHERE collection = %s AND path LIKE %s
                   ORDER BY path LIMIT %s""",
                (collection, like, min(limit, 1000) + 1),
            )).fetchall()

        truncated = len(rows) > min(limit, 1000)
        files = [{"path": self.vault.reveal(r["path"]), "lines": r["line_count"]} for r in rows[:min(limit, 1000)]]
        return {"collection": collection, "files": files, "truncated": truncated}

    async def read(self, collection: str, path: str, start_line: int = 1, max_lines: int = 300) -> dict[str, Any]:
        """Numbered lines of one file."""
        masked_path = self.vault.mask(path).text
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT s3_key, line_count, uploaded_at FROM common_files WHERE collection = %s AND path = %s",
                (collection, masked_path),
            )).fetchone()
        if row is None:
            raise ValueError(f"{collection}:{path} not found (use common_list or common_search to find paths)")

        lines = (await self.store.get_text(row["s3_key"])).splitlines()
        start = max(1, start_line)
        end = min(len(lines), start + max(1, min(max_lines, 1000)) - 1)
        numbered = "\n".join(f"{n:>5}  {self.vault.reveal(lines[n - 1])}" for n in range(start, end + 1))
        return {
            "collection": collection, "path": path, "lines": f"{start}-{end} of {len(lines)}",
            "snapshot_uploaded": row["uploaded_at"].isoformat(), "content": numbered,
            "more": end < len(lines),
        }

    async def search(self, query: str, collection: str | None = None, path_prefix: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Hybrid (full-text + vector) search over file chunks."""
        limit = max(1, min(limit, 30))
        masked_query = self.vault.mask(query).text
        tsquery = build_tsquery(re.sub(r"[_./\\-]", " ", masked_query))
        vector = await self.embedder.embed_one(embedding_text("", [], masked_query))

        where, params = ["TRUE"], []
        if collection:
            where.append("f.collection = %s")
            params.append(collection)
        if path_prefix:
            where.append("f.path LIKE %s")
            params.append(self.vault.mask(path_prefix).text + "%")
        clause = " AND ".join(where)
        pool_size = max(limit * 4, 20)

        fts: list[tuple[int, float]] = []
        vec: list[tuple[int, float]] = []
        async with self.pool.connection() as conn:
            if tsquery:
                rows = await (await conn.execute(
                    f"""SELECT c.id, ts_rank_cd(c.tsv, to_tsquery('simple', %s)) AS rank FROM common_chunks c
                        JOIN common_files f ON f.id = c.file_id
                        WHERE {clause} AND c.tsv @@ to_tsquery('simple', %s) ORDER BY rank DESC LIMIT %s""",
                    (tsquery, *params, tsquery, pool_size),
                )).fetchall()
                fts = [(r["id"], r["rank"]) for r in rows]
            if vector is not None:
                rows = await (await conn.execute(
                    f"""SELECT c.id, c.embedding <=> %s::vector AS distance FROM common_chunks c
                        JOIN common_files f ON f.id = c.file_id
                        WHERE {clause} AND c.embedding IS NOT NULL ORDER BY c.embedding <=> %s::vector LIMIT %s""",
                    (vector, *params, vector, pool_size),
                )).fetchall()
                vec = [(r["id"], r["distance"]) for r in rows if r["distance"] < VECTOR_MAX_DISTANCE]
            ranks = fuse_scores(fts, vec)
            if not ranks:
                return []
            rows = await (await conn.execute(
                """SELECT c.id, f.collection, f.path, c.start_line, c.end_line, c.content
                   FROM common_chunks c JOIN common_files f ON f.id = c.file_id WHERE c.id = ANY(%s)""",
                (list(ranks),),
            )).fetchall()

        rows.sort(key=lambda r: ranks[r["id"]], reverse=True)
        return [
            {"collection": r["collection"], "path": self.vault.reveal(r["path"]), "lines": f"{r['start_line']}-{r['end_line']}",
             "score": round(ranks[r["id"]], 4), "preview": self.vault.reveal(r["content"])[:500]}
            for r in rows[:limit]
        ]

    async def grep(
        self, pattern: str, collection: str | None = None, path_prefix: str | None = None,
        regex: bool = False, case_sensitive: bool = False, max_results: int = 50,
    ) -> dict[str, Any]:
        """Line-level match like grep: literal text by default, POSIX regex when regex=True."""
        if not pattern:
            raise ValueError("pattern is required")
        masked_pattern = pattern if regex else self.vault.mask(pattern).text
        if regex:
            try:
                line_re = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
            except re.error as error:
                raise ValueError(f"invalid regex: {error}") from error
        else:
            line_re = re.compile(re.escape(masked_pattern), 0 if case_sensitive else re.IGNORECASE)

        where, params = ["TRUE"], []
        if collection:
            where.append("f.collection = %s")
            params.append(collection)
        if path_prefix:
            where.append("f.path LIKE %s")
            params.append(self.vault.mask(path_prefix).text + "%")
        if regex:
            where.append("c.content " + ("~" if case_sensitive else "~*") + " %s")
        else:
            where.append("c.content " + ("LIKE" if case_sensitive else "ILIKE") + " %s")
            masked_pattern = "%" + masked_pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        params.append(masked_pattern)

        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                f"""SELECT f.collection, f.path, c.start_line, c.content FROM common_chunks c
                    JOIN common_files f ON f.id = c.file_id WHERE {' AND '.join(where)}
                    ORDER BY f.collection, f.path, c.chunk_no LIMIT 400""",
                params,
            )).fetchall()

        # Regex runs on stored (masked) text, so a regex over a masked word won't hit; literal search masks the pattern
        hits: list[dict[str, Any]] = []
        for r in rows:
            for offset, line in enumerate(r["content"].split("\n")):
                if line_re.search(line):
                    hits.append({"collection": r["collection"], "path": self.vault.reveal(r["path"]),
                                 "line": r["start_line"] + offset, "text": self.vault.reveal(line.strip())[:300]})
                    if len(hits) >= max_results:
                        return {"matches": hits, "truncated": True}
        return {"matches": hits, "truncated": False}

    async def restore_from_store(self) -> int:
        """Rebuild the common index from S3 objects (path in the key is already masked)."""
        restored = 0
        for key in await self.store.list_keys("common/"):
            _, collection, stored_path = key.split("/", 2)
            text = await self.store.get_text(key)
            chunks = split_chunks(text)
            vectors = None
            if wants_vector(stored_path):
                vectors = await self.embedder.embed([embedding_text(stored_path, [], c[2]) for c in chunks])
            vectors = vectors or [None] * len(chunks)
            async with self.pool.connection() as conn, conn.transaction():
                row = await (await conn.execute(
                    """INSERT INTO common_files (collection, path, sha256, size_bytes, line_count, s3_key, uploaded_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (collection, path) DO NOTHING RETURNING id""",
                    (collection, stored_path, hashlib.sha256(text.encode()).hexdigest(), len(text.encode()),
                     len(text.splitlines()), key, datetime.now(timezone.utc)),
                )).fetchone()
                if row is None:
                    continue
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO common_chunks (file_id, chunk_no, start_line, end_line, content, embedding) VALUES (%s, %s, %s, %s, %s, %s)",
                        [(row["id"], n, s, e, t, v) for n, ((s, e, t), v) in enumerate(zip(chunks, vectors))],
                    )
            restored += 1
        return restored
