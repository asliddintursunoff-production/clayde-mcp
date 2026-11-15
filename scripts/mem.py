"""Command-line MCP client: call any memory tool from a shell (for agents without native MCP, and for debugging).

Usage:
    python scripts/mem.py <tool> '<json arguments>'
    python scripts/mem.py tools                       # list tools
    python scripts/mem.py --railway memory_stats      # cloud server (~/.config/clayde-mcp/railway.env)
    echo '{"kind": "semantic", ...}' | python scripts/mem.py memory_save -   # arguments from stdin

Reads MCP_TOKEN and MEMORY_URL (default http://localhost:8000) from the environment or backend/.env.
Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


RAILWAY_ENV = Path.home() / ".config" / "clayde-mcp" / "railway.env"


def load_env() -> None:
    """Pick the server: `--railway` uses ~/.config/clayde-mcp/railway.env, otherwise backend/.env fills gaps."""
    if "--railway" in sys.argv:
        sys.argv.remove("--railway")
        for line in RAILWAY_ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def rpc(method: str, params: dict) -> dict:
    """One JSON-RPC call to /mcp (the server is stateless, no session needed)."""
    url = os.environ.get("MEMORY_URL", "http://localhost:8000").rstrip("/") + "/mcp"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['MCP_TOKEN']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    })
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code}: {error.read().decode()[:300]}")


def main() -> None:
    """Parse arguments, call, print the result as JSON."""
    load_env()
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    # Tool listing
    if sys.argv[1] == "tools":
        for tool in rpc("tools/list", {})["result"]["tools"]:
            print(f"{tool['name']}: {tool.get('description', '').splitlines()[0]}")
        return

    # Tool call; arguments inline or from stdin
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    arguments = json.loads(sys.stdin.read() if raw == "-" else raw)
    reply = rpc("tools/call", {"name": sys.argv[1], "arguments": arguments})
    if "error" in reply:
        sys.exit(f"error: {reply['error'].get('message')}")

    result = reply["result"]
    if result.get("isError"):
        sys.exit("tool error: " + result["content"][0]["text"])
    content = result.get("structuredContent")
    if isinstance(content, dict) and set(content) == {"result"}:
        content = content["result"]
    print(json.dumps(content if content is not None else result["content"][0]["text"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
