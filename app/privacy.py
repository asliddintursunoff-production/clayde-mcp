"""Finds words in stored text that look like people's names but are not masked yet.

The dictionary only masks what it knows; this closes the gap by suggesting candidates for review.
Only prose is scanned (memories and doc files), not code.
"""

import re
from collections import Counter
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.common import MEANING_EXT

# A capitalized word in a context that usually means a person
PERSON_CONTEXT = re.compile(
    r"(?:\b(?:with|from|to|ask|asked|told|tell|ping|cc|by|per|and|thanks|hi|hey)\s+([A-Z][a-z]{2,15})\b(?![_(]))"
    r"|(?:\b([A-Z][a-z]{2,15})(?:'s|’s)?\s+(?:said|says|asked|wrote|replied|confirmed|approved|answered|wants|thinks|"
    r"deployed|merged|reviewed|mentioned|suggested|agreed)\b)"
    r"|(?:(?<![\w@])@([A-Z][a-z]{2,15})\b)"
)
WORD = re.compile(r"\b[A-Za-z][a-z]{2,15}\b")
NOT_NAMES = frozenset(
    "The This That Then There These Those What When Where Which While With From Monday Tuesday Wednesday Thursday "
    "Friday Saturday Sunday January February March April May June July August September October November December "
    "Teams Synapse Power Azure Python Spark Delta Note Notes Next Done Status Summary Decision Lesson Ticket Story "
    "Design Testing Dev Prod Test Data Source Table Column Pipeline Notebook Daily Agent Work Fact Clean Raw Load "
    "Ext Dbo Sql Json Yes Also Step Kutilgan Natija Xulosa Keyingi Qadam Savol Javob Muammo Yechim Eslatma".split()
)


async def suggest_names(pool: AsyncConnectionPool, known: set[str], limit: int = 40) -> list[dict[str, Any]]:
    """Name-like words that appear unmasked, most frequent first, with one example line each."""
    async with pool.connection() as conn:
        memories = await (await conn.execute(
            "SELECT title || E'\\n' || body AS text FROM memories WHERE status = 'active'"
        )).fetchall()
        docs = await (await conn.execute(
            f"""SELECT c.content AS text FROM common_chunks c JOIN common_files f ON f.id = c.file_id
                WHERE {' OR '.join("lower(f.path) LIKE %s" for _ in MEANING_EXT)}""",
            [f"%{ext}" for ext in MEANING_EXT],
        )).fetchall()

    counts: Counter = Counter()
    lower_use: Counter = Counter()
    examples: dict[str, str] = {}
    for row in memories + docs:
        # Ordinary words also show up in lower case; names almost never do
        lower_use.update(w for w in WORD.findall(row["text"]) if w.islower())
        for line in row["text"].splitlines():
            for match in PERSON_CONTEXT.finditer(line):
                word = next(g for g in match.groups() if g)
                if word in NOT_NAMES or word.lower() in known:
                    continue
                counts[word] += 1
                examples.setdefault(word, line.strip()[:160])

    names = [(w, n) for w, n in counts.most_common() if lower_use[w.lower()] <= n // 2]
    return [{"word": w, "count": n, "example": examples[w]} for w, n in names[:limit]]
