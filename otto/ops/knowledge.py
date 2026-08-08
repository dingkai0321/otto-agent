"""CLI for importing and inspecting the PostgreSQL/pgvector knowledge base."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console

from otto.config import load_settings
from otto.db import connect
from otto.memory.knowledge import KnowledgeStore, make_embedder


def _store():
    settings = load_settings()
    settings.ensure_home()
    conn = connect(
        settings.home,
        database_url=settings.database_url,
        schema=settings.database_schema,
        embedding_dimensions=settings.embedding_dimensions,
    )
    return conn, KnowledgeStore(conn, settings, embedder=make_embedder(settings))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="otto knowledge")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="import PDF, DOCX, Markdown, or text files")
    add.add_argument("paths", nargs="+")
    sub.add_parser("list", help="list imported documents")
    search = sub.add_parser("search", help="run hybrid keyword + vector search")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=6)
    delete = sub.add_parser("delete", help="delete one document and its chunks")
    delete.add_argument("document_id")
    args = parser.parse_args(argv)

    console = Console()
    conn, store = _store()
    try:
        if args.action == "add":
            for raw in args.paths:
                result = store.ingest(Path(raw))
                mode = "vector + keyword" if result.embedded else "keyword only (no embedding key)"
                duplicate = "already imported" if result.duplicate else "imported"
                console.print(
                    f"[green]{duplicate}[/green] {Path(result.source).name}: "
                    f"{result.chunks} chunks, {mode}, id={result.document_id}"
                )
        elif args.action == "list":
            rows = store.list()
            if not rows:
                console.print("No documents imported.")
            for row in rows:
                console.print(
                    f"{row['id']}  {row['title']}  {row['chunk_count']} chunks  {row['source']}"
                )
        elif args.action == "search":
            rows = store.search(args.query, args.top_k)
            console.print_json(json.dumps(rows, default=str, ensure_ascii=False))
        elif args.action == "delete":
            if store.delete(args.document_id):
                console.print(f"[green]deleted[/green] {args.document_id}")
            else:
                console.print(f"No document with id {args.document_id}")
    finally:
        conn.close()
