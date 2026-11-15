# Telegram bot that runs Claude Code (subscription login via CLAUDE_CODE_OAUTH_TOKEN) with the work-memory MCP server.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-requests ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

WORKDIR /app
COPY bot.py rules.md ./

# Sessions (~/.claude), state and the work folder live on the Railway volume mounted at /data
ENV HOME=/data DATA_DIR=/data PYTHONUNBUFFERED=1 DISABLE_AUTOUPDATER=1
CMD ["python3", "bot.py"]
