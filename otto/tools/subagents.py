"""Otto-native synchronous subagents with isolated context and bounded tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from otto.hooks import HookContext, HookEvent, HookResult
from otto.loop.agent import run_loop
from otto.runtime.context import ContextManager
from otto.runtime.status import AgentStatusBar
from otto.tasks import TaskError, TaskStore
from otto.tools.registry import Tool, ToolRegistry


@dataclass(frozen=True)
class SubagentRole:
    purpose: str
    instructions: str
    tools: frozenset[str]


_TASK_READ = frozenset({"task_get", "task_list"})
_KNOWLEDGE_READ = frozenset({
    "search_web", "fetch_url", "search_knowledge", "load_skill", "load_skill_resource",
})
_WORKSPACE_READ = frozenset({"read_file", "list_files", "search_files"})

ROLES: dict[str, SubagentRole] = {
    "general": SubagentRole(
        purpose="investigate a bounded non-coding subtask",
        instructions=(
            "Investigate the assigned subtask independently. Prefer evidence from tools. "
            "Return a concise conclusion, important evidence, and any unresolved uncertainty."
        ),
        tools=_TASK_READ | _KNOWLEDGE_READ | _WORKSPACE_READ | {"list_events", "github_read"},
    ),
    "researcher": SubagentRole(
        purpose="gather and synthesize evidence",
        instructions=(
            "Research only the assigned question. Compare relevant evidence, distinguish facts "
            "from inference, and finish with a compact source-backed synthesis."
        ),
        tools=_TASK_READ | _KNOWLEDGE_READ | _WORKSPACE_READ | {"github_read"},
    ),
    "planner": SubagentRole(
        purpose="turn a goal into an ordered executable plan",
        instructions=(
            "Analyze dependencies, risks, and verification criteria. Return an ordered plan. "
            "Do not execute the plan or mutate task state."
        ),
        tools=_TASK_READ | _KNOWLEDGE_READ | _WORKSPACE_READ,
    ),
    "reviewer": SubagentRole(
        purpose="independently review evidence or proposed work",
        instructions=(
            "Act as an independent reviewer. Look for correctness gaps, unsafe assumptions, "
            "missing tests, and unmet acceptance criteria. Report findings by severity."
        ),
        tools=_TASK_READ | _WORKSPACE_READ | {"search_knowledge", "github_read"},
    ),
}


def _system(role_name: str, role: SubagentRole, agent_id: str) -> str:
    now = datetime.now().astimezone()
    return f"""You are Otto subagent {agent_id}, acting as the {role_name} role.

{role.instructions}

Rules:
- Work only on the assigned subtask and do not continue the parent's conversation.
- You have a fresh context. Treat the task prompt and tool evidence as your entire brief.
- You cannot spawn or delegate to another agent.
- Do not claim to have used a tool you do not have.
- Return only the useful final conclusion; intermediate exploration stays in this child run.
- If blocked, state exactly what is missing instead of pretending the task is complete.

Current local time: {now:%Y-%m-%d %H:%M %Z}.
"""


def _record_path(home: Path, agent_id: str) -> Path:
    path = home / "subagents" / f"{agent_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_tool(
    settings,
    client,
    parent_registry: ToolRegistry,
    task_store: TaskStore,
    scope_key: Callable[[], str],
) -> Tool:
    """Create the parent-only agent_spawn tool.

    Child registries are allowlisted and never contain agent_spawn,
    delegate_task, or mutating task tools, which makes recursion impossible at
    the harness boundary. The run-scoped hook fork preserves permission prompts.
    """

    def agent_spawn(
        prompt: str,
        role: str = "general",
        task_id: int = 0,
        max_iterations: int = 6,
        *,
        _hooks,
    ) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            return "Error: agent_spawn requires a self-contained prompt."
        if len(prompt) > 12_000:
            return "Error: agent_spawn prompt must be at most 12000 characters."
        role_name = (role or "general").strip().lower()
        spec = ROLES.get(role_name)
        if spec is None:
            return f"Error: unknown subagent role '{role}'. Choose: {', '.join(ROLES)}."
        iterations = max(1, min(int(max_iterations or 6), 12))
        agent_id = f"{role_name}-{uuid4().hex[:10]}"
        session_id = scope_key()
        claimed = None

        if task_id:
            try:
                current = task_store.get(session_id, int(task_id))
                if current["status"] != "pending":
                    return (
                        f"Error: task {task_id} is {current['status']} and cannot be claimed "
                        f"by {agent_id}."
                    )
                claimed = task_store.update(
                    session_id, int(task_id), status="in_progress", owner=agent_id
                )
            except TaskError as exc:
                return f"Error: could not claim task {task_id} — {exc}."
            _hooks.trigger(
                HookEvent.TASK_UPDATED,
                action="claimed",
                session_id=session_id,
                task=claimed,
                agent_id=agent_id,
            )

        allowed = set(spec.tools)
        child_registry = parent_registry.subset(allowed)
        # Registrations (including permission approval) are inherited, but
        # activation/task scratch state is not: a fresh child must explicitly
        # load its own skills rather than seeing the parent's active set.
        child_hooks = _hooks.fork(share_state=False)

        def tag_child(ctx: HookContext) -> HookResult:
            return HookResult(
                updates={"subagent_id": agent_id, "subagent_role": role_name, "depth": 1}
            )

        child_hooks.register("*", tag_child, name="subagent_identity", priority=-20_000)

        def relay_tool(ctx: HookContext) -> None:
            _hooks.trigger(
                HookEvent.SUBAGENT_EVENT,
                type="tool" if ctx.event == HookEvent.POST_TOOL_USE else "tool_failure",
                agent=agent_id,
                role=role_name,
                tool=ctx.data.get("tool"),
                output=ctx.data.get("output", ""),
                depth=1,
            )

        child_hooks.register(HookEvent.POST_TOOL_USE, relay_tool, name="subagent_tool_relay")
        child_hooks.register(HookEvent.TOOL_FAILURE, relay_tool, name="subagent_failure_relay")

        _hooks.trigger(
            HookEvent.SUBAGENT_START,
            type="start",
            agent=agent_id,
            role=role_name,
            task_id=int(task_id) if task_id else None,
            tools=sorted(child_registry._tools),
            depth=1,
        )

        result = None
        failure = ""
        try:
            result = run_loop(
                client=client,
                model=settings.model,
                system=_system(role_name, spec, agent_id),
                messages=[{"role": "user", "content": prompt}],
                tools=child_registry,
                max_iterations=iterations,
                max_tokens=settings.max_tokens,
                hooks=child_hooks,
                stream=False,
                context_manager=ContextManager.from_settings(
                    settings, client, model=settings.small_model or settings.model
                ),
                status_bar=AgentStatusBar(
                    settings.workspace,
                    task_provider=(
                        (lambda: task_store.status_snapshot(session_id))
                        if task_store is not None else None
                    ),
                ),
            )
            if result.stop_reason != "end_turn":
                failure = f"child stopped with {result.stop_reason or 'unknown status'}"
        except Exception as exc:  # the parent receives a recoverable tool result
            failure = f"{type(exc).__name__}: {exc}"

        task_note = ""
        if claimed is not None:
            if not failure:
                task = task_store.get(session_id, int(task_id))
                gate = _hooks.trigger(
                    HookEvent.TASK_COMPLETED,
                    session_id=session_id,
                    task_id=int(task_id),
                    task_subject=task["subject"],
                    task_description=task["description"],
                    owner=agent_id,
                    subagent_id=agent_id,
                )
                if gate.block_reason:
                    failure = f"completion blocked by hook: {gate.block_reason}"
            if failure:
                released = task_store.update(session_id, int(task_id), status="pending")
                _hooks.trigger(
                    HookEvent.TASK_UPDATED,
                    action="released",
                    session_id=session_id,
                    task=released,
                    agent_id=agent_id,
                    reason=failure,
                )
                task_note = f"Task #{task_id} was released back to pending."
            else:
                completed = task_store.update(session_id, int(task_id), status="completed")
                _hooks.trigger(
                    HookEvent.TASK_UPDATED,
                    action="completed",
                    session_id=session_id,
                    task=completed,
                    agent_id=agent_id,
                )
                task_note = f"Task #{task_id} was marked completed."

        summary = result.reply.strip() if result is not None else ""
        record = {
            "agent_id": agent_id,
            "role": role_name,
            "session_id": session_id,
            "task_id": int(task_id) if task_id else None,
            "prompt": prompt,
            "status": "failed" if failure else "completed",
            "failure": failure or None,
            "summary": summary,
            "iterations": result.iterations if result is not None else 0,
            "tools": result.tool_calls if result is not None else [],
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path = _record_path(settings.home, agent_id)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
                        encoding="utf-8")

        _hooks.trigger(
            HookEvent.SUBAGENT_STOP,
            type="done" if not failure else "failed",
            agent=agent_id,
            role=role_name,
            task_id=int(task_id) if task_id else None,
            iterations=record["iterations"],
            summary=summary,
            error=failure or None,
            record=str(path),
            depth=1,
        )
        if failure:
            return f"Error: subagent {agent_id} failed — {failure}. {task_note} (record: {path})"
        return (
            f"Subagent {agent_id} ({role_name}) completed. {task_note}\n"
            f"{summary or '(no final text)'}\n(record: {path})"
        )

    return Tool(
        name="agent_spawn",
        description=(
            "Launch a Otto-native child agent with fresh context and a restricted read-only "
            "tool set. Roles: general, researcher, planner, reviewer. Use for a bounded "
            "independent subtask whose exploration would distract the main conversation. "
            "Optionally bind a pending task_id for atomic claim and lifecycle updates. "
            "For coding and file edits, use delegate_task when that specialist is available."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Self-contained child brief; it cannot see parent messages",
                },
                "role": {"type": "string", "enum": list(ROLES), "description": "default general"},
                "task_id": {
                    "type": "integer",
                    "description": "Optional pending task to claim and complete or release",
                },
                "max_iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "Child loop limit; default 6",
                },
            },
            "required": ["prompt"],
        },
        fn=agent_spawn,
        wants_hooks=True,
    )
