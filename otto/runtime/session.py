"""Ephemeral Agent Run — assembles working memory for each turn.

The inner box on the whiteboard: everything here is rebuilt per run and thrown
away. What persists lives in otto/memory. Working memory =

    system prompt (SOUL.md)            ← who Otto is
  + durable facts & episodes           ← what Otto remembers (gated!)
  + current chat history               ← this conversation
  + the user's new message
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from otto.config import Settings
from otto.hooks import HookManager
from otto.runtime.status import timestamp_event

DEFAULT_SOUL = """\
You are Otto, a personal assistant running locally on your user's laptop.
You are concise, warm, and proactive. You remember what your user tells you.

Rules:
- When the user wants to schedule something, use create_event. Resolve relative
  dates and times ("next Tuesday", "in 30 minutes") to ISO timestamps yourself;
  the current date and time are given below — trust them, never ask the user
  what time it is.
- When the user asks what's on their calendar (a day, a week, "yesterday"), use
  list_events — you CAN read the calendar, not just write to it.
- When the user shares something durable about a person, project, or preference,
  use save_note to remember it.
- When asked to message someone, use send_message (it drafts to a local outbox).
- If memory context is provided below, trust it — it came from your own store.
- Call each tool at most once per request. Your history shows [tools used: ...]
  lines for past turns — if a tool already ran, do NOT run it again; answer
  from that record instead.
- Be honest about where things live. Every tool's output states exactly where
  its artifact landed (local calendar file, Apple Calendar, memory database at
  PostgreSQL) — relay that truthfully, and never claim something synced
  anywhere the tool output doesn't say.
- For complex work with three or more meaningful steps, create durable tasks.
  Keep at most one task in_progress per owner, add dependency edges when order
  matters, and mark a task completed only after its result is verified. Skip
  task tracking for short, single-step requests.
- Use agent_spawn for a bounded research, planning, or review subtask when a
  fresh context materially helps. Give it a self-contained prompt because it
  cannot see this conversation. Bind a pending task_id when delegating tracked
  work. Do not spawn a child for simple work you can complete directly.
- You can manage your own memory: use manage_memory to correct or forget facts,
  update_soul to save a standing preference the user gives you, and create_skill
  to save a repeatable workflow the user teaches you (only after they say yes).
- Skills use progressive disclosure. Load a relevant skill before relying on
  specialist tools, and read only the referenced resource needed for the task.
"""


def load_soul(settings: Settings) -> str:
    """SOUL.md is the editable persona file, created on first run. Changing it
    changes who your Otto is — that's procedural memory at its simplest."""
    soul_path = settings.home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(DEFAULT_SOUL, encoding="utf-8")
    return soul_path.read_text(encoding="utf-8")


class Session:
    """Holds one conversation: the chat history plus the recipe for the
    system prompt. One Session per gateway connection."""

    def __init__(self, settings: Settings, memory=None, session_id: str = "default"):
        self.settings = settings
        self.memory = memory  # otto.memory.Memory (None until Phase-2 wiring)
        self.session_id = session_id
        self.history: list[dict] = []
        self.tool_counts: Counter[str] = Counter()
        from otto.runtime.prompt import PromptAssembler

        self.prompt = PromptAssembler(settings, memory)

    def build_prompt(self, user_message: str,
                     hooks: HookManager | None = None):
        """Return the stable system prefix and dynamic per-turn context.

        Callers must keep these sections separate: ``bundle.system`` goes to
        the model's system field, while ``bundle.dynamic_context`` is injected
        at the trajectory tail, immediately before Agent Status, for this
        request only.
        """
        return self.prompt.build(user_message, load_soul(self.settings), hooks=hooks)

    def build_system(self, user_message: str, hooks: HookManager | None = None) -> str:
        """Build only the stable SOUL + Skill-catalog system prompt."""
        return self.build_prompt(user_message, hooks=hooks).system

    def add_exchange(self, user_message: str, reply: str, tool_calls: list | None = None,
                     source: str = "cli", meta: dict | None = None,
                     occurred_at: datetime | str | None = None) -> None:
        """Record the turn in history (working memory) and, if memory is wired,
        in the chat log (so consolidation can distill it later).

        Tool activity is folded into the assistant's history entry as a compact
        [tools used: ...] line. Without it, the model forgets it already acted
        and happily re-runs the same tool next turn (the triple-booked-meeting
        bug from the first live test)."""
        record = reply
        if tool_calls:
            summary = "; ".join(f"{c['tool']}({c['args']}) -> {c['output']}" for c in tool_calls)
            record = f"{reply}\n[tools used: {summary}]"
        self.history.append({
            "role": "user", "content": timestamp_event(user_message, occurred_at)
        })
        self.history.append({"role": "assistant", "content": record})
        self.tool_counts.update(call["tool"] for call in (tool_calls or []))
        if self.memory is not None:
            self.memory.log_chat(user_message, record, session_id=self.session_id,
                                 source=source, meta=meta, occurred_at=occurred_at)

    # ---- session lifecycle (the "New chat" / history feature)
    # A session is just a tag on chat_log rows. Starting a new one clears working
    # memory; switching reloads a past conversation's history so replies have
    # context. Consolidation still reads ALL unconsolidated rows regardless.
    def start_new(self, session_id: str) -> None:
        self.session_id = session_id
        self.history = []
        self.tool_counts = Counter()

    def switch(self, session_id: str) -> None:
        self.session_id = session_id
        self.history = []
        self.tool_counts = Counter()
        if self.memory is None:
            return
        # only the recent tail of a past conversation goes back into working
        # memory (respond() also windows it, but don't hold the whole thread)
        turns = self.settings.history_turns
        for user_msg, reply in list(self.memory.session_history(session_id))[-turns:]:
            self.history.append({"role": "user", "content": user_msg})
            self.history.append({"role": "assistant", "content": reply})
            if "[tools used:" in reply:
                summary = reply.split("[tools used:", 1)[1]
                self.tool_counts.update(re.findall(r"(?:^|; )([A-Za-z_][\w]*)\(", summary))
