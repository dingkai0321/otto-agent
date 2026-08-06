"""DETERMINISTIC EVAL — Otto's durable Todo + task-DAG system."""

from __future__ import annotations

from collections import Counter

import pytest

from evals.helpers import ScriptedClient, make_otto
from otto.db import connect
from otto.hooks import HookEvent, HookManager, HookResult
from otto.tasks import TaskError, TaskStore
from otto.tools.registry import ToolRegistry
from otto.tools.tasks import make_tools


def test_task_graph_lifecycle_dependencies_and_cycle_detection(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    try:
        schema = store.create("session-a", "Create schema")
        api = store.create("session-a", "Build API")
        tests = store.create("session-a", "Write tests")

        api = store.update("session-a", api["id"], add_blocked_by=[schema["id"]])
        tests = store.update("session-a", tests["id"], add_blocked_by=[api["id"]])
        assert api["blocked"] is True
        assert tests["blockedBy"] == [api["id"]]
        with pytest.raises(TaskError, match="cycle"):
            store.update("session-a", schema["id"], add_blocked_by=[tests["id"]])
        with pytest.raises(TaskError, match="blocked"):
            store.update("session-a", api["id"], status="in_progress")

        store.update("session-a", schema["id"], status="in_progress", owner="otto")
        store.update("session-a", schema["id"], status="completed")
        api = store.get("session-a", api["id"])
        assert api["available"] is True
        assert api["blocks"] == [tests["id"]]
        snapshot = store.status_snapshot("session-a", limit=1)
        assert snapshot["counts"] == {
            "pending": 2, "in_progress": 0, "completed": 1,
        }
        assert snapshot["blocked"] == 1
        assert len(snapshot["tasks"]) == 1
    finally:
        conn.close()


def test_claim_is_atomic_per_owner_and_release_allows_reclaim(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    try:
        first = store.create("session-a", "First")
        second = store.create("session-a", "Second")
        store.update("session-a", first["id"], status="in_progress", owner="agent-a")
        with pytest.raises(TaskError, match="already working"):
            store.update("session-a", second["id"], status="in_progress", owner="agent-a")
        released = store.update("session-a", first["id"], status="pending")
        assert released["owner"] is None
        claimed = store.update("session-a", second["id"], status="in_progress", owner="agent-a")
        assert claimed["owner"] == "agent-a"
    finally:
        conn.close()


def test_task_lists_are_isolated_by_session(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    try:
        task = store.create("session-a", "Only A")
        assert [item["id"] for item in store.list("session-a")] == [task["id"]]
        assert store.list("session-b") == []
        with pytest.raises(TaskError, match="not found"):
            store.get("session-b", task["id"])
    finally:
        conn.close()


def test_task_lifecycle_hooks_can_gate_creation_and_completion(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    registry = ToolRegistry()
    for tool in make_tools(store, lambda: "session-a"):
        registry.register(tool)
    try:
        blocked_create = HookManager()
        blocked_create.register(
            HookEvent.TASK_CREATED,
            lambda _ctx: HookResult(block_reason="needs acceptance criteria"),
        )
        output = registry.execute("task_create", {"subject": "Vague"}, hooks=blocked_create)
        assert output.startswith("Error: task creation blocked")
        assert store.list("session-a") == []

        hooks = HookManager()
        created = registry.execute(
            "task_create",
            {"subject": "Verified", "description": "tests pass"},
            hooks=hooks,
        )
        assert '"task"' in created
        task = store.list("session-a")[0]
        registry.execute(
            "task_update",
            {"task_id": task["id"], "status": "in_progress"},
            hooks=hooks,
        )
        hooks.register(
            HookEvent.TASK_COMPLETED,
            lambda _ctx: HookResult(block_reason="tests failed"),
        )
        output = registry.execute(
            "task_update",
            {"task_id": task["id"], "status": "completed"},
            hooks=hooks,
        )
        assert output.startswith("Error: task completion blocked")
        assert store.get("session-a", task["id"])["status"] == "in_progress"
    finally:
        conn.close()


def test_otto_registers_task_tools_and_status_uses_only_current_session(tmp_path):
    otto = make_otto(tmp_path, client=ScriptedClient([]))
    try:
        assert {"task_create", "task_update", "task_get", "task_list"} <= set(otto.tools._tools)
        otto.tasks.create("default", "Resume me")
        status = otto.status_bar.render(
            iteration=1,
            max_iterations=10,
            elapsed_seconds=0,
            tool_counts=Counter(),
            tool_signature_counts=Counter(),
            tool_failures=0,
            last_tool=None,
            active_skills=set(),
            tool_output_chars=0,
        )
        assert 'total="1"' in status.text and "Resume me" in status.text

        otto.session.start_new("other-session")
        other = otto.status_bar.render(
            iteration=1,
            max_iterations=10,
            elapsed_seconds=0,
            tool_counts=Counter(),
            tool_signature_counts=Counter(),
            tool_failures=0,
            last_tool=None,
            active_skills=set(),
            tool_output_chars=0,
        )
        assert 'total="0"' in other.text and "Resume me" not in other.text
    finally:
        otto.close()


def test_run_state_is_fresh_per_turn_but_shared_with_nested_forks():
    template = HookManager()
    run = template.fork(share_state=False)
    run.state["task_touched"] = True
    assert run.fork().state["task_touched"] is True
    assert template.fork(share_state=False).state == {}
