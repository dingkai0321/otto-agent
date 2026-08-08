"""Wiring — builds one Otto from its parts. Gateways call `respond()`.

This file is the assembly diagram in code: config → db → tools → memory →
session → loop. If you want to understand the repo in one place, start here.
"""

from __future__ import annotations

from datetime import datetime

from otto.config import Settings, load_settings
from otto.db import connect
from otto.hooks import HookContext, HookEvent, HookManager, HookResult, build_hooks
from otto.loop.agent import Approver, LoopResult, Observer, run_loop
from otto.loop.models import get_client
from otto.ops.tracing import Tracer
from otto.runtime.context import ContextManager
from otto.runtime.session import Session, load_soul
from otto.runtime.status import AgentStatusBar, timestamp_event
from otto.tasks import TaskStore
from otto.tools import build_registry


class Otto:
    def __init__(self, settings: Settings | None = None, client=None, conn=None):
        # `client` and `conn` are injectable: evals swap in a scripted model,
        # the dashboard injects a cross-thread connection. Same seam either way.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = conn or connect(
            self.settings.home,
            database_url=self.settings.database_url,
            schema=self.settings.database_schema,
            embedding_dimensions=self.settings.embedding_dimensions,
        )
        self.client = client or get_client(self.settings)
        self.context = ContextManager.from_settings(self.settings, self.client)

        # Memory first: the memory-management tools need it.
        from otto.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.client)
        self.session = Session(self.settings, memory=self.memory)
        self.tasks = TaskStore(self.conn)
        self.status_bar = AgentStatusBar(
            self.settings.workspace,
            task_provider=lambda: self.tasks.status_snapshot(self.session.session_id),
        )
        self.tools = build_registry(
            self.conn,
            self.settings,
            self.memory,
            task_store=self.tasks,
            task_scope=lambda: self.session.session_id,
            client=self.client,
        )
        self.mcp_bridge = getattr(self.tools, "mcp_bridge", None)
        self.tracer = Tracer(self.settings)
        # Persistent registrations live here. Every turn forks this template so
        # gateway observers and one-off approval callbacks never leak into the
        # next turn. Users may extend Otto with `otto.hooks.register(...)`.
        self.hooks = build_hooks(self.tools.permission_policy, self.tracer.event)
        self.hooks.register(
            HookEvent.STOP,
            self._task_stop_hook,
            name="unfinished_task_guard",
            priority=20,
        )

    def make_hooks(self, *observers: Observer | None) -> HookManager:
        """One run-scoped lifecycle bus with safety and trace adapters installed."""
        hooks = self.hooks.fork(share_state=False)
        for observer in observers:
            hooks.add_observer(observer)
        return hooks

    def _task_stop_hook(self, ctx: HookContext) -> HookResult | None:
        """Give a task-producing turn one guarded chance to finish open work."""
        if (
            ctx.data.get("reason") != "end_turn"
            or ctx.data.get("stop_hook_active")
            or not ctx.hooks.state.get("task_touched")
        ):
            return None
        open_tasks = self.tasks.list(self.session.session_id, include_completed=False)
        if not open_tasks:
            return None
        return HookResult(
            continue_prompt=(
                "You created or updated a durable task list this turn and work remains. "
                "Review the current tasks, continue the next available item if it can be "
                "completed now, and update task status accurately. If genuinely blocked on "
                "the user or an external event, explain that instead of claiming completion."
            )
        )

    def emit(self, event: str, **data) -> None:
        """Send an out-of-turn lifecycle event (config/wake/etc.) through hooks."""
        self.make_hooks().trigger(event, **data)

    def close(self) -> None:
        """Release external resources (MCP subprocesses and PostgreSQL). Called when the
        dashboard rebuilds the agent after a settings change."""
        if self.mcp_bridge is not None:
            self.mcp_bridge.close()
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    def respond(self, user_message: str, observer: Observer | None = None,
                source: str = "cli", stream: bool = False,
                approver: Approver | None = None) -> LoopResult:
        """One full turn: assemble working memory → run the loop → persist.
        `source` tags which gateway the message arrived through (cli / voice /
        discord / whatsapp / dashboard), so the unified chat can show its origin.
        `stream=True` streams the reply text token by token to the observer.
        Everything that happens is both shown (observer) and recorded (tracer)."""
        # capture the gate + graph decisions as they flow by, so we can persist
        # them with the turn (the reopened-thread telemetry the dashboard shows)
        import time
        captured: dict = {}

        def _capture(kind, ev):
            if kind == "gate":
                captured["gate"] = {"decision": ev.get("decision"), "reason": ev.get("reason")}
            if kind == "route":
                captured["graph_route"] = {"target": ev.get("target"), "reason": ev.get("reason")}
            if kind == "triage":
                captured["triage_reason"] = ev.get("reason")
            if kind == "graph_end":
                captured["graph_path"] = ev.get("path")
        hooks = self.make_hooks(observer, _capture)
        if approver is not None:
            def approval_hook(ctx: HookContext) -> HookResult:
                allowed = approver(ctx.data["tool"], ctx.data["args"], ctx.data["reason"])
                return HookResult(
                    permission="allow" if allowed else "deny",
                    permission_reason=ctx.data["reason"],
                )

            hooks.register(
                HookEvent.PERMISSION_REQUEST,
                approval_hook,
                name="gateway_approval",
                priority=0,
            )
        t0 = time.perf_counter()
        submitted_at = datetime.now().astimezone()

        # Built-in context controls work through every gateway, not only the
        # dashboard's graph-command front door.
        command, _, command_arg = (user_message or "").strip().partition(" ")
        if command.lower() == "/compact":
            return self.compact_context(command_arg, hooks=hooks)
        if command.lower() == "/context":
            return LoopResult(reply=self.context_status(), stop_reason="context_status")

        with self.tracer.turn(user_message):
            submitted = hooks.trigger(
                HookEvent.USER_PROMPT_SUBMIT,
                prompt=user_message,
                source=source,
                session_id=self.session.session_id,
            )
            effective_message = str(submitted.data.get("prompt", user_message))
            extra_context = submitted.additional_context
            # The graph front door is optional and can NEVER make Otto worse:
            # flag off → this is exactly the old code path; flag on → the triage
            # graph decides quick vs full, and any failure anywhere falls open
            # to the plain loop below (same fail-open rule as the retrieval gate).
            result = None
            if submitted.block_reason:
                result = LoopResult(
                    reply=f"Request blocked by hook: {submitted.block_reason}",
                    iterations=0,
                )
            elif self.settings.graph_workflows:
                try:
                    result = self._respond_via_graph(
                        effective_message, hooks, stream, approver, extra_context,
                        submitted_at,
                    )
                except Exception as exc:
                    hooks.trigger(
                        HookEvent.GRAPH_END,
                        workflow="triage", ms=0, steps=0, path=[], error=repr(exc),
                    )
                    result = None
            if result is None:
                result = self._run_full_turn(
                    effective_message, hooks, stream, approver, extra_context,
                    submitted_at,
                )

            quick = captured.get("graph_route", {}).get("target") == "quick_reply"

            def _status(out: str) -> str:
                low = (out or "").lower()
                return "error" if ("failed" in low or "timed out" in low
                                   or low.startswith("error")) else "ok"
            meta = {
                "gate": captured.get("gate"),
                "graph": ({"workflow": "triage",
                           "route": "quick" if quick else "full",
                           "reason": captured.get("triage_reason", ""),
                           "path": captured.get("graph_path")}
                          if "graph_route" in captured else None),
                "iterations": result.iterations,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "tools": [{"tool": c["tool"], "status": _status(c["output"])}
                          for c in result.tool_calls],
                # which brain answered this turn — so a reopened thread (or a
                # thread you switched models mid-way) shows it per card. A quick
                # graph turn was answered by the small model; say so honestly.
                "model": self.settings.small_model if quick else self.settings.model,
                "provider": self.settings.provider,
            }
            self.session.add_exchange(user_message, result.reply, tool_calls=result.tool_calls,
                                      source=source, meta=meta, occurred_at=submitted_at)
            if self.memory is not None:
                self.memory.maybe_consolidate(hooks=hooks)
                self.memory.export_markdown()   # keep MEMORY.md in sync

        self.tracer.end_turn(result.reply, result.iterations)
        return result

    def _run_full_turn(self, user_message: str, hooks: HookManager, stream: bool,
                       approver: Approver | None = None,
                       extra_context: str = "",
                       submitted_at: datetime | str | None = None) -> LoopResult:
        """The classic turn: assemble working memory, run THE loop. Extracted
        verbatim so the graph's full_agent node calls the SAME code as the
        flag-off default — loop-as-a-node can never drift from loop-as-default."""
        prompt = self.session.build_prompt(user_message, hooks=hooks)
        system = prompt.system
        runtime_context = prompt.dynamic_context
        if extra_context:
            runtime_context = "\n\n".join(part for part in (
                runtime_context,
                "UserPromptSubmit hook context:\n" + extra_context,
            ) if part)
        # Working memory is a bounded window: only the last N turns (2 rows
        # each) enter the prompt, so context/cost/latency stay flat no matter
        # how long the conversation runs. Older turns live in PostgreSQL and
        # come back via the retrieval gate + episodic memory when relevant.
        window = self.settings.history_turns * 2
        messages = self.session.history[-window:] + [{
            "role": "user", "content": timestamp_event(user_message, submitted_at)
        }]

        return run_loop(
            client=self.client,
            model=self.settings.model,
            system=system,
            runtime_context=runtime_context,
            messages=messages,
            tools=self.tools,
            max_iterations=self.settings.max_iterations,
            max_tokens=self.settings.max_tokens,
            approver=approver,
            hooks=hooks,
            stream=stream,
            context_manager=self.context,
            status_bar=self.status_bar,
            prior_tool_counts=self.session.tool_counts,
        )

    def compact_context(self, instructions: str = "", *, hooks: HookManager | None = None) -> LoopResult:
        """Manually compact active working history; durable PostgreSQL rows stay intact."""
        manager = hooks or self.make_hooks()
        if not self.session.history:
            return LoopResult(
                reply="当前会话还没有可压缩的工作上下文。",
                stop_reason="manual_compact",
            )
        report = self.context.compact(
            system=load_soul(self.settings),
            messages=self.session.history,
            tools=self.tools.schemas(active_skills=set()),
            hooks=manager,
            trigger="manual",
            instructions=instructions,
            force=True,
        )
        if report is None:
            return LoopResult(
                reply="上下文压缩未执行（可能被 hook 阻止，或压缩器连续失败）。",
                stop_reason="manual_compact_failed",
            )
        # Session history should end on an assistant turn so the next real user
        # message preserves the alternating conversation shape.
        if self.session.history and self.session.history[-1].get("role") == "user":
            self.session.history.append({
                "role": "assistant",
                "content": "Context restored from the compacted session summary.",
            })
        return LoopResult(
            reply=(
                f"已压缩 {report.compacted_messages} 条工作消息："
                f"约 {report.before_tokens:,} → {report.after_tokens:,} tokens。"
                f"完整归档保存在 `{report.transcript_path}`。"
            ),
            iterations=1,
            stop_reason="manual_compact",
        )

    def context_status(self) -> str:
        """Human-readable `/context` snapshot without making another model call."""
        window = self.settings.history_turns * 2
        messages = self.session.history[-window:]
        status = self.context.status(
            system=load_soul(self.settings),
            messages=messages,
            tools=self.tools.schemas(active_skills=set()),
        )
        last = status["last_compaction"]
        lines = [
            "当前上下文（估算）",
            f"- 输入：{status['estimated_tokens']:,} tokens",
            f"- 自动压缩阈值：{status['threshold_tokens']:,} tokens",
            f"- 模型窗口：{status['context_window_tokens']:,} tokens",
            f"- 距离自动压缩：{status['remaining_before_compaction']:,} tokens",
        ]
        if last:
            lines.append(
                f"- 上次压缩：{last['trigger']}，{last['before_tokens']:,} → "
                f"{last['after_tokens']:,} tokens"
            )
        return "\n".join(lines)

    def _respond_via_graph(self, user_message: str, hooks: HookManager, stream: bool,
                           approver: Approver | None = None,
                           extra_context: str = "",
                           submitted_at: datetime | str | None = None) -> LoopResult | None:
        """One turn through the triage graph workflow. Returns None whenever
        the graph didn't produce an answer — respond() then falls open to the
        plain loop, so this path can only ever ADD speed, never lose a reply."""
        from otto.graph import run_graph
        from otto.graph.workflows.triage import (
            QUICK_REPLY_PROMPT,
            build_triage_graph,
            classify_message,
            todays_events,
        )

        def quick_reply(state: dict) -> str:
            prompt = QUICK_REPLY_PROMPT.format(calendar=state.get("calendar", ""),
                                               message=timestamp_event(
                                                   state["message"], submitted_at
                                               ))
            if extra_context:
                prompt += "\n\nHook-provided context:\n" + extra_context
            response = self.client.messages.create(
                model=self.settings.small_model, max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in response.content if b.type == "text")

        graph = build_triage_graph(
            classify_fn=lambda m: classify_message(self.client, self.settings.small_model, m),
            calendar_fn=lambda: todays_events(self.settings.home),
            quick_fn=quick_reply,
            # the full path is the SAME method the flag-off default runs; the
            # engine's tagged notifier stamps its inner events with node=
            full_fn=lambda state: self._run_full_turn(
                state["message"], state.get("_hooks", hooks), stream, approver,
                extra_context, submitted_at),
        )
        state = run_graph(graph, {"message": user_message}, hooks=hooks)
        if isinstance(state.get("result"), LoopResult):
            return state["result"]
        if state.get("reply"):
            return LoopResult(reply=state["reply"], tool_calls=[], iterations=1)
        return None  # graph produced nothing → caller falls open to the loop
