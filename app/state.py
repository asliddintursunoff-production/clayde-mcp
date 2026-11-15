"""Process-wide service container, filled once at startup."""

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.common import CommonService
from app.embeddings import Embedder
from app.memories import MemoryService
from app.storage import ObjectStore
from app.vault import Vault


@dataclass
class Services:
    """Everything the tools and routes need."""

    pool: AsyncConnectionPool
    store: ObjectStore
    vault: Vault
    embedder: Embedder
    memories: MemoryService
    common: CommonService


_services: Services | None = None


def set_services(value: Services | None) -> None:
    """Install (or clear) the container."""
    global _services
    _services = value


def services() -> Services:
    """The container; raises if the app has not started yet."""
    if _services is None:
        raise RuntimeError("server is still starting")
    return _services
