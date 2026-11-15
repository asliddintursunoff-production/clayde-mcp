"""Per-request access scope.

The full MCP token sees everything. The optional work token (for the Telegram bot and shared agents) only sees
work memories: anything tagged "personal" is invisible to it and it cannot create such memories.
"""

from contextvars import ContextVar

PERSONAL_TAG = "personal"
_work_only: ContextVar[bool] = ContextVar("work_only", default=False)


def set_work_only(value: bool) -> None:
    """Set the scope for the current request (called by the auth middleware)."""
    _work_only.set(value)


def work_only() -> bool:
    """True when the current request must not see personal memories."""
    return _work_only.get()


def personal_filter_sql() -> str:
    """Extra WHERE condition for memory queries in the current scope ('TRUE' when unrestricted)."""
    return f"NOT ('{PERSONAL_TAG}' = ANY(tags))" if work_only() else "TRUE"
