"""FastAPI app: MCP endpoint at /mcp, admin REST API at /api/admin, health at /health."""

import asyncio
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI, HTTPException, Request
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.common import CommonService
from app.config import get_settings
from app.db import create_pool, run_migrations
from app.embeddings import Embedder
from app.mcp_tools import mcp
from app.memories import MemoryService
from app.pseudonym import Pseudonymizer
from app.state import Services, services, set_services
from app.storage import ObjectStore
from app.vault import Vault

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memory")
settings = get_settings()

# MCP over streamable HTTP; stateless so restarts and several replicas need no sticky sessions
security = (
    TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if settings.allowed_hosts.strip() == "*"
    else TransportSecuritySettings(allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",")])
)
mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Migrate, connect, load the dictionary and the embedding model, then serve MCP."""
    applied = await run_migrations(settings.database_url)
    if applied:
        log.info("migrations applied: %s", applied)

    pool = create_pool(settings.database_url, settings.db_pool_max)
    await pool.open(wait=True)
    store = ObjectStore(settings.s3_bucket, settings.s3_endpoint_url, settings.s3_region,
                        settings.s3_access_key_id, settings.s3_secret_access_key, settings.s3_addressing_style)
    await store.ensure_bucket(settings.s3_create_bucket)

    vault = Vault(pool, store, Pseudonymizer(settings.pseudo_hmac_key, settings.pseudo_enc_key))
    await vault.load()
    embedder = Embedder(settings.embedding_model, settings.embedding_dim, settings.embedding_enabled, settings.embedding_cache_dir,
                        settings.embedding_threads)
    await anyio.to_thread.run_sync(embedder.load)

    set_services(Services(
        pool=pool, store=store, vault=vault, embedder=embedder,
        memories=MemoryService(pool, store, vault, embedder),
        common=CommonService(pool, store, vault, embedder, settings.common_max_file_kb),
    ))
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        set_services(None)
        await pool.close()


class BearerAuth:
    """Token check for /mcp (MCP or admin token) and /api/admin (admin token). /health is open."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization", b"").decode()
        token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
        is_admin = bool(token) and hmac.compare_digest(token, settings.admin_token)
        is_client = bool(token) and hmac.compare_digest(token, settings.mcp_token)

        if scope["path"].startswith("/api/admin"):
            allowed = is_admin
        else:
            allowed = is_admin or is_client
        if not allowed:
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app = FastAPI(title="work-memory", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(BearerAuth)
jobs: dict[str, dict[str, Any]] = {}
background_tasks: set[asyncio.Task] = set()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus whether vector search is available."""
    try:
        svc = services()
    except RuntimeError:
        return {"status": "starting"}
    async with svc.pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok", "embeddings": svc.embedder.ready}


class TermIn(BaseModel):
    """One dictionary entry for bulk import."""

    kind: str
    value: str
    match_mode: str = "word"
    case_insensitive: bool = True


@app.post("/api/admin/terms")
async def add_terms(terms: list[TermIn]) -> dict[str, Any]:
    """Bulk add terms to mask (names, company words, id patterns)."""
    svc = services()
    results = [await svc.vault.add_term(t.kind, t.value, t.match_mode, t.case_insensitive) for t in terms]
    remasked = 0
    for term, result in zip(terms, results):
        if result["status"] == "added":
            remasked += await svc.memories.remask(result["value"], term.match_mode)
            remasked += await svc.common.remask(result["value"], term.match_mode)
    return {"added": sum(r["status"] == "added" for r in results), "existing": sum(r["status"] == "exists" for r in results),
            "memories_remasked": remasked}


def start_job(kind: str, work) -> dict[str, str]:
    """Run a long admin task in the background; poll /api/admin/jobs/{id} (proxies cut long requests)."""
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "job": kind}

    async def run() -> None:
        try:
            jobs[job_id] = {"status": "done", **await work()}
        except Exception as error:  # report any failure to the poller instead of losing it
            log.exception("job %s failed", kind)
            jobs[job_id] = {"status": "failed", "error": str(error)}

    task = asyncio.get_running_loop().create_task(run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {"job_id": job_id}


@app.post("/api/admin/remask")
async def remask_all() -> dict[str, str]:
    """Apply every dictionary term to everything already stored (after adding terms in bulk or an upgrade)."""

    async def work() -> dict[str, int]:
        svc = services()
        memories = files = 0
        for term in svc.vault.pseudo.terms:
            memories += await svc.memories.remask(term.value, term.match_mode)
            files += await svc.common.remask(term.value, term.match_mode)
        return {"memories_remasked": memories, "common_files_remasked": files}

    return start_job("remask", work)


@app.post("/api/admin/common/{collection}")
async def upload_common(collection: str, request: Request) -> dict[str, str]:
    """Upload a tar.gz snapshot for a collection; processed in the background, poll /api/admin/jobs/{id}."""
    length = int(request.headers.get("content-length") or 0)
    if length > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"archive over {settings.max_upload_mb} MB")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body; send the tar.gz as the request body")

    return start_job(f"upload {collection}", lambda: services().common.upload(collection, data))


@app.get("/api/admin/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """Status of a background upload."""
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return jobs[job_id]


@app.delete("/api/admin/common/{collection}")
async def delete_common(collection: str) -> dict[str, Any]:
    """Remove a collection from the snapshot."""
    return await services().common.delete_collection(collection)


@app.post("/api/admin/restore")
async def restore() -> dict[str, Any]:
    """Rebuild an empty database from S3 (dictionary, tokens, memories, common)."""
    svc = services()
    async with svc.pool.connection() as conn:
        row = await (await conn.execute("SELECT (SELECT count(*) FROM memories) + (SELECT count(*) FROM common_files) AS n")).fetchone()
    if row["n"]:
        raise HTTPException(409, "database is not empty; restore only runs on a fresh database")
    vault_counts = await svc.vault.restore_from_store()
    return {**vault_counts, "memories": await svc.memories.restore_from_store(), "common_files": await svc.common.restore_from_store()}


# MCP last, so the routes above match first
app.mount("/", mcp_app)
