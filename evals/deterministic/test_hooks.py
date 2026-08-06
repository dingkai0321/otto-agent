"""DETERMINISTIC EVAL — hooks are Otto's single lifecycle extension surface."""

from __future__ import annotations

from evals.helpers import ScriptedClient, response, text_block
from otto.hooks import HookEvent, HookManager, HookResult, build_hooks
from otto.loop.agent import run_loop
from otto.permissions import PermissionPolicy
from otto.tools.registry import Tool, ToolRegistry


def _tool(name, fn) -> Tool:
    return Tool(name, "test", {"type": "object", "properties": {}}, fn)


def test_hooks_run_in_priority_order_and_merge_updates():
    hooks = HookManager()
    seen = []

    hooks.register("Demo", lambda ctx: seen.append("late") or None, priority=20)
    hooks.register(
        "Demo", lambda ctx: seen.append("early") or HookResult(updates={"value": 2}),
        priority=10,
    )

    outcome = hooks.trigger("Demo", value=1)
    assert seen == ["early", "late"]
    assert outcome.data["value"] == 2


def test_observer_is_a_read_only_hook_and_sees_rewritten_output():
    events = []
    hooks = build_hooks(None, lambda kind, event: events.append((kind, event)))
    hooks.register(
        HookEvent.POST_TOOL_USE,
        lambda ctx: HookResult(updates={"output": "redacted"}),
    )

    outcome = hooks.trigger(HookEvent.POST_TOOL_USE, tool="demo", args={}, output="secret")

    assert outcome.data["output"] == "redacted"
    assert events == [("tool", {"tool": "demo", "args": {}, "output": "redacted"})]


def test_pre_tool_rewrite_is_checked_by_terminal_permission_hook(tmp_path):
    called = []
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("bash", lambda command: called.append(command) or "ran"))
    hooks = build_hooks(registry.permission_policy)
    hooks.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: HookResult(updates={"args": {"command": "sudo reboot"}}),
        # Even an absurd priority cannot move a normal hook after terminal safety.
        priority=999_999,
    )

    output = registry.execute(
        "bash", {"command": "ls"}, hooks=hooks, approver=lambda *_: True
    )

    assert output.startswith("Error: permission denied")
    assert not called


def test_hook_allow_cannot_weaken_a_hard_deny(tmp_path):
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(_tool("bash", lambda command: "ran"))
    hooks = build_hooks(registry.permission_policy)
    hooks.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: HookResult(permission="allow", permission_reason="custom allow"),
    )

    output = registry.execute("bash", {"command": "rm -rf /"}, hooks=hooks)

    assert output.startswith("Error: permission denied")
    assert "root" in output


def test_pre_hook_can_block_and_post_hook_can_rewrite_result():
    called = []
    registry = ToolRegistry()
    registry.register(_tool("demo", lambda value=0: called.append(value) or f"raw:{value}"))

    blocked = HookManager()
    blocked.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: HookResult(block_reason="policy says no"),
    )
    assert registry.execute("demo", {"value": 1}, hooks=blocked).startswith(
        "Error: blocked by hook"
    )
    assert not called

    allowed = HookManager()
    allowed.register(
        HookEvent.POST_TOOL_USE,
        lambda ctx: HookResult(updates={"output": "rewritten"}),
    )
    assert registry.execute("demo", {"value": 2}, hooks=allowed) == "rewritten"
    assert called == [2]


def test_critical_hook_failure_fails_closed_but_optional_failure_does_not():
    def broken(_ctx):
        raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(_tool("demo", lambda: "ran"))

    optional = HookManager()
    optional.register(HookEvent.PRE_TOOL_USE, broken)
    assert registry.execute("demo", {}, hooks=optional) == "ran"

    critical = HookManager()
    critical.register(HookEvent.PRE_TOOL_USE, broken, critical=True)
    assert registry.execute("demo", {}, hooks=critical).startswith("Error: blocked by hook")


def test_permission_request_itself_is_a_hook(tmp_path):
    registry = ToolRegistry(PermissionPolicy(tmp_path))
    registry.register(Tool("external", "test", {"type": "object"}, lambda: "ran",
                           permission="ask"))
    hooks = build_hooks(registry.permission_policy)
    hooks.register(
        HookEvent.PERMISSION_REQUEST,
        lambda ctx: HookResult(permission="allow", permission_reason=ctx.data["reason"]),
    )

    assert registry.execute("external", {}, hooks=hooks) == "ran"


def test_stop_hook_can_request_one_guarded_continuation():
    hooks = HookManager()
    stops = []

    def continue_once(ctx):
        stops.append(ctx.data["stop_hook_active"])
        return HookResult(continue_prompt="Check your answer once more.")

    hooks.register(HookEvent.STOP, continue_once)
    client = ScriptedClient([
        response([text_block("first")]),
        response([text_block("second")]),
    ])

    result = run_loop(
        client=client,
        model="test",
        system="test",
        messages=[{"role": "user", "content": "answer"}],
        tools=ToolRegistry(),
        hooks=hooks,
    )

    assert result.reply == "second"
    assert result.iterations == 2
    assert stops == [False, True]


def test_user_prompt_hook_can_update_prompt_and_add_context():
    hooks = HookManager()
    hooks.register(
        HookEvent.USER_PROMPT_SUBMIT,
        lambda ctx: HookResult(
            updates={"prompt": ctx.data["prompt"].strip()},
            additional_context="tenant=local",
        ),
    )

    outcome = hooks.trigger(HookEvent.USER_PROMPT_SUBMIT, prompt="  hello  ")
    assert outcome.data["prompt"] == "hello"
    assert outcome.additional_context == "tenant=local"
