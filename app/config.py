"""Application settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for the memory server."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database (Railway sets DATABASE_URL automatically for its Postgres service)
    database_url: str = Field(default="postgresql://memory:memory@localhost:5432/memory")
    db_pool_max: int = 10

    # S3-compatible object storage: the source of truth for memories and the common snapshot
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "memory"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_create_bucket: bool = False
    s3_addressing_style: str = "path"  # Railway managed buckets: "virtual"

    # Pseudonymization keys: HMAC key makes tokens stable, Fernet key encrypts real values
    pseudo_hmac_key: str
    pseudo_enc_key: str

    # Access tokens: MCP clients (Claude, bot) and admin (uploads, rebuild)
    mcp_token: str
    admin_token: str

    # Embeddings (local ONNX model, no external API)
    embedding_enabled: bool = True
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    embedding_cache_dir: str | None = None
    embedding_threads: int = 2  # caps CPU and RAM peaks; 2 is enough for this load

    # HTTP
    allowed_hosts: str = "*"
    max_upload_mb: int = 200
    common_max_file_kb: int = 1024


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings object."""
    return Settings()
