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
import zipfile
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
BOT_NAME = os.environ.get("BOT_NAME", "Teresa Lisbon")
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
LONG_CODE_CHARS = 2500          # code blocks longer than this are sent as files, not chat messages
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
ZIP_MAX_TOTAL = 200 * 1024 * 1024
ZIP_MAX_FILES = 2000
CODE_EXT = {"python": "py", "py": "py", "sql": "sql", "json": "json", "bash": "sh", "sh": "sh", "yaml": "yaml",
            "yml": "yaml", "powerquery": "m", "m": "m", "dax": "dax", "csv": "csv", "xml": "xml", "html": "html",
            "javascript": "js", "js": "js", "ts": "ts", "markdown": "md", "md": "md", "text": "txt", "": "txt"}

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


def send_document(chat_id: int, name: str, content: str | bytes, caption: str = "") -> bool:
    """Send text or bytes as a file attachment (code, briefs, zips, images)."""
    data = content.encode() if isinstance(content, str) else content
    try:
        response = requests.post(f"{API}/sendDocument", data={"chat_id": chat_id, "caption": caption[:1000]},
                                 files={"document": (name, data)}, timeout=120)
        return bool(response.json().get("ok"))
    except requests.RequestException as error:
        print(f"sendDocument error: {error}", flush=True)
        return False


def work_path(raw: str) -> Path | None:
    """Resolve a path the model names; only files inside the work folder may be sent."""
    candidate = Path(raw.strip().strip("`'\""))
    candidate = (candidate if candidate.is_absolute() else WORK / candidate).resolve()
    return candidate if candidate.is_file() and candidate.is_relative_to(WORK.resolve()) else None


def keep_typing(chat_id: int, stop: threading.Event) -> None:
    """Show 'typing…' while Claude works."""
    while not stop.is_set():
        tg("sendChatAction", chat_id=chat_id, action="typing")
        stop.wait(4)


# ---- reply post-processing -----------------------------------------------------------------

def contact_names(contact: dict[str, Any]) -> list[str]:
    """Every name a contact can be called by: Telegram names, @username, learned aliases, role."""
    names = [contact.get("first_name"), contact.get("last_name"), contact.get("full_name"),
             contact.get("username") and f"@{contact['username']}", contact.get("role")]
    seen: dict[str, str] = {}
    for name in names + contact.get("aliases", []):
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def update_contact(state: dict[str, Any], chat_id: int, sender: dict[str, Any]) -> dict[str, Any]:
    """Refresh a contact's Telegram names from the message (identity is the chat id, names can change)."""
    contact = state.setdefault("contacts", {}).setdefault(str(chat_id), {"aliases": []})
    first, last = sender.get("first_name", ""), sender.get("last_name", "")
    contact.update(first_name=first, last_name=last, full_name=f"{first} {last}".strip(), username=sender.get("username", ""))
    if chat_id == state.get("owner"):
        contact["role"] = "ustoz"
    state.setdefault("names", {})[str(chat_id)] = contact["full_name"] or first or str(chat_id)
    return contact


def find_contact(state: dict[str, Any], name: str) -> int | None:
    """Chat id of a paired contact by id, any of its names/aliases/role (exact first, then partial)."""
    wanted = name.strip().lstrip("[").rstrip("]").strip().lower()
    if not wanted:
        return None
    if wanted.isdigit() and wanted in state.get("contacts", {}):
        return int(wanted)
    contacts = state.get("contacts", {})
    for exact in (True, False):
        for cid, contact in contacts.items():
            for known in contact_names(contact):
                known = known.lower()
                if (known == wanted) if exact else (wanted in known or known in wanted.split()):
                    return int(cid)
    return None


def apply_contact_block(state: dict[str, Any], from_owner: bool, header: str) -> str:
    """<<<KONTAKT who | ism: a, b | rol: r>>> - only the owner's messages may name people or give roles."""
    parts = [part.strip() for part in header.split("|")]
    target = find_contact(state, parts[0]) if parts else None
    if not from_owner:
        return "\n(⚠️ Kontakt ma'lumotini faqat ustoz xabari asosida o'zgartirish mumkin)\n"
    if target is None:
        return f"\n(⚠️ Kontakt topilmadi: {parts[0] if parts else '?'})\n"
    contact = state["contacts"][str(target)]
    for part in parts[1:]:
        key, _, value = part.partition(":")
        values = [v.strip() for v in value.split(",") if v.strip()]
        if key.strip().lower() in ("ism", "ismlar", "alias") and values:
            contact["aliases"] = list(dict.fromkeys(contact.get("aliases", []) + values))
        elif key.strip().lower() == "rol" and values and target != state.get("owner"):
            contact["role"] = values[0]
    save_state(state)
    return f"\n(👤 Kontakt yangilandi: {', '.join(contact_names(contact))})\n"


def deliver_blocks(state: dict[str, Any], sender: str, chat_id: int, reply: str) -> str:  # noqa: C901
    """Act on the reply's blocks, remove them and leave a short note in their place.

    <<<XABAR Name ... XABAR>>>          message a paired contact
    <<<FAYL name.ext [Name] ... FAYL>>>  send the text as a file
    <<<YUBOR path [Name]>>>             send an existing file from the work folder (any type)
    <<<KONTAKT who | ism: a, b | rol: r>>> remember names/role of a contact (only from the owner's messages)
    Code blocks longer than LONG_CODE_CHARS are also sent as files instead of chat text.
    """
    blocks = re.compile(r"<<<(XABAR|FAYL)([^\n]*)\n(.*?)\1>>>", re.S)

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

    def handle_send(match: re.Match[str]) -> str:
        parts = match.group(1).strip().split(maxsplit=1)
        path = work_path(parts[0]) if parts else None
        target = find_contact(state, parts[1]) if len(parts) > 1 else chat_id
        if path and target and send_document(target, path.name, path.read_bytes()):
            return f"\n(📎 Fayl yuborildi: {path.name})\n"
        return f"\n(⚠️ Fayl yuborilmadi: {parts[0] if parts else '?'} — ish papkasida topilmadi)\n"

    counter = iter(range(1, 1000))

    def handle_long_code(match: re.Match[str]) -> str:
        language, code = match.group(1).lower(), match.group(2)
        if len(code) <= LONG_CODE_CHARS:
            return match.group(0)
        name = f"code_{time.strftime('%H%M%S')}_{next(counter)}.{CODE_EXT.get(language, language or 'txt')}"
        if send_document(chat_id, name, code.rstrip() + "\n"):
            return f"\n(📎 Kod uzun bo'lgani uchun fayl qilib yuborildi: {name})\n"
        return match.group(0)

    from_owner = chat_id == state.get("owner")
    reply = re.sub(r"<<<KONTAKT([^\n>]*)>>>", lambda m: apply_contact_block(state, from_owner, m.group(1)), reply)
    reply = blocks.sub(handle_block, reply)
    reply = re.sub(r"<<<YUBOR([^\n>]*)>>>", handle_send, reply)
    return re.sub(r"```(\w*)\n(.*?)```", handle_long_code, reply, flags=re.S)


# ---- Claude --------------------------------------------------------------------------------

def contacts_prompt(state: dict[str, Any], current_chat: int | None = None) -> str:
    """Paired people by Telegram id with every known name and role, so Claude can recognise and address them."""
    lines = []
    for cid, contact in state.get("contacts", {}).items():
        role = "ustoz, egasi" if int(cid) == state.get("owner") else contact.get("role", "rol noma'lum")
        current = " ← hozir yozayotgan" if current_chat is not None and int(cid) == current_chat else ""
        lines.append(f"- id {cid}: {', '.join(dict.fromkeys(contact_names(contact)))} ({role}){current}")
    return ("\n\n## Telegram kontaktlar (botga ulangan odamlar; shaxs = Telegram id, nomlari har xil bo'lishi mumkin)\n"
            + ("\n".join(lines) or "- (hali yo'q)"))


def persona_prompt() -> str:
    """The name the bot owner chose; placed first so it frames the whole conversation."""
    return (
        f"# Rol\nBu Telegram botda siz bot egasi (ustoz) tanlagan ism bilan ishlaysiz: **{BOT_NAME}**. "
        f"Ismingizni so'rashsa yoki o'zingizni tanishtirsangiz, \"{BOT_NAME}\" deysiz (\"Claude\" emas). "
        "Bu bot egasining sozlamasi, xotirada alohida kelishuv izlash shart emas. "
        "Faqat kimdir jiddiy ravishda AI ekaningizni yoki qaysi modelga asoslanganingizni so'rasa, halol aytasiz: "
        "Anthropic'ning Claude modeliga asoslangan AI yordamchisiz.\n\n"
    )


def build_command(state: dict[str, Any], prompt: str, chat_id: int | None = None) -> list[str]:
    """The claude -p call: memory MCP only, no shell, writes limited to the work folder."""
    command = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", MODEL,
        "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--append-system-prompt", persona_prompt() + RULES_FILE.read_text() + contacts_prompt(state, chat_id),
    ]
    if state.get("session_id"):
        command += ["--resume", state["session_id"]]
    return command


def ask_claude(state: dict[str, Any], prompt: str, chat_id: int | None = None) -> str:
    """Run one turn of the shared conversation and return the reply text."""
    process = subprocess.run(build_command(state, prompt, chat_id), cwd=WORK, capture_output=True, text=True,
                             timeout=CLAUDE_TIMEOUT, stdin=subprocess.DEVNULL)
    try:
        output = json.loads(process.stdout)
    except json.JSONDecodeError:
        print(f"claude failed (exit {process.returncode}): {(process.stderr or process.stdout)[-500:]}", flush=True)
        return f"Xatolik (claude):\n{(process.stderr or process.stdout)[-1500:]}"

    # A resume of a lost session (new volume, cleared state) starts over instead of failing forever
    if output.get("is_error") and state.get("session_id") and "session" in str(output.get("result", "")).lower():
        state["session_id"] = None
        save_state(state)
        return ask_claude(state, prompt, chat_id)

    if output.get("is_error"):
        print(f"claude returned an error: {str(output.get('result'))[:500]}", flush=True)
    if output.get("session_id"):
        state["session_id"] = output["session_id"]
        save_state(state)
    return output.get("result") or f"Xatolik: {json.dumps(output)[:1500]}"


# ---- message handling ----------------------------------------------------------------------

def _download(file_id: str, size: int | None) -> bytes | None:
    """Download a Telegram file (bots can fetch up to 20 MB)."""
    if size and size > TELEGRAM_DOWNLOAD_LIMIT:
        return None
    file_path = tg("getFile", file_id=file_id).get("result", {}).get("file_path")
    if not file_path:
        return None
    return requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=120).content


def _extract_zip(archive: Path) -> tuple[Path, list[str]]:
    """Unpack a zip next to it, refusing unsafe paths and archive bombs; returns the folder and file list."""
    target = archive.with_suffix("")
    target.mkdir(exist_ok=True)
    names: list[str] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist()[:ZIP_MAX_FILES]:
            destination = (target / info.filename).resolve()
            if info.is_dir() or not destination.is_relative_to(target.resolve()):
                continue
            total += info.file_size
            if total > ZIP_MAX_TOTAL:
                break
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(zf.read(info))
            names.append(info.filename)
    return target, names


def _readable_copy(path: Path) -> Path | None:
    """Excel and Word files get a text copy the model can read (csv per sheet / plain text)."""
    try:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            import csv
            import openpyxl

            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            out = path.with_suffix(".sheets.txt")
            with out.open("w", newline="") as handle:
                for sheet in book.worksheets:
                    handle.write(f"### sheet: {sheet.title}\n")
                    writer = csv.writer(handle)
                    for row in sheet.iter_rows(values_only=True):
                        writer.writerow(["" if value is None else value for value in row])
            return out
        if path.suffix.lower() == ".docx":
            import docx

            out = path.with_suffix(".txt")
            out.write_text("\n".join(p.text for p in docx.Document(path).paragraphs))
            return out
    except Exception as error:  # a broken file must not stop the message
        print(f"convert failed for {path.name}: {error}", flush=True)
    return None


def save_attachment(message: dict[str, Any]) -> str:
    """Save any file the user sent (photo, document, zip, audio, video) and describe it for the model."""
    kinds = [("photo", None), ("document", None), ("video", ".mp4"), ("audio", ".mp3"), ("voice", ".ogg"),
             ("video_note", ".mp4"), ("animation", ".mp4")]
    for kind, default_ext in kinds:
        item = message.get(kind)
        if not item:
            continue
        if kind == "photo":
            item, default_ext = item[-1], ".jpg"  # last = largest size
        name = item.get("file_name") or f"{kind}{default_ext or ''}"
        data = _download(item["file_id"], item.get("file_size"))
        if data is None:
            return f"(Fayl yubordi: {name}, lekin u 20 MB dan katta yoki yuklab bo'lmadi — kichikroq qilib yoki zip qilib yuborishni so'rang.)"

        UPLOADS.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", Path(name).name) or "file"
        local = UPLOADS / f"{time.strftime('%Y%m%d_%H%M%S')}_{message['message_id']}_{safe}"
        local.write_bytes(data)
        note = f"(Fayl yubordi: {local} — Read tool bilan oching.)"

        if local.suffix.lower() == ".zip":
            try:
                folder, names = _extract_zip(local)
                shown = "\n".join(f"- {n}" for n in names[:60]) + (f"\n- ... yana {len(names) - 60} ta" if len(names) > 60 else "")
                note = f"(Zip yubordi, ochildi: {folder}\n{shown}\nKerakli fayllarni Read tool bilan oching.)"
            except zipfile.BadZipFile:
                note = f"(Zip yubordi: {local}, lekin arxiv buzuq.)"
        elif kind in ("audio", "voice", "video", "video_note", "animation"):
            note = f"(Audio/video yubordi: {local}. Uni eshitish/ko'rish imkoni yo'q — mazmunini matn bilan yozishini so'rang.)"
        else:
            readable = _readable_copy(local)
            if readable:
                note = f"(Fayl yubordi: {local}; o'qish uchun matn nusxasi: {readable} — Read tool bilan oching.)"
        return note

    if message.get("sticker"):
        return "(Stiker yubordi.)"
    if message.get("location"):
        return "(Joylashuv yubordi.)"
    if message.get("contact"):
        return "(Kontakt yubordi.)"
    return ""


def handle(state: dict[str, Any], message: dict[str, Any]) -> None:
    """Pairing, commands, then one Claude turn per message."""
    chat_id = message["chat"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    sender = message.get("from", {})
    who = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "?"

    # Only paired chats; the first paired chat becomes the owner. Others are asked for the code (never told it).
    if chat_id not in state["chat_ids"]:
        if text in (PAIR_CODE, f"/start {PAIR_CODE}"):
            state["chat_ids"].append(chat_id)
            state.setdefault("owner", chat_id)
            update_contact(state, chat_id, sender)
            save_state(state)
            print(f"paired chat {chat_id} ({who})", flush=True)
            send(chat_id, "Ulandingiz ✅ Endi oddiy yozavering.\n/new — yangi suhbat")
        else:
            print(f"unpaired message from chat {chat_id} ({who})", flush=True)
            send(chat_id, "Salom! Botdan foydalanish uchun ulanish kodini yuboring.")
        return

    update_contact(state, chat_id, sender)
    save_state(state)
    attachment = save_attachment(message)
    if attachment:
        text = f"{text}\n\n{attachment}".strip()
    if not text:
        send(chat_id, "Bu turdagi xabarni hali tushunmayman — matn yoki fayl yuboring.")
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
            reply = deliver_blocks(state, who, chat_id, ask_claude(state, f"[{who} | id {chat_id}]: {text}", chat_id))
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
    if tg("getMyName").get("result", {}).get("name") != BOT_NAME:  # Telegram rate-limits renames, so only when it differs
        tg("setMyName", name=BOT_NAME)
    state = load_state()
    for cid, name in state.get("names", {}).items():  # older state kept only first names
        state.setdefault("contacts", {}).setdefault(cid, {"first_name": name, "full_name": name, "aliases": []})
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
