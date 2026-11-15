"""FastAPI app: MCP endpoint at /mcp, admin REST API at /api/admin, health at /health."""

import asyncio
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
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
from app.rest import bearer
from app.scope import set_work_only
from app.rest import router as rest_router
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


# Health and the API docs page carry no data, so they are open; everything else needs a token
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/docs/oauth2-redirect"}


class BearerAuth:
    """Token check for /mcp (MCP or admin token) and /api/admin (admin token). /health is open."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization", b"").decode()
        token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
        is_admin = bool(token) and hmac.compare_digest(token, settings.admin_token)
        is_client = bool(token) and hmac.compare_digest(token, settings.mcp_token)
        is_work = bool(token and settings.work_token) and hmac.compare_digest(token, settings.work_token)

        if scope["path"].startswith("/api/admin"):
            allowed = is_admin
        else:
            allowed = is_admin or is_client or is_work
        set_work_only(is_work)
        if not allowed:
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


app = FastAPI(
    title="work-memory",
    version="1.0.0",
    description="Long-term memory for AI agents. AI clients use MCP at `/mcp`; this page is the REST view for people. "
                "Click **Authorize** and paste the MCP token (read) or the admin token (read + admin).",
    lifespan=lifespan,
    redoc_url=None,
)
app.add_middleware(BearerAuth)
app.include_router(rest_router)
admin_router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(bearer)])
jobs: dict[str, dict[str, Any]] = {}
background_tasks: set[asyncio.Task] = set()


@app.get("/health", tags=["health"])
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


@admin_router.post("/terms")
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


@admin_router.delete("/terms")
async def remove_terms(terms: list[TermIn]) -> dict[str, Any]:
    """Remove dictionary entries (exact kind, value, match_mode, case flag). Run /remask?full=true afterwards."""
    results = [await services().vault.remove_term(t.kind, t.value, t.match_mode, t.case_insensitive) for t in terms]
    return {"removed": sum(r["status"] == "removed" for r in results), "not_found": sum(r["status"] == "not_found" for r in results)}


@admin_router.post("/remask")
async def remask_all(full: bool = False) -> dict[str, str]:
    """Apply the dictionary to everything already stored (background job).

    full=false: only text that still holds a term in plain form. full=true: reveal and re-mask everything,
    which also un-masks words whose term was removed.
    """

    async def work() -> dict[str, int]:
        svc = services()
        if full:
            return {"memories_remasked": await svc.memories.renormalize_all(),
                    "common_files_remasked": await svc.common.renormalize_all()}
        memories = files = 0
        for term in svc.vault.pseudo.terms:
            memories += await svc.memories.remask(term.value, term.match_mode)
            files += await svc.common.remask(term.value, term.match_mode)
        return {"memories_remasked": memories, "common_files_remasked": files}

    return start_job("remask", work)


@admin_router.post("/common/{collection}")
async def upload_common(collection: str, request: Request) -> dict[str, str]:
    """Upload a tar.gz snapshot for a collection; processed in the background, poll /api/admin/jobs/{id}."""
    length = int(request.headers.get("content-length") or 0)
    if length > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"archive over {settings.max_upload_mb} MB")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body; send the tar.gz as the request body")

    return start_job(f"upload {collection}", lambda: services().common.upload(collection, data))


@admin_router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """Status of a background upload."""
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return jobs[job_id]


@admin_router.delete("/common/{collection}")
async def delete_common(collection: str) -> dict[str, Any]:
    """Remove a collection from the snapshot."""
    return await services().common.delete_collection(collection)


@admin_router.post("/restore")
async def restore() -> dict[str, Any]:
    """Rebuild an empty database from S3 (dictionary, tokens, memories, common)."""
    svc = services()
    async with svc.pool.connection() as conn:
        row = await (await conn.execute("SELECT (SELECT count(*) FROM memories) + (SELECT count(*) FROM common_files) AS n")).fetchone()
    if row["n"]:
        raise HTTPException(409, "database is not empty; restore only runs on a fresh database")
    vault_counts = await svc.vault.restore_from_store()
    return {**vault_counts, "memories": await svc.memories.restore_from_store(), "common_files": await svc.common.restore_from_store()}


app.include_router(admin_router)

# MCP last, so the routes above match first
app.mount("/", mcp_app)
