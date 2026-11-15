"""End-to-end tests against a running server (docker compose exec app pytest). Synthetic data only.

Talks plain JSON-RPC to /mcp, the same way any MCP client does.
"""

import io
import json
import os
import tarfile
import time
import uuid

import httpx
import pytest

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
RUN = uuid.uuid4().hex[:6]

pytestmark = pytest.mark.skipif(not MCP_TOKEN, reason="needs a running server and MCP_TOKEN")


class Client:
    """Minimal MCP client: initialize once, then tools/call."""

    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url=BASE, timeout=120, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        })
        self.next_id = 0
        init = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1"},
        })
        self.http.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        self.instructions = init.get("instructions", "")

    def rpc(self, method: str, params: dict) -> dict:
        """One JSON-RPC request; raises on protocol errors."""
        self.next_id += 1
        response = self.http.post("/mcp", json={"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        return body["result"]

    def tool(self, name: str, **arguments) -> object:
        """Call a tool and return its parsed result (raises AssertionError on tool errors)."""
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"] if result.get("content") else ""
        if result.get("isError"):
            raise AssertionError(text)
        if "structuredContent" in result:
            content = result["structuredContent"]
            return content.get("result", content) if isinstance(content, dict) and set(content) == {"result"} else content
        return json.loads(text)


@pytest.fixture(scope="module")
def client() -> Client:
    return Client(MCP_TOKEN)


def test_auth_required() -> None:
    assert httpx.post(f"{BASE}/mcp", json={}).status_code == 401
    assert httpx.get(f"{BASE}/api/admin/jobs/x", headers={"Authorization": f"Bearer {MCP_TOKEN}"}).status_code == 401
    assert httpx.get(f"{BASE}/health").json()["status"] == "ok"


def test_tools_listed_with_instructions(client: Client) -> None:
    names = {t["name"] for t in client.rpc("tools/list", {})["tools"]}
    assert {"memory_search", "memory_save", "task_context", "common_grep", "pseudo_add_term"} <= names
    assert "never run" in client.instructions


def test_memory_lifecycle_with_masking(client: Client) -> None:
    ticket = f"TEST{RUN}"
    person = f"Zelda{RUN}"
    client.tool("pseudo_add_term", kind="PERSON", value=person)
    client.tool("pseudo_add_term", kind="ORG", value=f"globex{RUN}", match_mode="substring")

    # Save: secret removed, name masked at rest, revealed on read
    saved = client.tool(
        "memory_save", kind="semantic", ticket=ticket, tags=["daily_agent_work"], importance=4,
        title=f"{person} decided the backfill window",
        body=f"{person} (Globex{RUN}Box team) said backfill from April, minimum 90 days. password=Sup3rS3cret!x",
        source="test",
    )
    assert saved["status"] == "saved", saved
    assert saved["secrets_removed"] == {"conn_password": 1}

    got = client.tool("memory_get", ids=[saved["id"]])[0]
    assert person in got["title"] and f"Globex{RUN}Box" in got["body"]
    assert "Sup3rS3cret" not in got["body"]

    # Duplicate is not stored twice
    again = client.tool("memory_save", kind="semantic", ticket=ticket, title=got["title"],
                        body=f"{person} (Globex{RUN}Box team) said backfill from April, minimum 90 days. password=Sup3rS3cret!x")
    assert again["status"] == "duplicate"

    # Search by name (full-text over the token) and by meaning
    by_name = client.tool("memory_search", query=f"what did {person} say", ticket=ticket)
    assert by_name and by_name[0]["id"] == saved["id"]
    by_meaning = client.tool("memory_search", query="how many days of history to load", ticket=ticket)
    assert any(r["id"] == saved["id"] for r in by_meaning)

    # Supersede: old archived, new found
    newer = client.tool("memory_save", kind="semantic", ticket=ticket, importance=4, supersedes=saved["id"],
                        title=f"{person} changed the backfill window", body=f"{person}: backfill from March instead.")
    old = client.tool("memory_get", ids=[saved["id"]])[0]
    assert old["status"] == "archived" and old["superseded_by"] == newer["id"]
    assert all(r["id"] != saved["id"] for r in client.tool("memory_search", query="backfill window", ticket=ticket))

    # Update keeps a revision
    updated = client.tool("memory_update", id=newer["id"], body=f"{person}: backfill from March 2025 instead.")
    assert updated["revision"] == 2

    # Task wrap-up and context
    client.tool("close_task", ticket=ticket, summary="Synthetic task finished.",
                lessons=[f"Ask {person} before changing backfill windows ({RUN})."],
                decisions=["Keep the 90 day minimum."])
    context = client.tool("task_context", ticket=ticket)
    assert len(context["history"]) >= 3
    assert any(RUN in r["body"] for r in context["standing_rules"])


def test_stored_text_is_masked_in_database_and_s3() -> None:
    """Look under the hood: no plain name in Postgres or S3."""
    import boto3
    import psycopg

    person = f"Zelda{RUN}"
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute("SELECT title || body FROM memories WHERE ticket = %s", (f"TEST{RUN}".upper(),)).fetchall()
        assert rows and all(person not in r[0] for r in rows)
        assert all("PERSON_" in r[0] for r in rows if "decided" in r[0] or "changed" in r[0])

    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"], region_name="us-east-1")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=os.environ["S3_BUCKET"], Prefix="memories/").get("Contents", [])]
    texts = [s3.get_object(Bucket=os.environ["S3_BUCKET"], Key=k)["Body"].read().decode() for k in keys]
    assert texts and all(person not in t for t in texts)
    assert all("Sup3rS3cret" not in t for t in texts)


def test_term_added_later_remasks_old_memories(client: Client) -> None:
    late = f"Quill{RUN}"
    saved = client.tool("memory_save", kind="episodic", title=f"Call with {late}", body=f"{late} asked for a daily refresh.")
    result = client.tool("pseudo_add_term", kind="PERSON", value=late)
    assert result["memories_remasked"] == 1

    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        stored = conn.execute("SELECT title || body FROM memories WHERE id = %s", (saved["id"],)).fetchone()[0]
    assert late not in stored and "PERSON_" in stored
    assert late in client.tool("memory_get", ids=[saved["id"]])[0]["body"]


def test_tool_errors_are_readable(client: Client) -> None:
    with pytest.raises(AssertionError, match="importance must be 1..5|less than or equal"):
        client.tool("memory_save", kind="semantic", title="x", body="y", importance=9)
    with pytest.raises(AssertionError, match="not found"):
        client.tool("common_read", collection="nope", path="a.txt")


def test_common_snapshot(client: Client) -> None:
    collection = f"repo-{RUN}"
    notebook = {"name": "clean_Vendor", "properties": {"cells": [
        {"cell_type": "code", "source": ["df = spark.read.load(path)\n", f"# owner Zelda{RUN}\n"]},
        {"cell_type": "code", "source": ["merge_data(df, 'Derived_Key')\n"]},
    ]}}
    files = {
        "notebook/clean_Vendor.json": json.dumps(notebook),
        "sql/Load_Fact.sql": "CREATE PROC dbo.Load_Fact AS\nSELECT Minute_Count FROM ext.Fact;\n",
        "docs/design.md": "# Fact load\nThe nightly job rebuilds the agent work table from vendor schedules.\n",
        ".git/config": "[core]\n",
        ".env": "TOKEN=abc",
        "conf/app.ini": "AccountKey=abcdEFGHijklMNOPqrstUVWX1234==\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    job = httpx.post(f"{BASE}/api/admin/common/{collection}", content=buffer.getvalue(), headers=admin).json()
    for _ in range(60):
        status = httpx.get(f"{BASE}/api/admin/jobs/{job['job_id']}", headers=admin).json()
        if status["status"] != "running":
            break
        time.sleep(1)
    assert status["status"] == "done", status
    assert status["added"] == 4 and status["skipped"] == {"ignored_dir": 1, "secret_like_file": 1}
    assert status["secrets_removed"] == {"azure_account_key": 1}

    listing = client.tool("common_list", collection=collection)
    assert {f["path"] for f in listing["files"]} == {"notebook/clean_Vendor.json", "sql/Load_Fact.sql", "conf/app.ini", "docs/design.md"}

    page = client.tool("common_read", collection=collection, path="notebook/clean_Vendor.json")
    assert "cell 2" in page["content"] and f"Zelda{RUN}" in page["content"]

    grep = client.tool("common_grep", pattern="Minute_Count", collection=collection)
    assert grep["matches"][0]["path"] == "sql/Load_Fact.sql" and grep["matches"][0]["line"] == 2

    found = client.tool("common_search", query="merge_data Derived_Key", collection=collection)
    assert found[0]["path"] == "notebook/clean_Vendor.json"

    # Only the doc has a vector; it is found by meaning, code still by keywords
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        embedded = conn.execute(
            """SELECT f.path FROM common_chunks c JOIN common_files f ON f.id = c.file_id
               WHERE f.collection = %s AND c.embedding IS NOT NULL""", (collection,)).fetchall()
    assert [r[0] for r in embedded] == ["docs/design.md"]
    by_meaning = client.tool("common_search", query="how is the work table refreshed every night", collection=collection)
    assert by_meaning[0]["path"] == "docs/design.md"

    # Re-upload with the same content: nothing changes
    job = httpx.post(f"{BASE}/api/admin/common/{collection}", content=buffer.getvalue(), headers=admin).json()
    for _ in range(60):
        status = httpx.get(f"{BASE}/api/admin/jobs/{job['job_id']}", headers=admin).json()
        if status["status"] != "running":
            break
        time.sleep(1)
    assert status.get("unchanged") == 4

    # A name added later is masked in stored files too; the ticket note shows up in task_context
    late = "Orla" + RUN.translate(str.maketrans("0123456789", "ghijklmnop"))
    files2 = {f"TICKET{RUN}/PROGRESS.md": f"Meeting with {late}: she approved the design for TICKET{RUN}.\n"}
    buffer2 = io.BytesIO()
    with tarfile.open(fileobj=buffer2, mode="w:gz") as archive:
        for path, text in files2.items():
            info = tarfile.TarInfo(path)
            info.size = len(text.encode())
            archive.addfile(info, io.BytesIO(text.encode()))
    job = httpx.post(f"{BASE}/api/admin/common/notes-{RUN}", content=buffer2.getvalue(), headers=admin).json()
    for _ in range(60):
        if httpx.get(f"{BASE}/api/admin/jobs/{job['job_id']}", headers=admin).json()["status"] != "running":
            break
        time.sleep(1)
    assert any(s["word"] == late for s in client.tool("pseudo_suggest_terms"))
    added = client.tool("pseudo_add_term", kind="PERSON", value=late)
    assert added["common_files_remasked"] == 1
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM common_chunks WHERE content LIKE %s", (f"%{late}%",)).fetchone()[0] == 0
    assert late in client.tool("common_read", collection=f"notes-{RUN}", path=f"TICKET{RUN}/PROGRESS.md")["content"]
    context = client.tool("task_context", ticket=f"TICKET{RUN}")
    assert context["notes_in_common"][0]["path"] == f"TICKET{RUN}/PROGRESS.md"

    httpx.delete(f"{BASE}/api/admin/common/{collection}", headers=admin)
    httpx.delete(f"{BASE}/api/admin/common/notes-{RUN}", headers=admin)


def test_rest_api_and_docs(client: Client) -> None:
    """Docs page is open; data endpoints need a token; admin endpoints need the admin token."""
    assert httpx.get(f"{BASE}/docs").status_code == 200
    assert httpx.get(f"{BASE}/api/stats").status_code == 401
    read = {"Authorization": f"Bearer {MCP_TOKEN}"}
    assert httpx.get(f"{BASE}/api/admin/jobs/x", headers=read).status_code == 401

    saved = client.tool("memory_save", kind="semantic", title=f"REST check {RUN}", body="Visible through the REST view.")
    found = httpx.get(f"{BASE}/api/memories/search", params={"q": f"REST check {RUN}", "limit": 1}, headers=read).json()
    assert found[0]["id"] == saved["id"]
    assert httpx.get(f"{BASE}/api/memories/{saved['id']}", headers=read).json()["body"] == "Visible through the REST view."
    assert httpx.get(f"{BASE}/api/memories/not-a-uuid", headers=read).status_code == 400
    tools = httpx.get(f"{BASE}/api/tools", headers=read).json()["tools"]
    assert any(t["name"] == "memory_save" and t["parameters"] for t in tools)


def test_removed_term_is_unmasked_by_full_remask(client: Client) -> None:
    """A wrong term (a common word) can be removed and stored text returns to plain words."""
    word = "zed" + RUN.translate(str.maketrans("0123456789", "ghijklmnop"))
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    client.tool("pseudo_add_term", kind="PERSON", value=word)
    saved = client.tool("memory_save", kind="semantic", title=f"Merge {word} rule", body=f"The merge {word} are unique.")

    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert word not in conn.execute("SELECT body FROM memories WHERE id = %s", (saved["id"],)).fetchone()[0]

    term = [{"kind": "PERSON", "value": word}]
    assert httpx.request("DELETE", f"{BASE}/api/admin/terms", json=term, headers=admin).json()["removed"] == 1
    job = httpx.post(f"{BASE}/api/admin/remask", params={"full": "true"}, headers=admin).json()
    for _ in range(60):
        status = httpx.get(f"{BASE}/api/admin/jobs/{job['job_id']}", headers=admin).json()
        if status["status"] != "running":
            break
        time.sleep(1)
    assert status["status"] == "done" and status["memories_remasked"] >= 1
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert word in conn.execute("SELECT body FROM memories WHERE id = %s", (saved["id"],)).fetchone()[0]
