"""Persistence of the pseudonymization dictionary and token map (Postgres + encrypted S3 backup)."""

import json

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.pseudonym import KINDS, MaskResult, Pseudonymizer, Term
from app.storage import ObjectStore


class Vault:
    """Owns the Pseudonymizer and keeps its state in sync with storage."""

    def __init__(self, pool: AsyncConnectionPool, store: ObjectStore, pseudo: Pseudonymizer) -> None:
        self.pool = pool
        self.store = store
        self.pseudo = pseudo

    async def load(self) -> None:
        """Read every term and token from Postgres, decrypt, and load them into memory."""
        async with self.pool.connection() as conn:
            term_rows = await (await conn.execute("SELECT kind, value_enc, match_mode, case_insensitive FROM pseudo_terms")).fetchall()
            token_rows = await (await conn.execute("SELECT token, value_enc FROM pseudo_tokens")).fetchall()

        terms = [Term(r["kind"], self.pseudo.decrypt(r["value_enc"]), r["match_mode"], r["case_insensitive"]) for r in term_rows]
        tokens = {r["token"]: self.pseudo.decrypt(r["value_enc"]) for r in token_rows}
        self.pseudo.load(terms, tokens)

    def mask(self, text: str) -> MaskResult:
        """Mask text with the current dictionary (nothing is persisted here)."""
        return self.pseudo.mask(text)

    def reveal(self, text: str | None) -> str | None:
        """Turn tokens back into real values."""
        return self.pseudo.reveal(text) if text else text

    async def persist_tokens(self, conn: AsyncConnection, results: list[MaskResult]) -> list[tuple[str, str]]:
        """Store tokens first seen in these mask results, inside the caller's transaction.

        Call `remember` with the return value after the transaction commits.
        """
        new: dict[str, tuple[str, str]] = {}
        for result in results:
            new.update(result.new_tokens)

        for token, (kind, surface) in new.items():
            value_enc = self.pseudo.encrypt(surface)
            await conn.execute(
                "INSERT INTO pseudo_tokens (token, kind, value_enc) VALUES (%s, %s, %s) ON CONFLICT (token) DO NOTHING",
                (token, kind, value_enc),
            )
            await self.store.put_text(f"vault/tokens/{token}.json", json.dumps({"kind": kind, "value_enc": value_enc}), "application/json")

        return [(token, surface) for token, (_, surface) in new.items()]

    def remember(self, tokens: list[tuple[str, str]]) -> None:
        """Add committed tokens to the in-memory map so they can be revealed."""
        for token, surface in tokens:
            self.pseudo.add_token(token, surface)

    async def add_term(self, kind: str, value: str, match_mode: str = "word", case_insensitive: bool = True) -> dict:
        """Add a dictionary entry; text saved from now on is masked with it."""
        kind = kind.upper().strip()
        value = value.strip()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
        if match_mode not in ("word", "substring", "regex"):
            raise ValueError("match_mode must be word, substring or regex")
        if len(value) < 2:
            raise ValueError("value is too short")

        term = Term(kind, value, match_mode, case_insensitive)
        digest = self.pseudo.term_hmac(term)
        value_enc = self.pseudo.encrypt(value)

        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                """INSERT INTO pseudo_terms (kind, value_enc, value_hmac, match_mode, case_insensitive)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (value_hmac) DO NOTHING RETURNING id""",
                (kind, value_enc, digest, match_mode, case_insensitive),
            )).fetchone()
            if row is None:
                return {"status": "exists", "kind": kind, "value": value}
            backup = {"kind": kind, "value_enc": value_enc, "match_mode": match_mode, "case_insensitive": case_insensitive}
            await self.store.put_text(f"vault/terms/{digest}.json", json.dumps(backup), "application/json")

        self.pseudo.add_term(term)
        return {"status": "added", "kind": kind, "value": value, "match_mode": match_mode}

    async def remove_term(self, kind: str, value: str, match_mode: str = "word", case_insensitive: bool = True) -> dict:
        """Remove a dictionary entry (tokens already created stay revealable)."""
        term = Term(kind.upper().strip(), value.strip(), match_mode, case_insensitive)
        digest = self.pseudo.term_hmac(term)

        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("DELETE FROM pseudo_terms WHERE value_hmac = %s RETURNING id", (digest,))).fetchone()
            if row is None:
                return {"status": "not_found"}
            await self.store.delete([f"vault/terms/{digest}.json"])

        await self.load()
        return {"status": "removed", "kind": term.kind, "value": term.value}

    async def restore_from_store(self) -> dict[str, int]:
        """Rebuild the dictionary and token tables from the S3 backup, then reload."""
        counts = {"terms": 0, "tokens": 0}
        async with self.pool.connection() as conn, conn.transaction():
            for key in await self.store.list_keys("vault/terms/"):
                item = json.loads(await self.store.get_text(key))
                digest = key.rsplit("/", 1)[1].removesuffix(".json")
                await conn.execute(
                    """INSERT INTO pseudo_terms (kind, value_enc, value_hmac, match_mode, case_insensitive)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (value_hmac) DO NOTHING""",
                    (item["kind"], item["value_enc"], digest, item["match_mode"], item["case_insensitive"]),
                )
                counts["terms"] += 1
            for key in await self.store.list_keys("vault/tokens/"):
                item = json.loads(await self.store.get_text(key))
                token = key.rsplit("/", 1)[1].removesuffix(".json")
                await conn.execute(
                    "INSERT INTO pseudo_tokens (token, kind, value_enc) VALUES (%s, %s, %s) ON CONFLICT (token) DO NOTHING",
                    (token, item["kind"], item["value_enc"]),
                )
                counts["tokens"] += 1
        await self.load()
        return counts

    def list_terms(self) -> list[dict]:
        """Dictionary entries with real values (the caller is authenticated)."""
        return [
            {"kind": t.kind, "value": t.value, "match_mode": t.match_mode, "case_insensitive": t.case_insensitive}
            for t in sorted(self.pseudo.terms, key=lambda t: (t.kind, t.value.lower()))
        ]
