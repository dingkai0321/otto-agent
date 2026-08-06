"""DETERMINISTIC EVAL - the CLI /memory command reads PostgreSQL state."""

from __future__ import annotations

from otto.db import connect
from otto.gateway.cli import _memory_snapshot


def test_memory_snapshot_reads_seeded_home(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO facts (subject, content, source) VALUES (%s, %s, %s)",
        ("project", "Otto stays local-first", "user"),
    )
    conn.execute(
        "INSERT INTO facts (subject, content, source) VALUES (%s, %s, %s)",
        ("alex", "Alex prefers morning meetings", "consolidation"),
    )
    conn.execute(
        "INSERT INTO episodes (happened_at, summary) VALUES (%s, %s)",
        ("2026-07-16", "Planned the launch"),
    )
    conn.execute(
        "INSERT INTO episodes (happened_at, summary) VALUES (%s, %s)",
        ("2026-07-17", "Reviewed the launch checklist"),
    )
    conn.execute("INSERT INTO chat_log (role, content, consolidated) VALUES ('user', 'old', true)")
    conn.execute(
        "INSERT INTO chat_log (role, content, consolidated) VALUES ('user', 'new question', false)"
    )
    conn.execute(
        "INSERT INTO chat_log (role, content, consolidated) VALUES ('assistant', 'new answer', false)"
    )
    conn.commit()

    snapshot = _memory_snapshot(conn)

    assert "Semantic facts (2)" in snapshot
    assert "[alex] Alex prefers morning meetings" in snapshot
    assert "[project] Otto stays local-first" in snapshot
    assert "Recent episodes (2)" in snapshot
    assert "2026-07-17 - Reviewed the launch checklist" in snapshot
    assert "2026-07-16 - Planned the launch" in snapshot
    assert "Unconsolidated chat messages: 2" in snapshot


def test_memory_snapshot_handles_empty_home(tmp_path):
    snapshot = _memory_snapshot(connect(tmp_path))

    assert "Semantic facts (0)\n- none yet" in snapshot
    assert "Recent episodes (0)\n- none yet" in snapshot
    assert "Unconsolidated chat messages: 0" in snapshot
