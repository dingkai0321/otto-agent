"""PostgreSQL-backed task lists with dependencies and atomic claiming.

Todo-style progress and multi-agent coordination share one source of truth:
small work can use a flat list, while complex work adds DAG edges and owners.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb


class TaskError(ValueError):
    """A task request is invalid for the current graph or state."""


class TaskStore:
    VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "deleted"})

    def __init__(self, conn) -> None:
        self.conn = conn

    @staticmethod
    def _text(value: str, field: str, *, required: bool = False, limit: int = 4000) -> str:
        value = (value or "").strip()
        if required and not value:
            raise TaskError(f"{field} is required")
        if len(value) > limit:
            raise TaskError(f"{field} must be at most {limit} characters")
        return value

    def ensure_list(self, scope_key: str, *, scope_type: str = "session", title: str = "") -> str:
        scope_key = self._text(scope_key, "scope_key", required=True, limit=240)
        if scope_type not in {"session", "shared"}:
            raise TaskError("scope_type must be session or shared")
        row = self.conn.execute(
            "SELECT id FROM task_lists WHERE scope_type=%s AND scope_key=%s",
            (scope_type, scope_key),
        ).fetchone()
        if row:
            return str(row["id"])
        candidate = uuid4()
        self.conn.execute(
            """INSERT INTO task_lists (id, scope_type, scope_key, title)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (scope_type, scope_key) DO NOTHING""",
            (candidate, scope_type, scope_key, self._text(title, "title", limit=240)),
        )
        row = self.conn.execute(
            "SELECT id FROM task_lists WHERE scope_type=%s AND scope_key=%s",
            (scope_type, scope_key),
        ).fetchone()
        return str(row["id"])

    def create(
        self,
        scope_key: str,
        subject: str,
        description: str = "",
        active_form: str = "",
        metadata: dict[str, Any] | None = None,
        *,
        scope_type: str = "session",
    ) -> dict[str, Any]:
        subject = self._text(subject, "subject", required=True, limit=240)
        description = self._text(description, "description", limit=12_000)
        active_form = self._text(active_form, "active_form", limit=240)
        if metadata is not None and not isinstance(metadata, dict):
            raise TaskError("metadata must be an object")
        list_id = self.ensure_list(scope_key, scope_type=scope_type)
        row = self.conn.execute(
            """INSERT INTO tasks
                   (task_list_id, subject, description, active_form, metadata)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id""",
            (list_id, subject, description, active_form, Jsonb(metadata or {})),
        ).fetchone()
        self.conn.execute(
            "UPDATE task_lists SET status='active', updated_at=now() WHERE id=%s",
            (list_id,),
        )
        return self.get(scope_key, int(row["id"]), scope_type=scope_type)

    def _list_id(self, scope_key: str, scope_type: str = "session") -> str | None:
        row = self.conn.execute(
            "SELECT id FROM task_lists WHERE scope_type=%s AND scope_key=%s",
            (scope_type, scope_key),
        ).fetchone()
        return str(row["id"]) if row else None

    def get(
        self,
        scope_key: str,
        task_id: int,
        *,
        scope_type: str = "session",
    ) -> dict[str, Any]:
        list_id = self._list_id(scope_key, scope_type)
        if list_id is None:
            raise TaskError(f"task {task_id} not found")
        row = self.conn.execute(
            """SELECT t.*,
                      COALESCE(array_agg(DISTINCT d.blocked_by_task_id)
                        FILTER (WHERE d.blocked_by_task_id IS NOT NULL), '{}') AS blocked_by,
                      COALESCE(array_agg(DISTINCT r.task_id)
                        FILTER (WHERE r.task_id IS NOT NULL), '{}') AS blocks
                 FROM tasks t
                 LEFT JOIN task_dependencies d ON d.task_id=t.id
                 LEFT JOIN task_dependencies r ON r.blocked_by_task_id=t.id
                WHERE t.task_list_id=%s AND t.id=%s
                GROUP BY t.id""",
            (list_id, int(task_id)),
        ).fetchone()
        if not row or row["status"] == "deleted":
            raise TaskError(f"task {task_id} not found")
        task = dict(row)
        task["task_list_id"] = str(task["task_list_id"])
        task["metadata"] = task["metadata"] or {}
        task["blockedBy"] = sorted(task.pop("blocked_by") or [])
        task["blocks"] = sorted(task["blocks"] or [])
        task["blocked"] = bool(self._incomplete_dependencies(int(task_id)))
        task["available"] = task["status"] == "pending" and not task["blocked"]
        for key in ("created_at", "updated_at"):
            task[key] = task[key].isoformat() if hasattr(task[key], "isoformat") else str(task[key])
        return task

    def list(
        self,
        scope_key: str,
        *,
        scope_type: str = "session",
        include_completed: bool = True,
    ) -> list[dict[str, Any]]:
        list_id = self._list_id(scope_key, scope_type)
        if list_id is None:
            return []
        status_sql = "status <> 'deleted'" if include_completed else "status IN ('pending','in_progress')"
        ids = self.conn.execute(
            f"SELECT id FROM tasks WHERE task_list_id=%s AND {status_sql} ORDER BY id",
            (list_id,),
        ).fetchall()
        return [self.get(scope_key, int(row["id"]), scope_type=scope_type) for row in ids]

    def _incomplete_dependencies(self, task_id: int) -> list[int]:
        rows = self.conn.execute(
            """SELECT d.blocked_by_task_id
                 FROM task_dependencies d
                 JOIN tasks dep ON dep.id=d.blocked_by_task_id
                WHERE d.task_id=%s AND dep.status <> 'completed'
                ORDER BY d.blocked_by_task_id""",
            (task_id,),
        ).fetchall()
        return [int(row["blocked_by_task_id"]) for row in rows]

    def _add_dependency(self, list_id: str, task_id: int, blocked_by: int) -> None:
        if task_id == blocked_by:
            raise TaskError("a task cannot depend on itself")
        rows = self.conn.execute(
            "SELECT id, status FROM tasks WHERE task_list_id=%s AND id=ANY(%s) AND status <> 'deleted' FOR UPDATE",
            (list_id, [task_id, blocked_by]),
        ).fetchall()
        states = {int(row["id"]): row["status"] for row in rows}
        if set(states) != {task_id, blocked_by}:
            raise TaskError("dependencies must reference tasks in the same active task list")
        if states[task_id] == "completed":
            raise TaskError(f"cannot add a dependency to completed task {task_id}")
        if states[task_id] == "in_progress" and states[blocked_by] != "completed":
            raise TaskError(
                f"cannot make in-progress task {task_id} depend on unfinished task {blocked_by}"
            )
        cycle = self.conn.execute(
            """WITH RECURSIVE ancestors(id) AS (
                   SELECT blocked_by_task_id FROM task_dependencies WHERE task_id=%s
                   UNION
                   SELECT d.blocked_by_task_id
                     FROM task_dependencies d JOIN ancestors a ON d.task_id=a.id
               ) SELECT 1 FROM ancestors WHERE id=%s LIMIT 1""",
            (blocked_by, task_id),
        ).fetchone()
        if cycle:
            raise TaskError(f"dependency would create a cycle between tasks {task_id} and {blocked_by}")
        self.conn.execute(
            """INSERT INTO task_dependencies (task_id, blocked_by_task_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (task_id, blocked_by),
        )

    def update(
        self,
        scope_key: str,
        task_id: int,
        *,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        scope_type: str = "session",
    ) -> dict[str, Any] | None:
        list_id = self._list_id(scope_key, scope_type)
        if list_id is None:
            raise TaskError(f"task {task_id} not found")
        if status is not None and status not in self.VALID_STATUSES:
            raise TaskError("status must be pending, in_progress, completed, or deleted")
        if metadata is not None and not isinstance(metadata, dict):
            raise TaskError("metadata must be an object")
        with self.conn.transaction():
            current = self.conn.execute(
                "SELECT * FROM tasks WHERE task_list_id=%s AND id=%s FOR UPDATE",
                (list_id, int(task_id)),
            ).fetchone()
            if not current or current["status"] == "deleted":
                raise TaskError(f"task {task_id} not found")

            for dependency in add_blocked_by or []:
                self._add_dependency(list_id, int(task_id), int(dependency))
            for downstream in add_blocks or []:
                self._add_dependency(list_id, int(downstream), int(task_id))

            old_status = current["status"]
            next_owner = owner.strip() if isinstance(owner, str) else current["owner"]
            if status == "in_progress" and old_status != "in_progress":
                if old_status != "pending":
                    raise TaskError(f"cannot move task {task_id} from {old_status} to in_progress")
                blocked = self._incomplete_dependencies(int(task_id))
                if blocked:
                    raise TaskError(f"task {task_id} is blocked by unfinished tasks: {blocked}")
                next_owner = next_owner or "otto"
                busy = self.conn.execute(
                    """SELECT id FROM tasks WHERE task_list_id=%s AND owner=%s
                       AND status='in_progress' AND id<>%s LIMIT 1""",
                    (list_id, next_owner, int(task_id)),
                ).fetchone()
                if busy:
                    raise TaskError(f"owner '{next_owner}' is already working on task {busy['id']}")
            elif status == "completed" and old_status != "completed":
                if old_status != "in_progress":
                    raise TaskError(f"task {task_id} must be in_progress before completion")
            elif status == "pending" and old_status not in {"pending", "in_progress"}:
                raise TaskError(f"cannot release task {task_id} from {old_status}")
            if status == "pending":
                next_owner = None
            if status == "deleted":
                next_owner = None
                self.conn.execute(
                    "DELETE FROM task_dependencies WHERE task_id=%s OR blocked_by_task_id=%s",
                    (int(task_id), int(task_id)),
                )

            values: dict[str, Any] = {"owner": next_owner}
            if status is not None:
                values["status"] = status
            if subject is not None:
                values["subject"] = self._text(subject, "subject", required=True, limit=240)
            if description is not None:
                values["description"] = self._text(description, "description", limit=12_000)
            if active_form is not None:
                values["active_form"] = self._text(active_form, "active_form", limit=240)
            if metadata is not None:
                values["metadata"] = Jsonb(metadata)
            assignments = ", ".join(f"{key}=%s" for key in values)
            self.conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at=now() WHERE id=%s",
                (*values.values(), int(task_id)),
            )
            unfinished = self.conn.execute(
                "SELECT 1 FROM tasks WHERE task_list_id=%s AND status IN ('pending','in_progress') LIMIT 1",
                (list_id,),
            ).fetchone()
            self.conn.execute(
                "UPDATE task_lists SET status=%s, updated_at=now() WHERE id=%s",
                ("active" if unfinished else "completed", list_id),
            )
        if status == "deleted":
            return None
        return self.get(scope_key, int(task_id), scope_type=scope_type)

    def compact_context(self, scope_key: str) -> str:
        tasks = self.list(scope_key, include_completed=False)
        if not tasks:
            return ""
        lines = ["Current task list (durable; use task tools to update it):"]
        for task in tasks[:20]:
            marker = "▶" if task["status"] == "in_progress" else "○"
            blocked = f" blockedBy={task['blockedBy']}" if task["blocked"] else ""
            owner = f" owner={task['owner']}" if task["owner"] else ""
            lines.append(f"{marker} #{task['id']} [{task['status']}] {task['subject']}{blocked}{owner}")
        return "\n".join(lines)

    def status_snapshot(self, scope_key: str, *, limit: int = 12) -> dict[str, Any]:
        """Return exact aggregate progress plus a bounded open-task projection.

        Unlike ``list(include_completed=True)``, this never hydrates every old
        completed task on every LLM iteration. PostgreSQL computes the exact
        counts; only the small open-task tail needed by AgentStatusBar is read.
        """
        list_id = self._list_id(scope_key)
        if list_id is None:
            return {
                "counts": {"pending": 0, "in_progress": 0, "completed": 0},
                "blocked": 0,
                "tasks": [],
            }
        counts_row = self.conn.execute(
            """SELECT
                   count(*) FILTER (WHERE status='pending') AS pending,
                   count(*) FILTER (WHERE status='in_progress') AS in_progress,
                   count(*) FILTER (WHERE status='completed') AS completed
                 FROM tasks
                WHERE task_list_id=%s AND status <> 'deleted'""",
            (list_id,),
        ).fetchone()
        blocked_row = self.conn.execute(
            """SELECT count(DISTINCT task.id) AS blocked
                 FROM tasks task
                WHERE task.task_list_id=%s
                  AND task.status IN ('pending','in_progress')
                  AND EXISTS (
                      SELECT 1
                        FROM task_dependencies edge
                        JOIN tasks dependency ON dependency.id=edge.blocked_by_task_id
                       WHERE edge.task_id=task.id AND dependency.status <> 'completed'
                  )""",
            (list_id,),
        ).fetchone()
        rows = self.conn.execute(
            """SELECT id FROM tasks
                WHERE task_list_id=%s AND status IN ('pending','in_progress')
                ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, id
                LIMIT %s""",
            (list_id, max(0, int(limit))),
        ).fetchall()
        return {
            "counts": {
                state: int(counts_row[state] or 0)
                for state in ("pending", "in_progress", "completed")
            },
            "blocked": int(blocked_row["blocked"] or 0),
            "tasks": [self.get(scope_key, int(row["id"])) for row in rows],
        }

    @staticmethod
    def to_json(task: dict[str, Any] | list[dict[str, Any]]) -> str:
        return json.dumps(task, ensure_ascii=False, indent=2)
