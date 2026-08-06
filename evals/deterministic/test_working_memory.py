"""DETERMINISTIC EVAL — working-memory assembly is pure string logic.

Regression net for a live bug found on the dashboard: the agent had the date
but not the time, so it asked the user "what time is it?" before scheduling
something "in 30 minutes." The system prompt must carry a real clock.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from otto.config import load_settings
from otto.runtime.session import Session
from otto.runtime.status import AgentStatusBar


def test_agent_status_bar_includes_current_time(tmp_path):
    status = AgentStatusBar(
        tmp_path,
        now=lambda: datetime.fromisoformat("2026-08-06T13:45:00+08:00"),
    ).render(
        iteration=1,
        max_iterations=10,
        elapsed_seconds=0.25,
        tool_counts=Counter(),
        tool_signature_counts=Counter(),
        tool_failures=0,
        last_tool=None,
        active_skills=set(),
        tool_output_chars=0,
    )
    assert "13:45:00+08:00" in status.text
    assert 'timezone="CST"' in status.text


def test_session_tags_history_with_its_session_id():
    # sessions are just a session_id label; a fresh Session carries the default.
    settings = load_settings()
    assert Session(settings, memory=None).session_id == "default"
    s = Session(settings, memory=None)
    s.start_new("s-test")
    assert s.session_id == "s-test" and s.history == []


def test_prompt_excludes_provider_and_model_name():
    settings = load_settings()
    settings.ensure_home()
    settings.provider, settings.model = "kimi", "kimi-k3"
    prompt = Session(settings, memory=None).build_prompt("what model are you?")
    assert "kimi-k3" not in prompt.dynamic_context
    assert "kimi" not in prompt.dynamic_context
    assert "kimi-k3" not in prompt.system
