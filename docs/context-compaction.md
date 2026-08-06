# Context compaction

Otto uses a Claude Code-style layered strategy for the ephemeral prompt. This
is separate from long-term memory consolidation: PostgreSQL keeps the durable
chat log unchanged, while compaction changes only what the next model call sees.

## Pipeline

Before every LLM call, `ContextManager` applies the cheapest safe operation first:

1. **Tool-result budget** — the live prompt may contain at most
   `OTTO_TOOL_RESULT_BUDGET_CHARS` characters of observations. A single large
   result, or aggregate overflow, is written to
   `$OTTO_HOME/tool-results/*.txt`; the model receives a preview and path.
2. **Micro-compaction** — all but the newest
   `OTTO_COMPACT_KEEP_TOOL_RESULTS` tool results become recoverable file
   pointers. Tool-use/tool-result pairs remain adjacent.
3. **Pair-safe snip** — if a loop grows past `OTTO_COMPACT_MAX_MESSAGES`, an
   old prefix is archived under `$OTTO_HOME/transcripts/` and removed only at a
   safe message boundary. A `tool_result` is never left without its `tool_use`.
4. **Automatic summary** — input is estimated portably from UTF-8 bytes. When it
   reaches `context_window - max_output - safety_buffer`, the small model turns
   the old prefix into a continuation handoff and keeps a recent tail verbatim.
5. **Reactive recovery** — a provider error such as “maximum context length”
   forces compaction and retries the original LLM request exactly once. Failed
   summarization is capped; it cannot recurse forever.

The summary prompt preserves intent, constraints, decisions, identifiers, paths,
task state, evidence, errors, and next steps. Every summarized prefix is archived
as JSONL before replacement.

Active Skill instructions are protected separately from approximate summaries.
`load_skill` tool results are not micro-compacted; if their message is removed,
the exact SKILL.md is reinserted under `OTTO_SKILL_REINJECT_BUDGET_CHARS`.

## Controls

- `/context` reports the estimated size, model window, automatic threshold, and
  last successful compaction.
- `/compact` compacts current session history immediately.
- `/compact keep exact API names` passes optional instructions to the summarizer.

These work through Otto itself, so CLI and gateways share the same behavior. The
dashboard recognizes them as built-in commands before graph workflow discovery.

## Hooks

`PreCompact` runs before an LLM summary and may block it or rewrite the custom
instructions. `PostCompact` reports `completed` or `failed`, trigger
(`auto`, `manual`, or `reactive`), token estimates, archive path, and summary or
error. Both use the same HookManager as tools, permissions, tasks, and tracing.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `OTTO_CONTEXT_WINDOW_TOKENS` | `200000` | configured model input/output window |
| `OTTO_COMPACT_BUFFER_TOKENS` | `13000` | input safety reserve |
| `OTTO_TOOL_RESULT_BUDGET_CHARS` | `200000` | total live tool-output budget |
| `OTTO_COMPACT_KEEP_TOOL_RESULTS` | `3` | newest full observations retained |
| `OTTO_COMPACT_KEEP_MESSAGES` | `8` | recent messages kept after summary |
| `OTTO_COMPACT_MAX_MESSAGES` | `50` | deterministic message-count cap |
| `OTTO_COMPACT_SUMMARY_MAX_TOKENS` | `4096` | summary response ceiling |
| `OTTO_COMPACT_MAX_FAILURES` | `3` | consecutive summarizer failure cap |
| `OTTO_SKILL_REINJECT_BUDGET_CHARS` | `50000` | exact active-Skill protection budget |

Set `OTTO_CONTEXT_WINDOW_TOKENS` to the actual selected model window. The
portable estimator is deliberately conservative enough for mixed English/CJK,
but provider tokenizers remain authoritative; reactive recovery covers the gap.
