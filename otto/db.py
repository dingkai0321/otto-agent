"""PostgreSQL + pgvector persistence for all structured Otto state.

One local PostgreSQL database holds chat history, memories, calendar events,
and document-RAG chunks.  A deterministic schema name derived from OTTO_HOME
keeps separate projects (and tests) isolated while sharing one server.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

DEFAULT_DATABASE_URL = "postgresql:///otto"
SCHEMA_VERSION = 2


class CompatRow(dict):
    """Mapping row that also supports concise integer indexing."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []

    def make_row(values):
        return CompatRow(zip(names, values, strict=True))

    return make_row


def schema_name(home: Path, explicit: str = "") -> str:
    """Return a safe, stable PostgreSQL schema for one OTTO_HOME."""
    if explicit:
        candidate = explicit.strip().lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", candidate):
            raise ValueError("OTTO_DATABASE_SCHEMA must be a valid PostgreSQL identifier")
        return candidate
    absolute = str(home.expanduser().resolve())
    digest = hashlib.sha256(absolute.encode()).hexdigest()[:16]
    if os.getenv("PYTEST_CURRENT_TEST"):
        return f"otto_test_{os.getpid()}_{digest}"
    return f"otto_{digest}"


def _schema_sql(dimensions: int) -> str:
    if not 1 <= dimensions <= 2000:
        raise ValueError("OTTO_EMBEDDING_DIMENSIONS must be between 1 and 2000")
    return f"""
CREATE TABLE IF NOT EXISTS otto_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    "end" TEXT,
    attendees TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (title, start)
);

CREATE TABLE IF NOT EXISTS facts (
    id BIGSERIAL PRIMARY KEY,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(subject, '') || ' ' || coalesce(content, ''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS facts_search_idx ON facts USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS episodes (
    id BIGSERIAL PRIMARY KEY,
    happened_at DATE NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(summary, ''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS episodes_search_idx ON episodes USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS episodes_happened_idx ON episodes (happened_at DESC);

CREATE TABLE IF NOT EXISTS chat_log (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    consolidated BOOLEAN NOT NULL DEFAULT false,
    session_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL DEFAULT 'cli',
    meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_session_idx ON chat_log (session_id, id);
CREATE INDEX IF NOT EXISTS chat_unconsolidated_idx
    ON chat_log (id) WHERE consolidated = false;

CREATE TABLE IF NOT EXISTS task_lists (
    id UUID PRIMARY KEY,
    scope_type TEXT NOT NULL DEFAULT 'session'
        CHECK (scope_type IN ('session', 'shared')),
    scope_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL PRIMARY KEY,
    task_list_id UUID NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active_form TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed', 'deleted')),
    owner TEXT,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_list_status_idx
    ON tasks (task_list_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_open_per_owner_idx
    ON tasks (task_list_id, owner)
    WHERE status = 'in_progress' AND owner IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocked_by_task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, blocked_by_task_id),
    CHECK (task_id <> blocked_by_task_id)
);
CREATE INDEX IF NOT EXISTS task_dependencies_reverse_idx
    ON task_dependencies (blocked_by_task_id, task_id);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    embedding vector({dimensions}),
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(content, ''))
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS knowledge_chunks_document_idx
    ON knowledge_chunks (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS knowledge_chunks_search_idx
    ON knowledge_chunks USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
"""


def _redact_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", url)


def connect(
    home: Path,
    check_same_thread: bool = True,
    *,
    database_url: str | None = None,
    schema: str | None = None,
    embedding_dimensions: int | None = None,
) -> psycopg.Connection:
    """Open a PostgreSQL connection and idempotently initialize its Otto schema.

    ``check_same_thread`` remains in the signature so existing gateway callers
    keep working; PostgreSQL connections don't have SQLite's thread-affinity
    switch and dashboard access is already serialized by its lock.
    """
    del check_same_thread
    url = database_url or os.getenv("OTTO_DATABASE_URL", DEFAULT_DATABASE_URL)
    target_schema = schema_name(home, schema or os.getenv("OTTO_DATABASE_SCHEMA", ""))
    dimensions = embedding_dimensions or int(os.getenv("OTTO_EMBEDDING_DIMENSIONS", "1536"))
    try:
        # Autocommit prevents long-lived agent/dashboard readers from sitting
        # "idle in transaction" and retaining table/schema locks. Multi-write
        # operations open explicit transaction() blocks at their own boundary.
        conn = psycopg.connect(url, row_factory=_row_factory, autocommit=True)
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(target_schema)))
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(target_schema))
        )
        conn.execute(_schema_sql(dimensions))
        vector_type = conn.execute(
            """SELECT format_type(a.atttypid, a.atttypmod) AS type
                 FROM pg_attribute a
                 JOIN pg_class c ON c.oid=a.attrelid
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() AND c.relname='knowledge_chunks'
                  AND a.attname='embedding'"""
        ).fetchone()["type"]
        if vector_type != f"vector({dimensions})":
            raise ValueError(
                f"Existing knowledge_chunks uses {vector_type}, but "
                f"OTTO_EMBEDDING_DIMENSIONS={dimensions}"
            )
        conn.execute(
            "INSERT INTO otto_meta (key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        register_vector(conn)
        return conn
    except Exception as exc:
        if "conn" in locals():
            conn.close()
        raise RuntimeError(
            f"Cannot initialize Otto PostgreSQL at {_redact_url(url)!r}: {exc}. "
            "Start PostgreSQL and ensure the database exists and pgvector is available."
        ) from exc
