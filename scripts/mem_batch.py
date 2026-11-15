"""Run many MCP tool calls from a JSONL file (one {"tool": ..., "args": {...}} per line).

Usage:
    python scripts/mem_batch.py [--railway] calls.jsonl

Prints one compact line per call (status/id or error), so an agent can save dozens of memories in one step.
Uses the same environment as mem.py (MEMORY_URL, MCP_TOKEN).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mem import load_env, rpc  # noqa: E402


def main() -> None:
    """Call each line's tool and report."""
    load_env()
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    for number, line in enumerate(Path(sys.argv[1]).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            call = json.loads(line)
        except ValueError as error:
            print(f"{number}: bad json: {error}")
            continue

        reply = rpc("tools/call", {"name": call["tool"], "arguments": call.get("args", {})})
        result = reply.get("result", {})
        if "error" in reply or result.get("isError"):
            message = reply.get("error", {}).get("message") or result["content"][0]["text"]
            print(f"{number}: ERROR {message[:200]}")
            continue
        content = result.get("structuredContent")
        if isinstance(content, dict) and set(content) == {"result"}:
            content = content["result"]
        title = call.get("args", {}).get("title") or call.get("args", {}).get("value", "")
        print(f"{number}: {json.dumps(content, ensure_ascii=False)[:220]} | {title[:60]}")


if __name__ == "__main__":
    main()
