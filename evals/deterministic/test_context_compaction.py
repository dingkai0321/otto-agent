"""Deterministic coverage for Otto's layered context compaction."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from evals.helpers import make_otto, response, text_block
from otto.hooks import HookEvent, HookManager, HookResult
from otto.loop.agent import run_loop
from otto.runtime.context import ContextManager, is_context_overflow
from otto.tools.registry import ToolRegistry


class RecordingClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def manager(tmp_path, client, **overrides):
    defaults = {
        "client": client,
        "model": "compact-model",
        "home": tmp_path,
        "context_window_tokens": 1_000,
        "max_output_tokens": 100,
        "buffer_tokens": 0,
        "tool_result_budget_chars": 1_000,
        "keep_recent_tool_results": 1,
        "keep_recent_messages": 2,
        "max_messages": 10,
    }
    defaults.update(overrides)
    return ContextManager(**defaults)


def test_large_tool_output_is_archived_before_entering_context(tmp_path):
    ctx = manager(tmp_path, RecordingClient([]), tool_result_budget_chars=1_000)
    original = "important-result\n" * 200

    compact = ctx.budget_tool_output("tu/unsafe", "demo", original)

    assert len(compact) < len(original)
    assert "Only this preview is in context" in compact
    files = list((tmp_path / "tool-results").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == original
    assert "/" not in files[0].name


def test_micro_compaction_retains_newest_result_and_pointers_are_recoverable(tmp_path):
    ctx = manager(tmp_path, RecordingClient([]), keep_recent_tool_results=1)
    messages = [
        {"role": "assistant", "content": [SimpleNamespace(
            type="tool_use", id="one", name="demo", input={})]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "old evidence"}
        ]},
        {"role": "assistant", "content": [SimpleNamespace(
            type="tool_use", id="two", name="demo", input={})]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "two", "content": "fresh evidence"}
        ]},
    ]

    assert ctx.micro_compact(messages) == 1
    old = messages[1]["content"][0]["content"]
    assert old.startswith("[Earlier tool result compacted;")
    assert messages[3]["content"][0]["content"] == "fresh evidence"
    stored = list((tmp_path / "tool-results").glob("*.txt"))
    assert stored and stored[0].read_text(encoding="utf-8") == "old evidence"


def test_safe_snip_never_orphans_a_tool_result(tmp_path):
    ctx = manager(tmp_path, RecordingClient([]), max_messages=8)
    messages = [{"role": "user", "content": "start"}]
    for index in range(8):
        messages.extend([
            {"role": "assistant", "content": [SimpleNamespace(
                type="tool_use", id=f"t{index}", name="demo", input={})]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{index}", "content": str(index)}
            ]},
        ])

    removed = ctx.safe_snip(messages)

    assert removed > 0
    assert len(messages) <= 8
    # Synthetic user notice may be followed by an assistant tool_use; its
    # matching result must still be the immediately following user message.
    for index, message in enumerate(messages):
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        result_id = message["content"][0]["tool_use_id"]
        assert index > 0
        previous = messages[index - 1]["content"]
        assert any(getattr(block, "id", None) == result_id for block in previous)


def test_auto_compaction_summarizes_prefix_and_emits_hooks(tmp_path):
    client = RecordingClient([response([text_block("Goal kept; next step is verify.")])])
    ctx = manager(tmp_path, client)
    messages = [
        {"role": "user", "content": "old " + "x" * 4_000},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent request"},
        {"role": "assistant", "content": "recent answer"},
    ]
    events = []
    hooks = HookManager()
    hooks.add_observer(lambda kind, event: events.append((kind, event)))

    report = ctx.maybe_compact(system="system", messages=messages, tools=[], hooks=hooks)

    assert report is not None
    assert report.trigger == "auto"
    assert report.before_tokens > report.after_tokens
    assert "Goal kept" in messages[0]["content"]
    assert messages[-2]["content"] == "recent request"
    assert [kind for kind, _ in events] == ["compact_start", "compact"]
    assert events[-1][1]["status"] == "completed"
    assert (tmp_path / "transcripts").exists()


def test_precompact_hook_can_block_without_calling_summarizer(tmp_path):
    client = RecordingClient([])
    ctx = manager(tmp_path, client)
    hooks = HookManager()
    hooks.register(
        HookEvent.PRE_COMPACT,
        lambda _ctx: HookResult(block_reason="retain exact legal text"),
    )
    messages = [{"role": "user", "content": "x" * 5_000}]

    report = ctx.compact(
        system="", messages=messages, tools=[], hooks=hooks,
        trigger="manual", force=True,
    )

    assert report is None
    assert not client.calls
    assert messages == [{"role": "user", "content": "x" * 5_000}]


def test_prompt_too_long_forces_compaction_then_retries_once(tmp_path):
    client = RecordingClient([
        RuntimeError("maximum context length exceeded"),
        response([text_block("Preserve the current request.")]),
        response([text_block("Recovered answer")]),
    ])
    ctx = manager(
        tmp_path, client,
        context_window_tokens=100_000,
        max_output_tokens=1_000,
        keep_recent_messages=8,
    )
    hooks = HookManager()
    events = []
    hooks.add_observer(lambda kind, event: events.append((kind, event)))

    result = run_loop(
        client=client,
        model="main-model",
        system="system",
        messages=[{"role": "user", "content": "large provider-specific request"}],
        tools=ToolRegistry(),
        hooks=hooks,
        context_manager=ctx,
    )

    assert result.reply == "Recovered answer"
    assert len(client.calls) == 3
    assert client.calls[1]["model"] == "compact-model"
    assert "context_summary" in str(client.calls[2]["messages"])
    assert any(kind == "compact" and event["trigger"] == "reactive"
               for kind, event in events)


def test_context_overflow_detection_is_specific():
    assert is_context_overflow(RuntimeError("prompt is too long for this context window"))
    assert not is_context_overflow(RuntimeError("rate limit exceeded"))


def test_otto_context_commands_report_and_manually_compact_session(tmp_path):
    client = RecordingClient([response([text_block("Keep decision D and file /tmp/a.")])])
    app = make_otto(tmp_path / "home", client=client)
    try:
        app.session.history = [
            {"role": "user", "content": "Choose D"},
            {"role": "assistant", "content": "Decision D recorded in /tmp/a"},
        ]

        status = app.respond("/context")
        assert "自动压缩阈值" in status.reply
        assert not client.calls

        compacted = app.respond("/compact preserve exact paths")
        assert compacted.stop_reason == "manual_compact"
        assert "已压缩 2 条工作消息" in compacted.reply
        assert "preserve exact paths" in client.calls[0]["messages"][0]["content"]
        assert "context_summary" in app.session.history[0]["content"]
        assert app.session.history[-1]["role"] == "assistant"
    finally:
        app.close()
