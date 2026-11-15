-- Memory server schema. Postgres is the search index; S3 holds the source of truth.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Pseudonymization dictionary: terms to mask (names, company words, ids). Values are encrypted.
CREATE TABLE pseudo_terms (
    id               serial PRIMARY KEY,
    kind             text NOT NULL,
    value_enc        text NOT NULL,
    value_hmac       text NOT NULL UNIQUE,
    match_mode       text NOT NULL DEFAULT 'word' CHECK (match_mode IN ('word', 'substring', 'regex')),
    case_insensitive boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Every surface form seen in text gets one stable token (KIND_<10 hex>); the real value is encrypted.
CREATE TABLE pseudo_tokens (
    token      text PRIMARY KEY,
    kind       text NOT NULL,
    value_enc  text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Memories: episodic (what happened), semantic (facts, decisions, people), procedural (rules, lessons).
CREATE TABLE memories (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL CHECK (kind IN ('episodic', 'semantic', 'procedural')),
    title         text NOT NULL,
    body          text NOT NULL,
    ticket        text,
    tags          text[] NOT NULL DEFAULT '{}',
    source        text,
    importance    smallint NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    archive_note  text,
    superseded_by uuid REFERENCES memories (id),
    revision      int NOT NULL DEFAULT 1,
    content_hash  text NOT NULL,
    s3_key        text NOT NULL,
    embedding     vector(384),
    tsv           tsvector
);

CREATE TABLE memory_revisions (
    memory_id  uuid NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    revision   int NOT NULL,
    title      text NOT NULL,
    body       text NOT NULL,
    tags       text[] NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, revision)
);

-- Read-only snapshot of shared code/repos ("common"), split into line chunks for search.
CREATE TABLE common_files (
    id          serial PRIMARY KEY,
    collection  text NOT NULL,
    path        text NOT NULL,
    sha256      text NOT NULL,
    size_bytes  int NOT NULL,
    line_count  int NOT NULL,
    s3_key      text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (collection, path)
);

CREATE TABLE common_chunks (
    id         bigserial PRIMARY KEY,
    file_id    int NOT NULL REFERENCES common_files (id) ON DELETE CASCADE,
    chunk_no   int NOT NULL,
    start_line int NOT NULL,
    end_line   int NOT NULL,
    content    text NOT NULL,
    embedding  vector(384),
    tsv        tsvector
);

-- Full-text vectors are kept by triggers ('simple' config: no stemming, works for mixed languages)
CREATE FUNCTION memories_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A')
            || setweight(to_tsvector('simple', coalesce(NEW.ticket, '')), 'A')
            || setweight(to_tsvector('simple', array_to_string(NEW.tags, ' ')), 'B')
            || setweight(to_tsvector('simple', NEW.body), 'C');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER memories_tsv BEFORE INSERT OR UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_tsv_trigger();

CREATE FUNCTION common_chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('simple', regexp_replace(NEW.content, '[_./\\-]', ' ', 'g'));
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER common_chunks_tsv BEFORE INSERT OR UPDATE ON common_chunks
    FOR EACH ROW EXECUTE FUNCTION common_chunks_tsv_trigger();

-- Indexes
CREATE INDEX memories_tsv_idx ON memories USING gin (tsv);
CREATE INDEX memories_embedding_idx ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX memories_ticket_idx ON memories (ticket);
CREATE INDEX memories_kind_status_idx ON memories (kind, status);
CREATE INDEX memories_tags_idx ON memories USING gin (tags);
CREATE INDEX memories_hash_idx ON memories (content_hash);
CREATE INDEX common_chunks_tsv_idx ON common_chunks USING gin (tsv);
CREATE INDEX common_chunks_embedding_idx ON common_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX common_chunks_file_idx ON common_chunks (file_id, chunk_no);
