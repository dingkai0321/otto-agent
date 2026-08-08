"""Lifecycle hooks — one extension surface for the whole Otto harness.

The loop knows *when* something happened; hooks decide what extra behavior runs
there.  This keeps permissions, tracing, streaming UI events and future policy
checks out of the loop's control flow.

Hooks may inspect every event. Lifecycle hooks may additionally update context,
block an action, inject prompt context, or ask the Stop point to continue once.
Legacy ``observer(kind, event)`` callbacks are adapters on this same bus: they
are read-only hooks, not a second event system.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from otto.permissions import PermissionPolicy

PermissionBehavior = Literal["allow", "ask", "deny", "passthrough"]
Observer = Callable[[str, dict[str, Any]], None]


class HookEvent:
    """Canonical lifecycle and telemetry event names."""

    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_LLM_CALL = "PreLLMCall"
    AGENT_STATUS = "AgentStatus"
    LLM_RESPONSE = "LLMResponse"
    TEXT_DELTA = "TextDelta"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    TOOL_FAILURE = "ToolFailure"
    PERMISSION_DECISION = "PermissionDecision"
    PERMISSION_REQUEST = "PermissionRequest"
    STOP = "Stop"
    RETRIEVAL_DECISION = "RetrievalDecision"
    CONSOLIDATION_COMPLETE = "ConsolidationComplete"
    GRAPH_START = "GraphStart"
    GRAPH_NODE_START = "GraphNodeStart"
    GRAPH_NODE_END = "GraphNodeEnd"
    GRAPH_ROUTE = "GraphRoute"
    GRAPH_END = "GraphEnd"
    TRIAGE_DECISION = "TriageDecision"
    SUBAGENT_EVENT = "SubagentEvent"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    TASK_UPDATED = "TaskUpdated"
    CONFIG_CHANGE = "ConfigChange"
    WAKE_SCAN = "WakeScan"
    HOOK_FAILURE = "HookFailure"


# External observers and existing trace/dashboard data keep their compact names.
# New code emits only canonical events; this adapter is the compatibility edge.
_OBSERVER_NAMES = {
    HookEvent.PRE_COMPACT: "compact_start",
    HookEvent.POST_COMPACT: "compact",
    HookEvent.AGENT_STATUS: "status",
    HookEvent.LLM_RESPONSE: "llm",
    HookEvent.TEXT_DELTA: "text",
    HookEvent.POST_TOOL_USE: "tool",
    HookEvent.TOOL_FAILURE: "tool",
    HookEvent.PERMISSION_DECISION: "permission",
    HookEvent.RETRIEVAL_DECISION: "gate",
    HookEvent.CONSOLIDATION_COMPLETE: "consolidation",
    HookEvent.GRAPH_START: "graph_start",
    HookEvent.GRAPH_NODE_START: "node_start",
    HookEvent.GRAPH_NODE_END: "node_end",
    HookEvent.GRAPH_ROUTE: "route",
    HookEvent.GRAPH_END: "graph_end",
    HookEvent.TRIAGE_DECISION: "triage",
    HookEvent.SUBAGENT_EVENT: "subagent",
    HookEvent.SUBAGENT_START: "subagent",
    HookEvent.SUBAGENT_STOP: "subagent",
    HookEvent.TASK_CREATED: "task_created",
    HookEvent.TASK_COMPLETED: "task_completed",
    HookEvent.TASK_UPDATED: "task",
    HookEvent.CONFIG_CHANGE: "config",
    HookEvent.WAKE_SCAN: "wake_scan",
    HookEvent.HOOK_FAILURE: "hook_failure",
}


@dataclass
class HookContext:
    event: str
    data: dict[str, Any]
    hooks: HookManager


@dataclass
class HookResult:
    """What one hook wants to change about the current lifecycle point."""

    updates: dict[str, Any] = field(default_factory=dict)
    block_reason: str | None = None
    additional_context: str = ""
    continue_prompt: str | None = None
    prevent_continuation: bool = False
    permission: PermissionBehavior = "passthrough"
    permission_reason: str = ""


@dataclass
class HookOutcome(HookResult):
    """Merged result plus the final event data seen by later hooks."""

    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


HookCallback = Callable[[HookContext], HookResult | None]


@dataclass(frozen=True)
class _RegisteredHook:
    terminal: bool
    priority: int
    order: int
    name: str
    callback: HookCallback
    critical: bool = False


class HookManager:
    """Priority-ordered, thread-safe registry for lifecycle extensions."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[_RegisteredHook]] = defaultdict(list)
        self._counter = 0
        self._lock = threading.RLock()
        # Mutable state belongs to one top-level agent run. Nested graph forks
        # share it so a task touched inside a node is still visible at Stop.
        self.state: dict[str, Any] = {}

    def register(
        self,
        event: str,
        callback: HookCallback,
        *,
        name: str | None = None,
        priority: int = 100,
        critical: bool = False,
        terminal: bool = False,
    ) -> None:
        """Register a callback. Lower priority numbers run first; ties keep order."""
        with self._lock:
            self._counter += 1
            item = _RegisteredHook(
                terminal, priority, self._counter,
                name or getattr(callback, "__name__", "hook"),
                callback, critical,
            )
            self._hooks[event].append(item)
            self._hooks[event].sort(key=lambda h: (h.terminal, h.priority, h.order))

    def add_observer(self, observer: Observer | None) -> None:
        """Adapt the old read-only Observer protocol onto the hook bus."""
        if observer is None:
            return
        observer_lock = threading.RLock()

        def forward(ctx: HookContext) -> None:
            kind = _OBSERVER_NAMES.get(ctx.event)
            if kind is None:
                return
            with observer_lock:
                observer(kind, dict(ctx.data))

        # Observers run after behavioral hooks so they see rewritten args/output.
        self.register("*", forward, name="observer", priority=10_000, terminal=True)

    def has_hook(self, event: str, name: str) -> bool:
        with self._lock:
            return any(h.name == name for h in self._hooks.get(event, ()))

    def fork(self, *, share_state: bool = True) -> HookManager:
        """Copy registrations; nested calls may share the top-level run state."""
        child = HookManager()
        with self._lock:
            child._hooks = defaultdict(list, {key: list(value) for key, value in self._hooks.items()})
            child._counter = self._counter
            child.state = self.state if share_state else {}
        return child

    def trigger(self, event: str, **data: Any) -> HookOutcome:
        """Run matching hooks and merge their decisions deterministically.

        Permission decisions are monotonic: deny beats ask, ask beats allow, and
        no later hook can weaken an earlier result. This keeps the hard security
        policy invariant even when user extensions are registered.
        """
        outcome = HookOutcome(data=dict(data))
        context = HookContext(event=event, data=outcome.data, hooks=self)
        with self._lock:
            callbacks = sorted(
                [*self._hooks.get(event, ()), *self._hooks.get("*", ())],
                key=lambda h: (h.terminal, h.priority, h.order),
            )

        rank = {"passthrough": 0, "allow": 1, "ask": 2, "deny": 3}
        for hook in callbacks:
            try:
                result = hook.callback(context)
            except Exception as exc:  # one optional extension never crashes a turn
                message = f"{hook.name}: {type(exc).__name__}: {exc}"
                outcome.errors.append(message)
                if hook.critical and outcome.block_reason is None:
                    outcome.block_reason = f"critical hook failed — {message}"
                continue
            if result is None:
                continue
            if result.updates:
                outcome.data.update(result.updates)
                outcome.updates.update(result.updates)
            if result.block_reason and outcome.block_reason is None:
                outcome.block_reason = result.block_reason
            if result.additional_context:
                outcome.additional_context = "\n".join(
                    p for p in (outcome.additional_context, result.additional_context) if p
                )
            if result.continue_prompt and outcome.continue_prompt is None:
                outcome.continue_prompt = result.continue_prompt
            outcome.prevent_continuation |= result.prevent_continuation
            if rank[result.permission] > rank[outcome.permission]:
                outcome.permission = result.permission
                outcome.permission_reason = result.permission_reason

        return outcome


def permission_hook(policy: PermissionPolicy) -> HookCallback:
    """Wrap the s03 permission policy as the first critical PreToolUse hook."""

    def check(ctx: HookContext) -> HookResult:
        tool = ctx.data["tool"]
        args = ctx.data.get("args") or {}
        declared = ctx.data.get("declared_permission", "passthrough")
        decision = policy.evaluate(
            tool,
            args,
            declared=declared,
            declared_reason=ctx.data.get("declared_reason", ""),
        )
        return HookResult(
            permission=decision.behavior,
            permission_reason=decision.reason,
        )

    return check


def build_hooks(
    permission_policy: PermissionPolicy | None = None,
    *observers: Observer | None,
) -> HookManager:
    """Build one run-scoped bus with Otto's safety hook and observer adapters."""
    hooks = HookManager()
    if permission_policy is not None:
        hooks.register(
            HookEvent.PRE_TOOL_USE,
            permission_hook(permission_policy),
            name="permission_policy",
            priority=-10_000,
            critical=True,
            terminal=True,
        )
    for observer in observers:
        hooks.add_observer(observer)
    return hooks
