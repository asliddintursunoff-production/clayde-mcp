"""Masking of sensitive text before it is stored.

Two kinds of protection:
- Secrets (passwords, keys, tokens, connection strings) are removed for good: `[REDACTED_SECRET:<type>]`.
- Names, company words, emails and ids from the dictionary are replaced by stable tokens such as
  `PERSON_3f9a0c12be`. The real value is kept only encrypted (Fernet), so the database and S3
  never hold it in plain text, and `reveal` turns tokens back into real values for the client.
"""

import hashlib
import hmac
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

KINDS = ("PERSON", "ORG", "EMAIL", "ID", "PLACE", "PROJECT", "TERM")
TOKEN_RE = re.compile(r"(?:%s)_[0-9a-f]{10}" % "|".join(KINDS))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _entropy(value: str) -> float:
    """Shannon entropy per character, used to tell random keys from ordinary words."""
    counts = Counter(value)
    return -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values())


def _looks_random(value: str) -> bool:
    """True for strings that look like generated secrets (mixed letters/digits, high entropy)."""
    stripped = value.strip("'\"")
    has_digit = any(ch.isdigit() for ch in stripped)
    has_alpha = any(ch.isalpha() for ch in stripped)
    return len(stripped) >= 16 and has_digit and has_alpha and _entropy(stripped) >= 3.5


@dataclass(frozen=True)
class SecretPattern:
    """One secret detector: regex, which group to cut out, and an optional value check."""

    name: str
    regex: re.Pattern[str]
    group: int = 0
    must_look_random: bool = False


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    SecretPattern("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    SecretPattern("azure_account_key", re.compile(r"(?i)AccountKey=([A-Za-z0-9+/=]{20,})"), group=1),
    SecretPattern("sas_signature", re.compile(r"(?i)\bsig=([A-Za-z0-9%+/=]{20,})"), group=1),
    SecretPattern("conn_password", re.compile(r"(?i)\b(?:password|pwd)\s*=\s*([^;\s'\"]{4,})"), group=1),
    SecretPattern("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    SecretPattern("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    SecretPattern("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    SecretPattern("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    SecretPattern("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    SecretPattern("bearer", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_\-.=]{20,})"), group=1),
    SecretPattern(
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|client[_-]?secret|password|passwd|access[_-]?key)\b"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=.]{16,})[\"']?"
        ),
        group=1,
        must_look_random=True,
    ),
)


@dataclass(frozen=True)
class Term:
    """A dictionary entry: a value to mask and how to match it."""

    kind: str
    value: str
    match_mode: str = "word"
    case_insensitive: bool = True


@dataclass
class MaskResult:
    """Masked text plus what happened, so callers can persist new tokens and report redactions."""

    text: str
    new_tokens: dict[str, tuple[str, str]] = field(default_factory=dict)
    redactions: Counter = field(default_factory=Counter)
    masked: Counter = field(default_factory=Counter)


@dataclass(order=True)
class _Span:
    """A candidate replacement; lower priority number wins on overlap, then the longer span."""

    priority: int
    neg_length: int
    start: int
    end: int
    replacement: str = field(compare=False)
    kind: str = field(compare=False, default="")
    surface: str = field(compare=False, default="")


class Pseudonymizer:
    """Masks and reveals text with an in-memory copy of the dictionary and token map."""

    def __init__(self, hmac_key: str, enc_key: str) -> None:
        self._hmac_key = hmac_key.encode()
        self._fernet = Fernet(enc_key.encode())
        self._terms: list[Term] = []
        self._token_to_value: dict[str, str] = {}
        self._literal_re: re.Pattern[str] | None = None
        self._literal_cs: dict[str, str] = {}
        self._literal_ci: dict[str, str] = {}
        self._regex_terms: list[tuple[str, re.Pattern[str]]] = []

    # ---- keys and encryption -------------------------------------------------------------

    def digest(self, value: str) -> str:
        """Keyed hash used for tokens and for de-duplicating dictionary entries."""
        return hmac.new(self._hmac_key, value.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        """Encrypt a real value for storage."""
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value_enc: str) -> str:
        """Decrypt a stored value."""
        return self._fernet.decrypt(value_enc.encode()).decode()

    def term_hmac(self, term: Term) -> str:
        """Stable id of a dictionary entry (case folded when the entry ignores case)."""
        value = term.value.lower() if term.case_insensitive else term.value
        return self.digest(f"term:{term.kind}:{term.match_mode}:{value}")

    def token_for(self, kind: str, surface: str) -> str:
        """Token for one exact surface form; the same input always gives the same token."""
        return f"{kind}_{self.digest(f'{kind}:{surface}')[:10]}"

    # ---- dictionary ----------------------------------------------------------------------

    def load(self, terms: list[Term], tokens: dict[str, str]) -> None:
        """Replace the in-memory dictionary and token map (values already decrypted)."""
        self._terms = list(terms)
        self._token_to_value = dict(tokens)
        self._rebuild()

    def add_term(self, term: Term) -> None:
        """Add one entry and recompile the matchers."""
        if term.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if term.match_mode == "regex":
            re.compile(term.value)
        self._terms.append(term)
        self._rebuild()

    def add_token(self, token: str, value: str) -> None:
        """Remember a token created elsewhere (for example after a DB insert)."""
        self._token_to_value[token] = value

    @property
    def terms(self) -> list[Term]:
        """Current dictionary entries."""
        return list(self._terms)

    def _rebuild(self) -> None:
        """Compile literal entries into one regex (longest first) and keep regex entries apart."""
        literals = sorted((t for t in self._terms if t.match_mode != "regex"), key=lambda t: -len(t.value))
        self._literal_cs = {t.value: t.kind for t in literals if not t.case_insensitive}
        self._literal_ci = {t.value.lower(): t.kind for t in literals if t.case_insensitive}

        pieces = []
        for term in literals:
            piece = re.escape(term.value)
            if term.case_insensitive:
                piece = f"(?i:{piece})"
            if term.match_mode == "word":
                piece = f"(?<![0-9A-Za-z]){piece}(?![0-9A-Za-z])"
            pieces.append(piece)
        self._literal_re = re.compile("|".join(pieces)) if pieces else None

        self._regex_terms = [(t.kind, re.compile(t.value)) for t in self._terms if t.match_mode == "regex"]

    # ---- masking -------------------------------------------------------------------------

    def mask(self, text: str) -> MaskResult:
        """Redact secrets and replace dictionary matches and emails with tokens.

        All detectors run on the original text; overlaps are resolved by priority
        (known token > secret > email > dictionary word > dictionary regex), then length.
        """
        result = MaskResult(text=text)
        if not text:
            return result

        spans: list[_Span] = []

        # Tokens already in the text stay as they are (text that was saved without reveal)
        for m in TOKEN_RE.finditer(text):
            if m.group(0) in self._token_to_value:
                spans.append(_Span(0, -(m.end() - m.start()), m.start(), m.end(), m.group(0)))

        # Secrets are cut out for good
        for pattern in SECRET_PATTERNS:
            for m in pattern.regex.finditer(text):
                if pattern.must_look_random and not _looks_random(m.group(pattern.group)):
                    continue
                start, end = m.span(pattern.group)
                spans.append(_Span(1, -(end - start), start, end, f"[REDACTED_SECRET:{pattern.name}]", "SECRET", pattern.name))

        # Emails and dictionary entries become tokens
        for m in EMAIL_RE.finditer(text):
            spans.append(self._token_span(2, m, "EMAIL"))
        if self._literal_re is not None:
            for m in self._literal_re.finditer(text):
                surface = m.group(0)
                kind = self._literal_cs.get(surface) or self._literal_ci.get(surface.lower(), "TERM")
                spans.append(self._token_span(3, m, kind))
        for kind, regex in self._regex_terms:
            for m in regex.finditer(text):
                if m.end() > m.start():
                    spans.append(self._token_span(4, m, kind))

        # Keep the best non-overlapping spans, then rebuild the text left to right
        chosen: list[_Span] = []
        for span in sorted(spans):
            if all(span.end <= c.start or span.start >= c.end for c in chosen):
                chosen.append(span)
        chosen.sort(key=lambda s: s.start)

        parts: list[str] = []
        cursor = 0
        for span in chosen:
            parts.append(text[cursor:span.start])
            parts.append(span.replacement)
            cursor = span.end
            if span.kind == "SECRET":
                result.redactions[span.surface] += 1
            elif span.kind:
                result.masked[span.kind] += 1
                if span.replacement not in self._token_to_value:
                    result.new_tokens[span.replacement] = (span.kind, span.surface)
        parts.append(text[cursor:])

        result.text = "".join(parts)
        return result

    def _token_span(self, priority: int, m: re.Match[str], kind: str) -> _Span:
        """Build a token replacement span for a regex match."""
        surface = m.group(0)
        return _Span(priority, -(m.end() - m.start()), m.start(), m.end(), self.token_for(kind, surface), kind, surface)

    def reveal(self, text: str) -> str:
        """Replace known tokens with their real values; unknown tokens are left as they are."""
        if not text:
            return text
        return TOKEN_RE.sub(lambda m: self._token_to_value.get(m.group(0), m.group(0)), text)
