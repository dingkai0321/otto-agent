"""DETERMINISTIC EVAL — working memory is a bounded sliding window.

An always-on gateway session can accumulate history forever, and every turn
would resend the whole thing (unbounded context ->
cost/latency climb -> eventual context-limit break). Working memory must be a
fixed window; older turns live in PostgreSQL + consolidation, not the prompt."""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_otto, response, text_block


def _gate_skip():
    return response([text_block('{"retrieve": false, "query": "", "reason": "t"}')])


def test_prompt_history_is_windowed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HISTORY_TURNS", "3")   # keep only last 3 turns
    sent = []

    class Recorder(ScriptedClient):
        def _create(self, **kwargs):
            # snapshot the message count NOW — run_loop mutates the same list
            # (appends the assistant reply) after this call returns
            sent.append(list(kwargs.get("messages", [])))
            return self._script.pop(0)

    # 5 turns; each turn = gate call (skip) + one loop call
    script = []
    for _ in range(5):
        script += [_gate_skip(), response([text_block("ok")])]
    app = make_otto(tmp_path / "home", client=Recorder(script))
    for i in range(5):
        app.respond(f"message number {i}")

    # The LAST loop call has at most 3 turns * 2 rows + the new user message,
    # then one ephemeral Agent Status row. Optional framework context is also
    # outside Session.history and therefore never consumes the history window.
    last = sent[-1]
    assert len(last) <= 1 + 3 * 2 + 1, f"window not applied: {len(last)} messages"
    assert "<agent_status" in last[-1]["content"]
    text_blob = " ".join(str(m.get("content", "")) for m in last)
    assert "message number 0" not in text_blob
    assert "message number 4" in text_blob   # the newest turn is present


def test_default_window_is_generous_but_finite(tmp_path, monkeypatch):
    monkeypatch.delenv("OTTO_HISTORY_TURNS", raising=False)
    app = make_otto(tmp_path / "home", client=ScriptedClient([]))
    assert app.settings.history_turns == 12
