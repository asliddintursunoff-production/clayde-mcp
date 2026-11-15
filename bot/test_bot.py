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

STATE = {"names": {"1": "Ustoz", "2": "Shogird"}, "owner": 1, "chat_ids": [1, 2]}


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
