"""Task tools: a durable checklist that grows into a dependency graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from otto.hooks import HookEvent
from otto.tasks import TaskStore
from otto.tools.registry import Tool


def make_tools(store: TaskStore, scope_key: Callable[[], str]) -> list[Tool]:
    def task_create(
        subject: str,
        description: str = "",
        active_form: str = "",
        metadata: dict[str, Any] | None = None,
        *,
        _hooks,
    ) -> str:
        scope = scope_key()
        before = _hooks.trigger(
            HookEvent.TASK_CREATED,
            session_id=scope,
            task_subject=subject,
            task_description=description,
            active_form=active_form,
            metadata=metadata or {},
        )
        if before.block_reason:
            return f"Error: task creation blocked by hook — {before.block_reason}."
        task = store.create(
            scope,
            str(before.data.get("task_subject", subject)),
            str(before.data.get("task_description", description)),
            str(before.data.get("active_form", active_form)),
            before.data.get("metadata", metadata or {}),
        )
        _hooks.state["task_touched"] = True
        _hooks.trigger(HookEvent.TASK_UPDATED, action="created", session_id=scope, task=task)
        return TaskStore.to_json({"task": task})

    def task_update(
        task_id: int,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        *,
        _hooks,
    ) -> str:
        scope = scope_key()
        if status == "completed":
            current = store.get(scope, task_id)
            before = _hooks.trigger(
                HookEvent.TASK_COMPLETED,
                session_id=scope,
                task_id=task_id,
                task_subject=current["subject"],
                task_description=current["description"],
                owner=current["owner"],
            )
            if before.block_reason:
                return f"Error: task completion blocked by hook — {before.block_reason}."
        task = store.update(
            scope,
            task_id,
            status=status,
            subject=subject,
            description=description,
            active_form=active_form,
            owner=owner,
            metadata=metadata,
            add_blocked_by=add_blocked_by,
            add_blocks=add_blocks,
        )
        _hooks.state["task_touched"] = True
        action = "deleted" if task is None else "updated"
        _hooks.trigger(
            HookEvent.TASK_UPDATED,
            action=action,
            session_id=scope,
            task=task or {"id": task_id, "status": "deleted"},
        )
        return TaskStore.to_json({"success": True, "task_id": task_id, "task": task})

    def task_get(task_id: int) -> str:
        return TaskStore.to_json({"task": store.get(scope_key(), task_id)})

    def task_list(include_completed: bool = True) -> str:
        tasks = store.list(scope_key(), include_completed=include_completed)
        counts = {
            status: sum(1 for task in tasks if task["status"] == status)
            for status in ("pending", "in_progress", "completed")
        }
        return TaskStore.to_json({"tasks": tasks, "stats": {"total": len(tasks), **counts}})

    task_fields = {
        "task_id": {"type": "integer", "description": "Task id returned by task_create"},
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed", "deleted"],
        },
        "subject": {"type": "string", "description": "Optional replacement title"},
        "description": {"type": "string", "description": "Optional detailed acceptance criteria"},
        "active_form": {"type": "string", "description": "Present-tense progress label"},
        "owner": {"type": "string", "description": "Agent or worker claiming the task"},
        "metadata": {"type": "object", "description": "Optional structured extension data"},
        "add_blocked_by": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Task ids that must complete before this task can start",
        },
        "add_blocks": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Downstream task ids blocked by this task",
        },
    }
    return [
        Tool(
            name="task_create",
            description=(
                "Create one durable task for complex multi-step work. Create separate tasks, then "
                "use task_update to add dependencies. Skip task tracking for short single-step work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Short actionable title"},
                    "description": {"type": "string", "description": "Details and completion criteria"},
                    "active_form": {"type": "string", "description": "Present-tense label shown while working"},
                    "metadata": {"type": "object"},
                },
                "required": ["subject"],
            },
            fn=task_create,
            wants_hooks=True,
        ),
        Tool(
            name="task_update",
            description=(
                "Patch one durable task. Use in_progress before work and completed only after "
                "verification. Add blocked_by/blocks edges for ordered work; pending releases a task."
            ),
            input_schema={"type": "object", "properties": task_fields, "required": ["task_id"]},
            fn=task_update,
            wants_hooks=True,
        ),
        Tool(
            name="task_get",
            description="Read one task with its details, owner, dependency edges, and availability.",
            input_schema={
                "type": "object",
                "properties": {"task_id": task_fields["task_id"]},
                "required": ["task_id"],
            },
            fn=task_get,
        ),
        Tool(
            name="task_list",
            description="Read the current session's durable task list and progress snapshot.",
            input_schema={
                "type": "object",
                "properties": {
                    "include_completed": {
                        "type": "boolean",
                        "description": "Include completed tasks; defaults to true",
                    }
                },
            },
            fn=task_list,
        ),
    ]
