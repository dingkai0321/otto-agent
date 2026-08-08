"""Knowledge RAG: ingest local documents and hybrid-search PostgreSQL/pgvector."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from psycopg.types.json import Jsonb


@dataclass
class IngestResult:
    document_id: str
    source: str
    chunks: int
    embedded: bool
    duplicate: bool = False


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        import openai

        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


def make_embedder(settings):
    key = settings.embedding_api_key or os.getenv("OPENAI_API_KEY", "")
    if not key and settings.provider == "openai":
        key = settings.api_key
    if not key:
        return None
    return OpenAIEmbedder(key, settings.embedding_model, settings.embedding_base_url)


def _read_segments(path: Path) -> list[tuple[str, dict]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF ingestion needs: pip install 'otto-agent[knowledge]'") from exc
        return [
            (page.extract_text() or "", {"page": index + 1})
            for index, page in enumerate(PdfReader(str(path)).pages)
        ]
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("DOCX ingestion needs: pip install 'otto-agent[knowledge]'") from exc
        text = "\n\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
        return [(text, {})]
    if suffix in {".txt", ".md", ".markdown", ".rst", ".csv", ".json"}:
        return [(path.read_text(encoding="utf-8"), {})]
    raise ValueError(f"Unsupported document type: {suffix or '(none)'}")


def _chunk(text: str, target: int = 1400, overlap: int = 180) -> list[str]:
    """Paragraph-aware chunks with a small overlap for boundary continuity."""
    clean = re.sub(r"[ \t]+", " ", text).strip()
    if not clean:
        return []
    chunks: list[str] = []
    cursor = 0
    while cursor < len(clean):
        end = min(cursor + target, len(clean))
        if end < len(clean):
            boundary = max(clean.rfind("\n", cursor + target // 2, end),
                           clean.rfind(". ", cursor + target // 2, end),
                           clean.rfind("。", cursor + target // 2, end))
            if boundary > cursor:
                end = boundary + 1
        piece = clean[cursor:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(clean):
            break
        cursor = max(end - overlap, cursor + 1)
    return chunks


class KnowledgeStore:
    def __init__(self, conn, settings, embedder=None):
        self.conn = conn
        self.settings = settings
        self.embedder = embedder

    def has_documents(self) -> bool:
        row = self.conn.execute("SELECT EXISTS (SELECT 1 FROM knowledge_documents) AS yes").fetchone()
        return bool(row["yes"])

    def ingest(self, file_path: str | Path) -> IngestResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = self.conn.execute(
            "SELECT id, source, chunk_count FROM knowledge_documents WHERE sha256=%s",
            (digest,),
        ).fetchone()
        if existing:
            return IngestResult(
                str(existing["id"]), existing["source"], existing["chunk_count"],
                embedded=self._document_is_embedded(str(existing["id"])), duplicate=True,
            )

        pieces: list[tuple[str, dict]] = []
        for segment, metadata in _read_segments(path):
            pieces.extend((content, metadata) for content in _chunk(segment))
        if not pieces:
            raise ValueError(f"No readable text found in {path.name}")

        vectors = self.embedder.embed([content for content, _ in pieces]) if self.embedder else []
        if vectors and any(len(vector) != self.settings.embedding_dimensions for vector in vectors):
            raise ValueError(
                f"Embedding model returned the wrong dimension; expected "
                f"{self.settings.embedding_dimensions}"
            )

        document_id = str(uuid.uuid4())
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with self.conn.transaction():
            self.conn.execute(
                """INSERT INTO knowledge_documents
                       (id, source, title, mime_type, sha256, chunk_count, metadata)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (document_id, str(path), path.stem, mime, digest, len(pieces),
                 Jsonb({"filename": path.name})),
            )
            for index, (content, metadata) in enumerate(pieces):
                vector = vectors[index] if vectors else None
                self.conn.execute(
                    """INSERT INTO knowledge_chunks
                           (document_id, chunk_index, content, token_count, embedding, metadata)
                         VALUES (%s, %s, %s, %s, %s, %s)""",
                    (document_id, index, content, max(1, len(content) // 4), vector,
                     Jsonb(metadata)),
                )
        return IngestResult(document_id, str(path), len(pieces), embedded=bool(vectors))

    def _document_is_embedded(self, document_id: str) -> bool:
        row = self.conn.execute(
            "SELECT bool_and(embedding IS NOT NULL) AS embedded "
            "FROM knowledge_chunks WHERE document_id=%s",
            (document_id,),
        ).fetchone()
        return bool(row and row["embedded"])

    def list(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, source, title, mime_type, sha256, chunk_count,
                      created_at, updated_at
                 FROM knowledge_documents ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, document_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM knowledge_documents WHERE id=%s", (document_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def search(self, query: str, top_k: int = 6) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        candidate_count = max(top_k * 4, 20)
        keyword = self.conn.execute(
            """SELECT c.id, c.chunk_index, c.content, c.metadata,
                      d.id AS document_id, d.source, d.title,
                      ts_rank_cd(c.search_vector, plainto_tsquery('simple', %s)) AS relevance
                 FROM knowledge_chunks c
                 JOIN knowledge_documents d ON d.id=c.document_id
                WHERE c.search_vector @@ plainto_tsquery('simple', %s)
                   OR c.content ILIKE %s
                ORDER BY relevance DESC, c.id
                LIMIT %s""",
            (query, query, f"%{query}%", candidate_count),
        ).fetchall()

        vector = []
        if self.embedder:
            try:
                query_vector = self.embedder.embed([query])[0]
                vector = self.conn.execute(
                    """SELECT c.id, c.chunk_index, c.content, c.metadata,
                              d.id AS document_id, d.source, d.title,
                              1 - (c.embedding <=> %s::vector) AS similarity
                         FROM knowledge_chunks c
                         JOIN knowledge_documents d ON d.id=c.document_id
                        WHERE c.embedding IS NOT NULL
                        ORDER BY c.embedding <=> %s::vector
                        LIMIT %s""",
                    (query_vector, query_vector, candidate_count),
                ).fetchall()
            except Exception:
                # An embeddings outage must not take away PostgreSQL keyword search.
                self.conn.rollback()
                vector = []

        # Reciprocal-rank fusion keeps lexical precision and semantic recall.
        combined: dict[int, dict] = {}
        for ranking, rows in (("keyword", keyword), ("vector", vector)):
            for rank, row in enumerate(rows, start=1):
                item = combined.setdefault(row["id"], {**dict(row), "score": 0.0, "matches": []})
                item["score"] += 1.0 / (60 + rank)
                item["matches"].append(ranking)
        return sorted(combined.values(), key=lambda item: item["score"], reverse=True)[:top_k]
