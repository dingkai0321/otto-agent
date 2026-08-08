"""Agent tool for citation-bearing hybrid search over imported documents."""

from __future__ import annotations

from pathlib import Path

from otto.tools.registry import Tool


def _citation(row: dict) -> str:
    source = Path(row["source"]).name
    page = (row.get("metadata") or {}).get("page")
    return f"{source}, p.{page}" if page else f"{source}, chunk {row['chunk_index'] + 1}"


def make_tool(store) -> Tool:
    def search_knowledge(query: str, top_k: int = 6) -> str:
        rows = store.search(query, max(1, min(int(top_k or 6), 12)))
        if not rows:
            return "No matching passages found in the imported knowledge base."
        return "\n\n".join(
            f"[{_citation(row)}]\n{row['content']}" for row in rows
        )

    return Tool(
        name="search_knowledge",
        description=(
            "Search the user's imported PDF, DOCX, Markdown, and text documents. "
            "Use for questions that may depend on those sources. Results include citations; "
            "cite the source in the answer and do not invent content beyond the passages."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "semantic search question"},
                "top_k": {"type": "integer", "description": "passages to return (default 6)"},
            },
            "required": ["query"],
        },
        fn=search_knowledge,
    )
