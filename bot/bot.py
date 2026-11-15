"""Telegram <-> Claude Code bridge, cloud edition.

Each Telegram message goes to `claude -p` (subscription login via CLAUDE_CODE_OAUTH_TOKEN), resuming one shared
conversation. Knowledge lives in the work-memory MCP server, not in local files. Only paired chats are served.
Claude here has no shell: it can use the memory tools and read/write files only inside the bot's work folder.
"""

import html
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

# ---- configuration (all from the environment) ----------------------------------------------

DATA = Path(os.environ.get("DATA_DIR", "/data"))
WORK = DATA / "work"
UPLOADS = WORK / "uploads"
STATE_FILE = DATA / "state.json"
MCP_CONFIG = DATA / "mcp.json"
RULES_FILE = Path(__file__).resolve().parent / "rules.md"

TOKEN = os.environ["TELEGRAM_TOKEN"]
PAIR_CODE = os.environ["PAIR_CODE"]
MODEL = os.environ.get("BOT_MODEL", "opus")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SEC", str(30 * 60)))
API = f"https://api.telegram.org/bot{TOKEN}"

# Permission paths: "//abs/path" is absolute. Files are reachable only inside the work folder; the token-bearing
# files (MCP config, state, Claude login) and /proc (process environment) are denied explicitly.
ALLOWED_TOOLS = ["mcp__work-memory", f"Read(/{WORK}/**)", f"Write(/{WORK}/**)", f"Edit(/{WORK}/**)"]
DISALLOWED_TOOLS = [
    "Bash", "WebFetch", "WebSearch", "NotebookEdit", "Agent", "Glob", "Grep",
    f"Read(/{MCP_CONFIG})", f"Read(/{STATE_FILE})", f"Read(/{DATA}/.claude/**)", f"Read(/{DATA}/.claude.json)",
    "Read(//proc/**)", "Read(//etc/**)", "Read(//app/**)",
]
MAX_CHUNK = 4000

lock = threading.Lock()


# ---- state ---------------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    """Paired chats, contact names, the shared Claude session and the Telegram update offset."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"chat_ids": [], "names": {}, "session_id": None, "offset": 0}


def save_state(state: dict[str, Any]) -> None:
    """Persist state on the volume (owner-only permissions)."""
    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATE_FILE.chmod(0o600)


def write_mcp_config() -> None:
    """MCP config for claude: the memory server over HTTP with the bearer token."""
    config = {"mcpServers": {"work-memory": {
        "type": "http",
        "url": os.environ["MEMORY_MCP_URL"],
        "headers": {"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"},
    }}}
    MCP_CONFIG.write_text(json.dumps(config))
    MCP_CONFIG.chmod(0o600)


# ---- Telegram ------------------------------------------------------------------------------

def tg(method: str, **params: Any) -> dict[str, Any]:
    """Call the Bot API; network drops are retried instead of crashing the thread."""
    for attempt in range(3):
        try:
            return requests.post(f"{API}/{method}", json=params, timeout=70).json()
        except requests.RequestException as error:
            print(f"{method} error (attempt {attempt + 1}): {error}", flush=True)
            time.sleep(5)
    return {}


def _text_html(text: str) -> str:
    """Plain text with **bold** and `code` turned into Telegram HTML."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)


def to_html_chunks(text: str) -> list[str]:
    """Split a reply into Telegram-sized HTML messages; ``` blocks become copyable <pre> blocks."""
    parts = re.split(r"```(\w*)\n(.*?)```", text, flags=re.S)
    pieces: list[str] = []
    for index in range(0, len(parts), 3):
        if parts[index].strip():
            pieces.append(_text_html(parts[index].strip()))
        if index + 2 < len(parts):
            language, code = parts[index + 1], parts[index + 2].rstrip()
            css = f' class="language-{language}"' if language else ""
            pieces.append(f"<pre><code{css}>{html.escape(code)}</code></pre>")

    chunks, current = [], ""
    for piece in pieces:
        if len(current) + len(piece) + 2 > MAX_CHUNK and current:
            chunks.append(current)
            current = ""
        while len(piece) > MAX_CHUNK:  # a single huge piece: hard split (rare)
            chunks.append(piece[:MAX_CHUNK])
            piece = piece[MAX_CHUNK:]
        current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks or ["(bo'sh javob)"]


def send(chat_id: int, text: str) -> None:
    """Send formatted text; fall back to plain text if Telegram rejects the HTML."""
    for chunk in to_html_chunks(text):
        result = tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML")
        if not result.get("ok"):
            tg("sendMessage", chat_id=chat_id, text=re.sub(r"<[^>]+>", "", html.unescape(chunk))[:MAX_CHUNK])


def send_document(chat_id: int, name: str, content: str) -> bool:
    """Send text as a file attachment (task briefs, SQL, notebooks)."""
    try:
        response = requests.post(f"{API}/sendDocument", data={"chat_id": chat_id},
                                 files={"document": (name, content.encode())}, timeout=70)
        return bool(response.json().get("ok"))
    except requests.RequestException as error:
        print(f"sendDocument error: {error}", flush=True)
        return False


def keep_typing(chat_id: int, stop: threading.Event) -> None:
    """Show 'typing…' while Claude works."""
    while not stop.is_set():
        tg("sendChatAction", chat_id=chat_id, action="typing")
        stop.wait(4)


# ---- reply post-processing -----------------------------------------------------------------

def find_contact(state: dict[str, Any], name: str) -> int | None:
    """Chat id of a paired contact by (partial) name."""
    name = name.strip().lower()
    return next((int(cid) for cid, known in state.get("names", {}).items()
                 if known.lower() == name or (name and name in known.lower())), None)


def deliver_blocks(state: dict[str, Any], sender: str, chat_id: int, reply: str) -> str:
    """Handle <<<XABAR Name ... XABAR>>> (message a contact) and <<<FAYL name.ext ... FAYL>>> (send a file).

    The blocks are removed from the reply and replaced with a short delivery note.
    """
    pattern = re.compile(r"<<<(XABAR|FAYL)([^\n]*)\n(.*?)\1>>>", re.S)

    def handle_block(match: re.Match[str]) -> str:
        kind, header, body = match.group(1), match.group(2).strip(), match.group(3).strip()
        if kind == "FAYL":
            parts = header.split(maxsplit=1)
            filename = parts[0] if parts else "fayl.md"
            target = find_contact(state, parts[1]) if len(parts) > 1 else chat_id
            if target and body and send_document(target, filename, body + "\n"):
                return f"\n(📎 Fayl yuborildi: {filename})\n"
            return f"\n(⚠️ Fayl yuborilmadi: {filename})\n"

        target = find_contact(state, header)
        if target and body:
            send(target, f"📨 Xabar ({sender} bilan suhbatdan):\n\n{body}")
            return f"\n(✅ Xabar yuborildi: {header})\n"
        return f"\n(⚠️ Xabar yuborilmadi: \"{header}\" kontaktlar ro'yxatida yo'q)\n"

    return pattern.sub(handle_block, reply)


# ---- Claude --------------------------------------------------------------------------------

def contacts_prompt(state: dict[str, Any]) -> str:
    """Who is paired with the bot, so Claude can address messages and files."""
    lines = [f"- {name}{' (ustoz, egasi)' if int(cid) == state.get('owner') else ''}"
             for cid, name in state.get("names", {}).items()]
    return "\n\n## Telegram kontaktlar (botga ulangan odamlar)\n" + ("\n".join(lines) or "- (hali yo'q)")


def build_command(state: dict[str, Any], prompt: str) -> list[str]:
    """The claude -p call: memory MCP only, no shell, writes limited to the work folder."""
    command = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", MODEL,
        "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--append-system-prompt", RULES_FILE.read_text() + contacts_prompt(state),
    ]
    if state.get("session_id"):
        command += ["--resume", state["session_id"]]
    return command


def ask_claude(state: dict[str, Any], prompt: str) -> str:
    """Run one turn of the shared conversation and return the reply text."""
    process = subprocess.run(build_command(state, prompt), cwd=WORK, capture_output=True, text=True,
                             timeout=CLAUDE_TIMEOUT, stdin=subprocess.DEVNULL)
    try:
        output = json.loads(process.stdout)
    except json.JSONDecodeError:
        return f"Xatolik (claude):\n{(process.stderr or process.stdout)[-1500:]}"

    # A resume of a lost session (new volume, cleared state) starts over instead of failing forever
    if output.get("is_error") and state.get("session_id") and "session" in str(output.get("result", "")).lower():
        state["session_id"] = None
        save_state(state)
        return ask_claude(state, prompt)

    if output.get("session_id"):
        state["session_id"] = output["session_id"]
        save_state(state)
    return output.get("result") or f"Xatolik: {json.dumps(output)[:1500]}"


# ---- message handling ----------------------------------------------------------------------

def save_image(message: dict[str, Any]) -> Path | None:
    """Download a photo (or an image sent as a file) into the work folder."""
    if message.get("photo"):
        file_id, ext = message["photo"][-1]["file_id"], ".jpg"
    elif message.get("document", {}).get("mime_type", "").startswith("image/"):
        file_id = message["document"]["file_id"]
        ext = Path(message["document"].get("file_name", "")).suffix or ".png"
    else:
        return None
    file_path = tg("getFile", file_id=file_id).get("result", {}).get("file_path")
    if not file_path:
        return None
    data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=60).content
    UPLOADS.mkdir(parents=True, exist_ok=True)
    local = UPLOADS / f"{time.strftime('%Y%m%d_%H%M%S')}_{message['message_id']}{ext}"
    local.write_bytes(data)
    return local


def handle(state: dict[str, Any], message: dict[str, Any]) -> None:
    """Pairing, commands, then one Claude turn per message."""
    chat_id = message["chat"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    who = message["from"].get("first_name", "?")

    # Only paired chats; the first paired chat becomes the owner
    if chat_id not in state["chat_ids"]:
        if text in (PAIR_CODE, f"/start {PAIR_CODE}"):
            state["chat_ids"].append(chat_id)
            state.setdefault("names", {})[str(chat_id)] = who
            state.setdefault("owner", chat_id)
            save_state(state)
            send(chat_id, "Ulandingiz ✅ Endi oddiy yozavering.\n/new — yangi suhbat")
        return

    image = save_image(message)
    if image:
        text = f"{text}\n\n(Rasm yubordi: {image} — Read tool bilan ochib ko'ring.)".strip()
    if not text:
        send(chat_id, "Hozircha matn va rasmlarni tushunaman.")
        return
    if text == "/new":
        state["session_id"] = None
        save_state(state)
        send(chat_id, "Yangi suhbat boshlandi. Kontekst xotiradan (MCP) olinadi.")
        return

    stop = threading.Event()
    threading.Thread(target=keep_typing, args=(chat_id, stop), daemon=True).start()
    try:
        with lock:  # one shared conversation, so one message at a time
            reply = deliver_blocks(state, who, chat_id, ask_claude(state, f"[{who}]: {text}"))
    except subprocess.TimeoutExpired:
        reply = "Javob juda uzoq cho'zildi, to'xtatildi."
    except Exception as error:  # keep the bot alive on any failure
        reply = f"Xatolik: {error}"
    finally:
        stop.set()
    send(chat_id, reply)


def main() -> None:
    """Long-poll Telegram forever; each message is handled in its own thread."""
    WORK.mkdir(parents=True, exist_ok=True)
    write_mcp_config()
    state = load_state()
    save_state(state)
    print("bridge started", flush=True)

    while True:
        try:
            result = requests.post(f"{API}/getUpdates", json={"offset": state["offset"], "timeout": 60}, timeout=70).json()
        except requests.RequestException as error:
            print(f"poll error: {error}", flush=True)
            time.sleep(5)
            continue
        if not result.get("ok"):
            # 409 = another instance polls with the same token (stop the local bot)
            print(f"poll failed: {result.get('description')}", flush=True)
            time.sleep(10)
            continue
        for update in result.get("result", []):
            state["offset"] = update["update_id"] + 1
            save_state(state)
            if "message" in update:
                threading.Thread(target=handle, args=(state, update["message"]), daemon=True).start()


if __name__ == "__main__":
    main()
