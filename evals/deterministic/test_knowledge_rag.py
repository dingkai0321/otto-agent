"""Deterministic PostgreSQL/pgvector tests for document knowledge RAG."""

from __future__ import annotations

from otto.config import Settings
from otto.db import connect
from otto.memory.knowledge import KnowledgeStore
from otto.tools.knowledge import make_tool


class FakeEmbedder:
    """Tiny semantic signal padded to the production 1536 dimensions."""

    def embed(self, texts):
        vectors = []
        for text in texts:
            low = text.lower()
            vector = [0.0] * 1536
            if "refund" in low or "money back" in low:
                vector[0] = 1.0
            else:
                vector[1] = 1.0
            vectors.append(vector)
        return vectors


def _store(tmp_path, embedder=None):
    settings = Settings(home=tmp_path, embedding_dimensions=1536)
    conn = connect(tmp_path)
    return conn, KnowledgeStore(conn, settings, embedder=embedder)


def test_text_ingestion_is_chunked_deduplicated_and_listed(tmp_path):
    path = tmp_path / "policy.txt"
    path.write_text("Refund requests are accepted within thirty days.\n\n" * 80)
    conn, store = _store(tmp_path, FakeEmbedder())

    first = store.ingest(path)
    second = store.ingest(path)

    assert first.chunks > 1 and first.embedded is True
    assert second.duplicate is True and second.document_id == first.document_id
    assert len(store.list()) == 1
    count = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    assert count == first.chunks


def test_hybrid_search_finds_semantic_paraphrase_and_returns_source(tmp_path):
    path = tmp_path / "returns.md"
    path.write_text("Refund requests require the original receipt.")
    _conn, store = _store(tmp_path, FakeEmbedder())
    store.ingest(path)

    rows = store.search("How do I get my money back?", top_k=3)

    assert rows and rows[0]["source"].endswith("returns.md")
    assert "Refund requests" in rows[0]["content"]
    assert "vector" in rows[0]["matches"]


def test_keyword_only_mode_works_without_embedding_credentials(tmp_path):
    path = tmp_path / "handbook.txt"
    path.write_text("The launch codename is Firefly.")
    _conn, store = _store(tmp_path)
    result = store.ingest(path)

    assert result.embedded is False
    assert store.search("Firefly")[0]["title"] == "handbook"


def test_docx_ingestion_and_citation_tool(tmp_path):
    from docx import Document

    path = tmp_path / "benefits.docx"
    document = Document()
    document.add_paragraph("Dental coverage begins after ninety days.")
    document.save(path)
    _conn, store = _store(tmp_path)
    store.ingest(path)

    output = make_tool(store).fn("Dental coverage")

    assert "[benefits.docx, chunk 1]" in output
    assert "ninety days" in output


def test_delete_cascades_to_chunks(tmp_path):
    path = tmp_path / "delete-me.txt"
    path.write_text("Temporary knowledge.")
    conn, store = _store(tmp_path)
    result = store.ingest(path)

    assert store.delete(result.document_id) is True
    assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 0
    assert store.delete(result.document_id) is False


def test_pgvector_hnsw_and_full_text_indexes_exist(tmp_path):
    conn, _store_obj = _store(tmp_path)
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=current_schema()"
    ).fetchall()
    indexes = {row["indexname"]: row["indexdef"].lower() for row in rows}

    assert "using hnsw" in indexes["knowledge_chunks_embedding_idx"]
    assert "using gin" in indexes["knowledge_chunks_search_idx"]
