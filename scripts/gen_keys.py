"""Print fresh secrets for .env (run once per environment; keep PSEUDO_* keys forever)."""

import secrets

from cryptography.fernet import Fernet

print(f"PSEUDO_HMAC_KEY={secrets.token_hex(32)}")
print(f"PSEUDO_ENC_KEY={Fernet.generate_key().decode()}")
print(f"MCP_TOKEN={secrets.token_urlsafe(32)}")
print(f"ADMIN_TOKEN={secrets.token_urlsafe(32)}")
print(f"WORK_TOKEN={secrets.token_urlsafe(32)}")
