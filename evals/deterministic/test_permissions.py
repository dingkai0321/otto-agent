"""DETERMINISTIC EVAL — every tool call crosses the permission boundary.

These tests assert behavior rather than prompt wording: forbidden calls never
reach a handler, context-dependent calls need an approver, and safe calls keep
the zero-friction path.
"""

from __future__ import annotations

import threading
import time

from otto.ops.dashboard import _await_permission, permission_action
from otto.permissions import PermissionPolicy
from otto.tools.registry import Tool, ToolRegistry


def _tool(name: str, fn, *, permission="passthrough") -> Tool:
    return Tool(
        name=name,
        description="test tool",
        input_schema={"type": "object", "properties": {}},
        fn=fn,
        permission=permission,
    )


def test_safe_commands_and_workspace_files_are_allowed(tmp_path):
    policy = PermissionPolicy(tmp_path)

    assert policy.evaluate("bash", {"command": "ls -la"}).behavior == "allow"
    assert policy.evaluate("write_file", {"path": "notes/today.md"}).behavior == "allow"


def test_hard_deny_wins_before_user_approval(tmp_path):
    called = []
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("bash", lambda command: called.append(command) or "ran"))

    approvals = []
    output = registry.execute(
        "bash",
        {"command": "sudo shutdown now"},
        approver=lambda *args: approvals.append(args) or True,
    )

    assert output.startswith("Error: permission denied")
    assert not called
    assert not approvals, "a hard deny must never be downgraded to an approval prompt"


def test_root_deletion_is_hard_denied(tmp_path):
    decision = PermissionPolicy(tmp_path).evaluate("bash", {"command": "rm -rf /"})
    assert decision.behavior == "deny"
    assert "root" in decision.reason


def test_destructive_shell_requires_approval_and_respects_answer(tmp_path):
    called = []
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("bash", lambda command: called.append(command) or "removed"))

    denied = registry.execute("bash", {"command": "rm old.txt"}, approver=lambda *_: False)
    assert denied.startswith("Error: permission denied")
    assert not called

    allowed = registry.execute("bash", {"command": "rm old.txt"}, approver=lambda *_: True)
    assert allowed == "removed"
    assert called == ["rm old.txt"]


def test_outside_workspace_file_access_requires_approval(tmp_path):
    policy = PermissionPolicy(tmp_path / "workspace")

    decision = policy.evaluate("fs_write_file", {"path": str(tmp_path / "elsewhere.txt")})

    assert decision.behavior == "ask"
    assert "outside workspace" in decision.reason


def test_unattended_gateway_fails_closed_for_ask(tmp_path):
    called = []
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("external_write", lambda: called.append(True) or "ran", permission="ask"))

    output = registry.execute("external_write", {})

    assert "no interactive approver" in output
    assert not called


def test_current_high_risk_actions_are_classified(tmp_path):
    policy = PermissionPolicy(tmp_path)

    assert policy.evaluate("delegate_task", {"task": "edit app.py"}).behavior == "ask"
    assert policy.evaluate("manage_memory", {"action": "delete", "id": 1}).behavior == "ask"
    assert policy.evaluate("manage_memory", {"action": "search", "query": "alex"}).behavior == "allow"


def test_permission_events_show_ask_then_final_decision(tmp_path):
    events = []
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("external_write", lambda: "done", permission="ask"))

    assert registry.execute(
        "external_write", {}, notify=lambda kind, event: events.append((kind, event)),
        approver=lambda *_: True,
    ) == "done"

    assert [(kind, event["decision"]) for kind, event in events if kind == "permission"] == [
        ("permission", "ask"),
        ("permission", "allow"),
    ]


def test_dashboard_request_can_be_resolved_from_another_http_thread():
    emitted = []
    answer = []

    worker = threading.Thread(
        target=lambda: answer.append(
            _await_permission(
                "delegate_task", {"task": "edit"}, "coding",
                lambda kind, event: emitted.append((kind, event)), 2,
            )
        )
    )
    worker.start()
    for _ in range(100):
        if emitted:
            break
        time.sleep(0.005)

    request_id = emitted[0][1]["request_id"]
    assert permission_action({"request_id": request_id, "decision": "allow"}) == {
        "ok": True,
        "decision": "allow",
    }
    worker.join(timeout=1)
    assert answer == [True]


def test_unknown_dashboard_permission_request_is_rejected():
    assert "error" in permission_action({"request_id": "missing", "decision": "allow"})
