"""Unit tests for the bot's pure helpers (no Telegram, no Claude)."""

import os
import sys
from pathlib import Path

os.environ.update(TELEGRAM_TOKEN="x", PAIR_CODE="p", MEMORY_MCP_URL="http://m/mcp", MCP_TOKEN="t")
sys.path.insert(0, str(Path(__file__).parent))
import bot  # noqa: E402

sent: list[tuple] = []
bot.send = lambda chat, text: sent.append(("msg", chat, text))
bot.send_document = lambda chat, name, content: sent.append(("doc", chat, name, content)) or True

STATE = {"owner": 1, "chat_ids": [1, 2], "contacts": {
    "1": {"first_name": "Ustoz", "full_name": "Ustoz", "aliases": [], "role": "ustoz"},
    "2": {"first_name": "Shogird", "full_name": "Shogird", "aliases": []}}}


def test_blocks_are_delivered_and_removed() -> None:
    sent.clear()
    reply = "Done.\n<<<XABAR shogird\nRun cell 3.\nXABAR>>>\n<<<FAYL brief.md Shogird\n# Task\nstep 1\nFAYL>>>\nEnd."
    out = bot.deliver_blocks(STATE, "Ustoz", 1, reply)
    assert "XABAR" not in out and "FAYL" not in out and "Xabar yuborildi" in out and "Fayl yuborildi: brief.md" in out
    assert sent[0][:2] == ("msg", 2) and sent[1][:3] == ("doc", 2, "brief.md")


def test_file_defaults_to_current_chat_and_unknown_contact_is_reported() -> None:
    sent.clear()
    out = bot.deliver_blocks(STATE, "Ustoz", 1, "<<<FAYL a.sql\nSELECT 1\nFAYL>>><<<XABAR Nobody\nhi\nXABAR>>>")
    assert sent == [("doc", 1, "a.sql", "SELECT 1\n")] and "yo'q" in out


def test_code_blocks_become_pre() -> None:
    chunks = bot.to_html_chunks("Text **bold**\n```sql\nSELECT * FROM t WHERE a < 1\n```\nafter")
    joined = "".join(chunks)
    assert "<b>bold</b>" in joined and '<pre><code class="language-sql">' in joined and "&lt; 1" in joined


def test_command_restricts_tools_and_resumes() -> None:
    command = bot.build_command({"session_id": "abc", "names": {}}, "hi")
    assert "--strict-mcp-config" in command and "Bash" in command[command.index("--disallowedTools"):]
    assert command[-2:] == ["--resume", "abc"] and "mcp__work-memory" in command


def test_unpaired_chat_is_asked_for_code_and_code_pairs() -> None:
    sent.clear()
    state = {"chat_ids": [], "names": {}, "session_id": None, "offset": 0}
    bot.save_state = lambda st: None
    message = {"chat": {"id": 7}, "from": {"first_name": "New"}, "text": "/start", "message_id": 1}
    bot.handle(state, message)
    assert sent[-1][:2] == ("msg", 7) and "kodini" in sent[-1][2] and bot.PAIR_CODE not in sent[-1][2]
    bot.handle(state, {**message, "text": bot.PAIR_CODE})
    assert state["chat_ids"] == [7] and state["owner"] == 7 and "Ulandingiz" in sent[-1][2]
    assert state["contacts"]["7"]["role"] == "ustoz"


def test_long_code_goes_as_file_short_code_stays() -> None:
    sent.clear()
    long_sql = "SELECT 1;\n" * 400
    out = bot.deliver_blocks(STATE, "Ustoz", 1, f"Short:\n```sql\nSELECT 2;\n```\nLong:\n```sql\n{long_sql}```")
    assert "SELECT 2;" in out and "fayl qilib yuborildi" in out and "SELECT 1;" not in out
    assert sent[-1][0] == "doc" and sent[-1][2].endswith(".sql")


def test_send_existing_file_only_inside_work(tmp_path=None) -> None:
    import tempfile
    sent.clear()
    bot.WORK = Path(tempfile.mkdtemp())
    (bot.WORK / "report.csv").write_text("a,b\n1,2\n")
    outside = Path(tempfile.mkdtemp()) / "secret.txt"
    outside.write_text("x")
    out = bot.deliver_blocks(STATE, "Ustoz", 1, f"<<<YUBOR report.csv>>> and <<<YUBOR {outside}>>>")
    assert sent == [("doc", 1, "report.csv", b"a,b\n1,2\n")]
    assert "Fayl yuborildi: report.csv" in out and "topilmadi" in out


def test_zip_is_extracted_safely_and_excel_is_converted() -> None:
    import io, tempfile, zipfile, openpyxl
    bot.WORK = Path(tempfile.mkdtemp())
    bot.UPLOADS = bot.WORK / "uploads"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("notes/a.md", "# hello")
        zf.writestr("../../evil.txt", "nope")
    book = openpyxl.Workbook(); book.active.append(["col", 1]); xbuf = io.BytesIO(); book.save(xbuf)
    files = {"z": buffer.getvalue(), "x": xbuf.getvalue()}
    bot._download = lambda file_id, size: files[file_id]

    note = bot.save_attachment({"message_id": 5, "document": {"file_id": "z", "file_name": "pack.zip"}})
    assert "notes/a.md" in note and "evil" not in note
    assert not (bot.WORK.parent / "evil.txt").exists() and not any(bot.WORK.rglob("evil.txt"))

    note = bot.save_attachment({"message_id": 6, "document": {"file_id": "x", "file_name": "data.xlsx"}})
    copy = Path(note.split("matn nusxasi: ")[1].split(" —")[0])
    assert "col,1" in copy.read_text()


def test_unknown_or_large_files_are_explained() -> None:
    bot._download = lambda file_id, size: None
    assert "20 MB" in bot.save_attachment({"message_id": 7, "document": {"file_id": "big", "file_name": "huge.zip", "file_size": 10**9}})
    assert bot.save_attachment({"message_id": 8, "sticker": {"file_id": "s"}}) == "(Stiker yubordi.)"


def test_prompt_carries_bot_name() -> None:
    command = bot.build_command({"session_id": None, "names": {}}, "hi")
    prompt = command[command.index("--append-system-prompt") + 1]
    assert prompt.startswith("# Rol") and "**Teresa Lisbon**" in prompt


def test_contacts_by_id_names_and_learned_aliases() -> None:
    import copy
    bot.save_state = lambda st: None
    state = copy.deepcopy(STATE)
    bot.update_contact(state, 2, {"first_name": "Oybek", "last_name": "Xushnazarov", "username": "oybek_x"})
    assert bot.find_contact(state, "Xushnazarov") == 2 and bot.find_contact(state, "@oybek_x") == 2
    assert bot.find_contact(state, "2") == 2 and bot.find_contact(state, "Ustoz") == 1

    # the apprentice cannot rename himself or take a role; the owner can teach aliases and roles
    note = bot.deliver_blocks(state, "Oybek", 2, "<<<KONTAKT 2 | rol: ustoz>>>")
    assert "faqat ustoz" in note and state["contacts"]["2"].get("role") is None
    bot.deliver_blocks(state, "Ustoz", 1, "ok <<<KONTAKT Oybek | ism: hushnazarov, Oybek aka | rol: shogird>>>")
    assert bot.find_contact(state, "shogird") == 2 and bot.find_contact(state, "hushnazarov") == 2
    prompt = bot.contacts_prompt(state, 2)
    assert "id 2: Oybek, Xushnazarov" in prompt and "(shogird) ← hozir yozayotgan" in prompt


def test_owner_connects_a_waiting_chat_without_the_code() -> None:
    import copy
    sent.clear()
    bot.save_state = lambda st: None
    state = copy.deepcopy(STATE)
    knock = {"chat": {"id": 9}, "from": {"first_name": "Oybek", "last_name": "Xushnazarov", "username": "oybek_x"},
             "text": "/start", "message_id": 1}

    # the newcomer is kept waiting, the owner is told once, the code is never revealed
    bot.handle(state, knock)
    bot.handle(state, {**knock, "text": "salom"})
    notices = [m for m in sent if m[1] == 1]
    assert 9 not in state["chat_ids"] and "9" in state["pending"] and len(notices) == 1 and "/add 9" in notices[0][2]
    assert all(bot.PAIR_CODE not in m[2] for m in sent if m[1] == 9)
    assert "id 9: Oybek Xushnazarov @oybek_x" in bot.contacts_prompt(state)

    # only the owner can connect; Claude's ULASH block from the owner's turn connects by name
    assert "faqat ustoz" in bot.deliver_blocks(state, "Shogird", 2, "<<<ULASH 9>>>")
    out = bot.deliver_blocks(state, "Ustoz", 1, "ok <<<ULASH Xushnazarov>>> <<<KONTAKT 9 | rol: shogird>>>")
    assert "Ulandi" in out and 9 in state["chat_ids"] and "9" not in state["pending"]
    assert state["contacts"]["9"]["role"] == "shogird" and sent[-1][:2] == ("msg", 9) and "Ulandingiz" in sent[-1][2]

    # /add for someone who never wrote explains what to do
    bot.handle(state, {"chat": {"id": 1}, "from": {"first_name": "Ustoz"}, "text": "/add 12345", "message_id": 2})
    assert "avval botga" in sent[-1][2]
