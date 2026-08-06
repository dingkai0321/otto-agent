# Stack report — verified 2026-07-10

The brief (§7) asked for a research-then-verify pass before building. Result: every layer
below was installed together in one venv (Python 3.13, uv) and smoke-tested end-to-end.

## Chosen stack (and what was rejected)

| Layer | Choice | Verified version | Rejected & why |
|---|---|---|---|
| Loop / harness | Hand-rolled ~150-line loop on the Anthropic SDK | `anthropic 0.116.0` | PydanticAI (great, but hides the loop we're teaching); smolagents (HF experimental tier); PocketFlow (graph abstraction, not a loop); LangGraph via launch-DeepResearch-Backend (drags in the whole LangChain stack — see below) |
| Memory | Hand-built personal-memory stores on PostgreSQL full-text search | PostgreSQL 17.6 | mem0 / Letta / Zep-Graphiti hide the consolidation + retrieval-gating mechanics this repo teaches |
| Knowledge RAG | PostgreSQL + pgvector, HNSW + GIN hybrid retrieval | pgvector 0.8.1 | Chroma/Qdrant would add a second database service; a unified backend keeps metadata, permissions, text ranks, vectors, and citations transactional |
| Eval — deterministic | plain pytest assertions on tool calls | `pytest 9.1.1` | — (this side must NOT use an LLM; that's the point) |
| Eval — LLM-as-judge | DeepEval (GEval), pytest-native | `deepeval 4.0.8` | promptfoo: strong assertions but YAML-driven + Node CLI, less teachable in a Python repo |
| Tracing / LLM Ops | JSONL trace per run + OTel spans → Arize Phoenix locally | `arize-phoenix 17.23.0`, OTLP gRPC | Langfuse v3 self-host needs ~6 containers (web, worker, Postgres, ClickHouse, Redis, MinIO) — too heavy for clone-and-run. Langfuse **cloud** still works via the same OTel env toggle |
| Gateway | CLI REPL default; Discord and WhatsApp optional | extras `[discord]` (`discord.py >=2.4`) and `[whatsapp]` (`httpx >=0.25`) | — |

## Smoke tests run (all passing)

1. **PostgreSQL + pgvector**: schema initialization, GIN full-text indexes, HNSW cosine index,
   document ingest, and reciprocal-rank fusion all pass against a local PostgreSQL 17.6 instance.
2. **Phoenix + OTel**: `python -m phoenix.server.main serve` came up on `localhost:6006` in ~1s;
   a fake `agent_run → retrieval_gate → tool.create_event` span tree exported over OTLP
   (`localhost:4317`) and appeared via the Phoenix API (`traceCount: 1`).
3. **DeepEval**: custom deterministic `BaseMetric` (no LLM) measures and passes; `GEval`
   imports cleanly for the judge suite.
4. **Anthropic SDK**: imports at 0.116.0. Live tool-use round-trip pending an
   `ANTHROPIC_API_KEY` in `.env` (deliberately not sourced from other repos' secrets).

## Why not extend launch-DeepResearch-Backend's loop (brief asked)

Explored in depth: it is a LangGraph `StateGraph` fork of `open_deep_research` — supervisor
fan-out, research-specific state and prompts, hard LangChain coupling. The *pattern* worth
keeping (tools-by-name dict, parallel dispatch with `asyncio.gather`, safe-execute wrapper)
is reimplemented here in plain Python in `otto/loop/agent.py`.
