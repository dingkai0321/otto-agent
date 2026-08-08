"""Configuration — every knob is an env var, documented in .env.example.

No settings framework: a dataclass read once at startup. If you can read this
file, you know everything Otto can be configured to do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _load_env() -> str:
    """Find the user's .env the way the user expects: from where they ARE.

    Bare `load_dotenv()` searches upward from the file that called it, not from
    the working directory. Inside a git checkout that is invisible — config.py
    lives in the project, so walking up from it lands on the project's .env and
    everything works. Installed from PyPI it walks up from site-packages,
    reaches the filesystem root, and finds nothing: the user stands in a folder
    holding a perfectly good .env and Otto reports "No API key". Reported from
    a clean install on 2026-07-31, from inside the repo folder itself.

    usecwd=True is the whole fix. The walk upward is kept on purpose, so
    running `otto` from a subdirectory of your project still finds the .env at
    its root — the same rule git, npm and pytest already taught everyone.

    Returns the path that was loaded (empty string if none) so `otto doctor`
    and the first-run error can say WHICH file was read, rather than leaving
    people guessing between three .env files.
    """
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path


DOTENV_PATH = _load_env()


@dataclass
class Settings:
    # --- LLM: pick a provider, set its key. See otto/loop/models.py PROVIDERS.
    provider: str = field(default_factory=lambda: os.getenv("OTTO_PROVIDER", "anthropic"))
    # Explicit overrides (optional): key, endpoint, and model ids. Left empty,
    # the provider's own key env var and default models are used.
    api_key: str = field(default_factory=lambda: os.getenv("OTTO_API_KEY", ""))
    base_url: str | None = field(default_factory=lambda: os.getenv("OTTO_BASE_URL") or None)
    model: str = field(default_factory=lambda: os.getenv("OTTO_MODEL", ""))
    # Cheap model used by the retrieval gate and the consolidation summarizer.
    small_model: str = field(default_factory=lambda: os.getenv("OTTO_SMALL_MODEL", ""))

    # --- Home: where Otto keeps SOUL, skills, calendar exports, outbox and traces.
    # Structured state lives in PostgreSQL; each home maps to an isolated schema.
    home: Path = field(default_factory=lambda: Path(os.getenv("OTTO_HOME", ".otto")))
    # Filesystem boundary used by the tool permission pipeline. Relative file
    # operations resolve here; access outside it requires explicit approval.
    workspace: Path = field(
        default_factory=lambda: Path(os.getenv("OTTO_WORKSPACE", str(Path.cwd())))
    )
    permission_timeout: int = field(
        default_factory=lambda: int(os.getenv("OTTO_PERMISSION_TIMEOUT", "120"))
    )

    # --- PostgreSQL: the one structured-data backend (memory, chat, calendar, RAG).
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "OTTO_DATABASE_URL", "postgresql:///otto"
        )
    )
    # Optional stable override. When omitted, db.py derives one from OTTO_HOME.
    database_schema: str = field(default_factory=lambda: os.getenv("OTTO_DATABASE_SCHEMA", ""))

    # --- Loop guardrails
    max_iterations: int = field(default_factory=lambda: int(os.getenv("OTTO_MAX_ITERATIONS", "10")))
    # Headroom matters for REASONING models (kimi-k3, gpt-5.x, gemini-*-pro):
    # they spend output tokens thinking before the answer, so a low cap makes
    # them hit stop_reason=max_tokens mid-thought and return an EMPTY reply
    # (watched kimi-k3 do exactly that at 2048). 8192 leaves room to think AND
    # answer; it's a ceiling, not a target, so efficient models still cost the same.
    max_tokens: int = field(default_factory=lambda: int(os.getenv("OTTO_MAX_TOKENS", "8192")))
    # Working memory is a SLIDING WINDOW (like context RAM): only the last N
    # turns go into the prompt. Older turns aren't lost — they're in PostgreSQL,
    # distilled into facts by consolidation, and pulled back by the retrieval
    # gate when relevant. Without this cap a long-running gateway session
    # resends its whole history every turn until it explodes.
    history_turns: int = field(default_factory=lambda: int(os.getenv("OTTO_HISTORY_TURNS", "12")))
    # Adaptive context compaction. The trigger reserves both output capacity
    # and a safety buffer: input >= window - max_tokens - buffer.
    context_window_tokens: int = field(
        default_factory=lambda: int(os.getenv("OTTO_CONTEXT_WINDOW_TOKENS", "200000"))
    )
    compact_buffer_tokens: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_BUFFER_TOKENS", "13000"))
    )
    # Shared prompt budget for tool observations. Overflow is persisted under
    # OTTO_HOME/tool-results and replaced by a preview/pointer.
    tool_result_budget_chars: int = field(
        default_factory=lambda: int(os.getenv("OTTO_TOOL_RESULT_BUDGET_CHARS", "200000"))
    )
    compact_keep_tool_results: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_KEEP_TOOL_RESULTS", "3"))
    )
    compact_keep_messages: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_KEEP_MESSAGES", "8"))
    )
    compact_max_messages: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_MAX_MESSAGES", "50"))
    )
    compact_summary_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_SUMMARY_MAX_TOKENS", "4096"))
    )
    compact_max_failures: int = field(
        default_factory=lambda: int(os.getenv("OTTO_COMPACT_MAX_FAILURES", "3"))
    )
    skill_reinject_budget_chars: int = field(
        default_factory=lambda: int(os.getenv("OTTO_SKILL_REINJECT_BUDGET_CHARS", "50000"))
    )

    # --- Memory
    # Consolidate (distill chats into durable facts) only after N new exchanges.
    consolidate_every: int = field(default_factory=lambda: int(os.getenv("OTTO_CONSOLIDATE_EVERY", "6")))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("OTTO_RETRIEVAL_TOP_K", "4")))
    # PostgreSQL is the default. Notion remains an optional mirror/backend for episodes.
    episodic_store: str = field(default_factory=lambda: os.getenv("OTTO_EPISODIC_STORE", "postgres"))
    embedding_model: str = field(
        default_factory=lambda: os.getenv("OTTO_EMBEDDING_MODEL", "text-embedding-3-small")
    )
    embedding_dimensions: int = field(
        default_factory=lambda: int(os.getenv("OTTO_EMBEDDING_DIMENSIONS", "1536"))
    )
    embedding_api_key: str = field(
        default_factory=lambda: os.getenv("OTTO_EMBEDDING_API_KEY", "")
    )
    embedding_base_url: str | None = field(
        default_factory=lambda: os.getenv("OTTO_EMBEDDING_BASE_URL") or None
    )

    # --- Tools
    # Sync created events into Apple Calendar (a dedicated "Otto" calendar)
    # via AppleScript. Opt-in because it writes to your real calendar app.
    apple_calendar: bool = field(
        default_factory=lambda: os.getenv("OTTO_APPLE_CALENDAR", "") in ("1", "true", "yes")
    )
    # Mirror locally-created events to Google Calendar. PostgreSQL + ICS remain the
    # source of truth; this is only an opt-in write target.
    google_calendar: bool = field(
        default_factory=lambda: os.getenv("OTTO_GOOGLE_CALENDAR", "") in ("1", "true", "yes")
    )
    google_calendar_id: str = field(
        default_factory=lambda: os.getenv("OTTO_GOOGLE_CALENDAR_ID", "") or "primary"
    )
    # Give the agent read/write access to Apple Calendar, Mail, Reminders, Notes
    # (macOS; first use triggers the system Automation permission prompts).
    apple_tools: bool = field(
        default_factory=lambda: os.getenv("OTTO_APPLE_TOOLS", "") in ("1", "true", "yes")
    )
    # Read-only GitHub access through the `gh` CLI's own auth (no token here).
    # Off by default and deliberately so: every registered tool ships in every
    # prompt, and reading PRs is maintainer capability, not assistant capability.
    # The gather workflow calls otto/tools/github.py as a library and does NOT
    # need this on — the switch only decides whether the MODEL can reach it.
    gh_tool: bool = field(
        default_factory=lambda: os.getenv("OTTO_GH_TOOL", "") in ("1", "true", "yes")
    )
    # owner/name to assume when a call omits it — for when Otto runs outside a
    # checkout, where `gh` has no remote to infer from.
    gh_repo: str = field(default_factory=lambda: os.getenv("OTTO_GH_REPO", ""))
    # Register the experimental tools (delegate_task -> pi sub-agent, ...). Env is
    # the global switch; the arena sets this per-race so a coding race can hand
    # work to pi WITHOUT flipping it on for the whole process.
    experimental: bool = field(
        default_factory=lambda: os.getenv("OTTO_EXPERIMENTAL", "") in ("1", "true", "yes")
    )
    # Route every message through the triage graph workflow first (a small model
    # classifies it; trivial messages get a fast small-model reply, real tasks
    # run the normal loop as a graph node). Any failure anywhere fails open to
    # the plain loop, so this can never make Otto worse — only faster/cheaper.
    graph_workflows: bool = field(
        default_factory=lambda: os.getenv("OTTO_GRAPH_WORKFLOWS", "") in ("1", "true", "yes")
    )

    # --- Optional gateway
    whatsapp_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_TOKEN", ""))
    whatsapp_phone_number_id: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    )

    # --- Tracing (JSONL always; OTel exports if an endpoint is set)
    otel_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "traces").mkdir(exist_ok=True)
        (self.home / "outbox").mkdir(exist_ok=True)
        (self.home / "tool-results").mkdir(exist_ok=True)
        (self.home / "transcripts").mkdir(exist_ok=True)
        return self.home


def load_settings() -> Settings:
    return Settings()
