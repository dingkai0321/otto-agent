"""DETERMINISTIC EVAL — native child context, safety, and task handoff."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from evals.helpers import ScriptedClient, make_otto, response, text_block, tool_block
from otto.config import Settings
from otto.db import connect
from otto.hooks import HookEvent, HookResult, build_hooks
from otto.permissions import PermissionPolicy
from otto.tasks import TaskStore
from otto.tools.registry import Tool, ToolRegistry
from otto.tools.subagents import make_tool


class RecordingClient(ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return super()._create(**kwargs)


class BrokenClient:
    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    @staticmethod
    def _create(**_kwargs):
        raise RuntimeError("provider unavailable")


def _registry(tmp_path, client, store, scope="session-a"):
    settings = Settings(home=tmp_path, api_key="offline", model="test-model")
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(
        Tool(
            "search_web",
            "fake child search",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            lambda query: f"evidence:{query}",
            permission="ask",
            permission_reason="test child approval",
        )
    )
    registry.register(make_tool(settings, client, registry, store, lambda: scope))
    return registry


def _allowing_hooks(registry, events):
    hooks = build_hooks(registry.permission_policy, lambda kind, event: events.append((kind, event)))
    hooks.register(
        HookEvent.PERMISSION_REQUEST,
        lambda ctx: HookResult(permission="allow", permission_reason=ctx.data["reason"]),
        name="test_approval",
    )
    return hooks


def test_native_child_has_fresh_context_restricted_tools_and_permission_bubbling(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    client = RecordingClient([
        response([tool_block("search_web", {"query": "evidence"})], "tool_use"),
        response([text_block("Independent conclusion")]),
    ])
    registry = _registry(tmp_path, client, store)
    events = []
    hooks = _allowing_hooks(registry, events)
    try:
        output = registry.execute(
            "agent_spawn",
            {"prompt": "Investigate only this", "role": "researcher"},
            hooks=hooks,
        )
        assert "Independent conclusion" in output
        first = client.calls[0]
        assert first["messages"][0] == {
            "role": "user", "content": "Investigate only this"
        }
        assert "<agent_status" in first["messages"][-1]["content"]
        child_tools = {tool["name"] for tool in first["tools"]}
        assert child_tools == {"search_web"}
        assert "agent_spawn" not in child_tools and "delegate_task" not in child_tools

        permission_events = [event for kind, event in events if kind == "permission"]
        assert any(event["tool"] == "agent_spawn" for event in permission_events)
        assert any(
            event["tool"] == "search_web" and event.get("subagent_id")
            for event in permission_events
        )
        sub_events = [event for kind, event in events if kind == "subagent"]
        assert any(event.get("type") == "start" for event in sub_events)
        assert any(event.get("type") == "done" for event in sub_events)
    finally:
        conn.close()


def test_bound_task_is_claimed_then_completed(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create("session-a", "Research options")
    client = RecordingClient([response([text_block("Recommendation ready")])])
    registry = _registry(tmp_path, client, store)
    events = []
    hooks = _allowing_hooks(registry, events)
    try:
        output = registry.execute(
            "agent_spawn",
            {"prompt": "Compare the options", "role": "researcher", "task_id": task["id"]},
            hooks=hooks,
        )
        completed = store.get("session-a", task["id"])
        assert completed["status"] == "completed"
        assert completed["owner"].startswith("researcher-")
        assert f"Task #{task['id']} was marked completed" in output
        task_actions = [event["action"] for kind, event in events if kind == "task"]
        assert task_actions == ["claimed", "completed"]
    finally:
        conn.close()


def test_completion_gate_releases_task_for_retry(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create("session-a", "Review acceptance")
    client = RecordingClient([response([text_block("Looks done")])])
    registry = _registry(tmp_path, client, store)
    events = []
    hooks = _allowing_hooks(registry, events)
    hooks.register(
        HookEvent.TASK_COMPLETED,
        lambda _ctx: HookResult(block_reason="acceptance test failed"),
    )
    try:
        output = registry.execute(
            "agent_spawn",
            {"prompt": "Review it", "role": "reviewer", "task_id": task["id"]},
            hooks=hooks,
        )
        released = store.get("session-a", task["id"])
        assert released["status"] == "pending" and released["owner"] is None
        assert "completion blocked by hook" in output
        assert "released back to pending" in output
    finally:
        conn.close()


def test_child_failure_releases_claim_and_records_failure(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    task = store.create("session-a", "Retryable investigation")
    registry = _registry(tmp_path, BrokenClient(), store)
    hooks = _allowing_hooks(registry, [])
    try:
        output = registry.execute(
            "agent_spawn",
            {"prompt": "Investigate", "task_id": task["id"]},
            hooks=hooks,
        )
        released = store.get("session-a", task["id"])
        assert released["status"] == "pending" and released["owner"] is None
        assert "provider unavailable" in output
        assert list((tmp_path / "subagents").glob("*.json"))
    finally:
        conn.close()


def test_blocked_task_cannot_be_delegated(tmp_path):
    conn = connect(tmp_path)
    store = TaskStore(conn)
    first = store.create("session-a", "Foundation")
    blocked = store.create("session-a", "Depends on foundation")
    store.update("session-a", blocked["id"], add_blocked_by=[first["id"]])
    client = RecordingClient([response([text_block("must not run")])])
    registry = _registry(tmp_path, client, store)
    try:
        output = registry.execute(
            "agent_spawn",
            {"prompt": "Start early", "task_id": blocked["id"]},
            hooks=_allowing_hooks(registry, []),
        )
        assert "blocked by unfinished tasks" in output
        assert client.calls == []
    finally:
        conn.close()


def test_otto_registers_native_subagent_without_experimental_flag(tmp_path):
    otto = make_otto(tmp_path, client=ScriptedClient([]), experimental=False)
    try:
        assert "agent_spawn" in otto.tools._tools
        assert "delegate_task" not in otto.tools._tools
    finally:
        otto.close()
