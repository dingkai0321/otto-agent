"""Dashboard — every pillar on one local page. Zero new dependencies.

    make dashboard        # → http://localhost:7777

One stdlib HTTP server reading the files Otto already writes:
  loop + harness   traces/*.jsonl   (turns, gate decisions, tool calls, tokens)
  memory           PostgreSQL       (facts, episodes, chat log, consolidation)
  tools            PostgreSQL + calendar.ics + outbox/
  eval             eval_report.json (written by `make gate`)

The overview mirrors the architecture diagram — every box is clickable and
opens that section's live data. The chat dock is a real gateway: type (or speak)
a message and watch the same harness (gate, loop, tools, memory) that the CLI,
voice, Discord, and WhatsApp gateways drive light up in the browser as it runs.

The frontend is plain static files (static/index.html + style.css + app.js)
served as-is — no build step, no framework. This file is just the server + API.
Bound to 127.0.0.1 only. For deep trace waterfalls use Phoenix (`make trace`).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from psycopg import sql as pg_sql

from otto.config import load_settings
from otto.db import connect
from otto.ops import browser_agent, commands, compare_history
from otto.ops.arena import (
    compare_clear,
    compare_delete_run,
    compare_regrade,
    compare_stream,
    history_response,
)
from otto.ops.browser_agent import agent_lock, dash_session, get_agent, maybe_rotate_session
from otto.ops.catalog import list_models
from otto.ops.pricing import price_for, usage_summary
from otto.ops.settings_api import apply_settings, pin_action, settings_info
from otto.ops.tracing import TraceEncodingError, iter_trace_lines

PORT = 7777
# The frontend lives in its own files (static/index.html + style.css + app.js),
# served as-is by this stdlib server — no build step, no framework. Edit those
# to change the UI; edit this file to change the server/API.
STATIC = Path(__file__).resolve().parent / "static"

# A dashboard tool call runs on one HTTP thread while its approve/deny response
# is posted on another. Entries are random, process-local, and short lived.
_permission_lock = threading.Lock()
_pending_permissions: dict[str, dict] = {}


def _await_permission(tool: str, args: dict, reason: str, emit, timeout: int) -> bool:
    request_id = uuid.uuid4().hex
    state = {"event": threading.Event(), "allow": False}
    with _permission_lock:
        _pending_permissions[request_id] = state
    emit("permission_request", {
        "request_id": request_id,
        "tool": tool,
        "args": args,
        "reason": reason,
        "timeout": timeout,
    })
    state["event"].wait(max(1, timeout))
    with _permission_lock:
        _pending_permissions.pop(request_id, None)
    return bool(state["allow"])


def permission_action(payload: dict) -> dict:
    request_id = str(payload.get("request_id", ""))
    with _permission_lock:
        state = _pending_permissions.get(request_id)
        if state is None:
            return {"error": "permission request is missing or expired"}
        state["allow"] = payload.get("decision") == "allow"
        state["event"].set()
    return {"ok": True, "decision": "allow" if state["allow"] else "deny"}


def chat(message: str) -> dict:
    """One turn, one JSON result — the non-streaming door to the same room.

    The dashboard itself uses /api/chat/stream; this exists for scripts and for
    `curl`. It deliberately does NOT reimplement the turn: it drives chat_stream
    and keeps the final "done" payload, because the two used to be separate
    copies of the same 25 lines and had already drifted (the streaming one
    reported which model answered, this one didn't). One implementation means
    they cannot disagree again.
    """
    final: dict = {}

    def collect_done(kind: str, ev: dict) -> None:
        if kind == "done":
            final.update(ev)

    chat_stream(message, collect_done)
    return final


def chat_stream(message: str, emit, *, interactive: bool = False) -> None:
    """Run one turn, calling emit(kind, event) for every harness event AS it
    happens — gate decision, tool calls, and the reply text token by token —
    so the browser can show thinking stream in (like the CLI/voice do). Ends
    with a 'done' event carrying the final structured result.

    A leading slash calls a graph workflow BY NAME instead of running a turn.
    Both doors end in the same 'done' event, so the chat renders the answer the
    same way whether the harness routed it or you named the shape yourself."""
    command = commands.parse(message)
    if command is not None:
        _run_command(command, emit)
        return

    events: list[dict] = []

    def observer(kind, ev):
        if kind in ("gate", "consolidation", "route", "triage"):
            events.append({"kind": kind, **ev})
        emit(kind, ev)

    with agent_lock:
        agent = get_agent()
        maybe_rotate_session(agent)
        start = datetime.now(UTC)
        approver = None
        if interactive:
            approver = lambda tool, args, reason: _await_permission(
                tool, args, reason, emit, agent.settings.permission_timeout
            )
        result = agent.respond(
            message,
            observer=observer,
            source="dashboard",
            stream=True,
            approver=approver,
        )
        latency_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)

    gate = next((e for e in events if e["kind"] == "gate"), None)
    cons = next((e for e in events if e["kind"] == "consolidation"), None)
    route = next((e for e in events if e["kind"] == "route"), None)
    triage = next((e for e in events if e["kind"] == "triage"), None)
    quick = bool(route) and route.get("target") == "quick_reply"
    emit("done", {
        "reply": result.reply,
        "gate": {"decision": gate["decision"], "reason": gate.get("reason")} if gate else None,
        "graph": ({"workflow": route.get("workflow", "triage"),
                   "route": "quick" if quick else "full",
                   "reason": (triage or {}).get("reason", "")} if route else None),
        "tools": [{"tool": c["tool"], "args": c["args"], "output": c["output"],
                   "status": _tool_status(c["output"]),
                   "summary": (c["output"] or "").split(". ")[0][:120]} for c in result.tool_calls],
        "consolidation": {"new_facts": cons["new_facts"]} if cons else None,
        "iterations": result.iterations,
        "latency_ms": latency_ms,
        # which brain answered — shown per card; a quick graph turn was the small model
        "model": agent.settings.small_model if quick else agent.settings.model,
    })


# A NAME -> runner table, never a dynamic import of whatever the browser sent.
# "run the workflow the client named" is one careless refactor away from "import
# and call whatever string arrives", so the indirection is a dict on purpose.
def WORKFLOW_RUNNERS() -> dict[str, str]:  # noqa: N802 — reads as a table
    """Discovered, not hand-listed. A hardcoded table and a slash-command list
    are two registries of the same fact, and they drift."""
    return commands.discover()


def graph_stream(payload: dict, emit) -> None:
    """Run a graph workflow, streaming its node events as SSE.

    Only the engine's own events go out — graph_start / node_start / node_end /
    route / graph_end. They already carry `workflow` and `node`, which is all a
    card needs, and they carry no node OUTPUT, so a digest can never leak into
    a frame. Unlike the Arena this needs no lock of its own: run_graph already
    serialises observer hooks, so events arrive whole.
    """
    name = (payload.get("workflow") or "").strip()
    target = WORKFLOW_RUNNERS().get(name)
    if target is None:
        emit("done", {"error": f"unknown workflow '{name}'"})
        return
    module_name, _, fn_name = target.partition(":")
    try:
        import importlib

        run = getattr(importlib.import_module(module_name), fn_name)
        state = run(observer=lambda kind, ev: emit(kind, ev))
        emit("done", {
            "workflow": name,
            "digest": (state.get("digest") or "")[:4000],
            "draft_path": state.get("draft_path", ""),
            "errors": state.get("errors") or {},
        })
    except Exception as exc:
        # Includes GraphStateCollision, which run_graph raises OUT (unlike node
        # errors) — better shown in the card than dropped on the floor.
        emit("done", {"error": f"{type(exc).__name__}: {exc}"})


def _run_command(command: tuple[str, str], emit) -> None:
    """Handle `/name` from the chat box.

    The node events go out exactly as the engine emits them, so the topology
    chart animates from the same trace poll that animates a normal turn — a
    named workflow lights the picture as readily as a routed one.
    """
    name, arg = command
    start = datetime.now(UTC)
    if name in ("compact", "context"):
        with agent_lock:
            agent = get_agent()
            maybe_rotate_session(agent)
            result = agent.respond(
                f"/{name}" + (f" {arg}" if arg else ""),
                observer=emit,
                source="dashboard",
            )
        emit("done", {
            "reply": result.reply, "tools": [], "gate": None,
            "consolidation": None, "iterations": result.iterations,
            "latency_ms": int((datetime.now(UTC) - start).total_seconds() * 1000),
        })
        return
    if name in ("graphs", "help", "?"):
        emit("done", {"reply": commands.describe(), "tools": [], "iterations": 0,
                      "latency_ms": 0, "gate": None})
        return
    try:
        state = commands.run(name, emit, arg)
    except Exception as exc:
        emit("done", {"reply": f"`/{name}` failed: {type(exc).__name__}: {exc}",
                      "tools": [], "iterations": 0, "latency_ms": 0, "gate": None})
        return
    if state is None:
        emit("done", {"reply": commands.unknown_reply(name), "tools": [],
                      "iterations": 0, "latency_ms": 0, "gate": None})
        return
    reply = state.get("digest") or "(the workflow produced no text)"
    if state.get("ignored_argument"):
        reply = (f"*`/{name}` takes no input, so \u201c{state['ignored_argument']}\u201d "
                 f"was not used — a fixed shape always fetches the same sources. "
                 f"Ask a normal question to use the loop instead.*\n\n") + reply
    if state.get("draft_path"):
        reply += f"\n\n*saved to `{state['draft_path']}`*"
    for node, err in (state.get("errors") or {}).items():
        reply += f"\n\n*{node}: {err}*"
    emit("done", {
        "reply": reply, "tools": [], "gate": None, "consolidation": None,
        "iterations": 0,
        "latency_ms": int((datetime.now(UTC) - start).total_seconds() * 1000),
        "workflow": name,
    })


def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _tool_status(output: str) -> str:
    """Classify a tool result for the UI: ok / warn / error — from the output
    string alone (tools already report honestly, so trust their words)."""
    low = (output or "").lower()
    if "failed" in low or "timed out" in low or low.startswith("error"):
        return "error"
    if "already exists" in low or "not synced" in low or "skipped" in low:
        return "warn"
    return "ok"


# Notion-backed episodes live across the network, so the client AND the result
# are cached with a short TTL — collect() runs on every dashboard auto-refresh
# and must not round-trip to Notion every few seconds (rate limits + latency).
# The PostgreSQL path is a local query and doesn't need this.
_NOTION_EPISODES_TTL = 30.0   # seconds; the page polls ~every 5s
_notion_lock = threading.Lock()
_notion_store = None                       # built once (its constructor calls Notion)
_notion_episodes: tuple[float, list] | None = None   # (fetched_at, items)


def _get_notion_store():
    """The ONE NotionEpisodeStore for the whole dashboard process. Its
    constructor round-trips to Notion (data-source resolution), so it's built
    lazily and cached. Callers must hold _notion_lock."""
    global _notion_store
    if _notion_store is None:
        from otto.memory.episodic.notion_store import NotionEpisodeStore

        _notion_store = NotionEpisodeStore()
    return _notion_store


def collect() -> dict:
    """Everything the page shows, in one JSON blob."""
    settings = load_settings()
    settings.ensure_home()
    home = settings.home
    conn = connect(home)

    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    def episodes_payload() -> dict:
        """Episodes from the active backend: PostgreSQL (default) or Notion.
        A Notion outage must not take down the whole dashboard payload."""
        if settings.episodic_store != "notion":
            return {
                "source": "postgres",
                "error": "",
                "items": rows(
                    "SELECT id, happened_at, summary FROM episodes ORDER BY happened_at DESC"
                ),
            }
        try:
            global _notion_episodes
            with _notion_lock:
                store = _get_notion_store()
                if _notion_episodes and time.time() - _notion_episodes[0] < _NOTION_EPISODES_TTL:
                    return {"source": "notion", "error": "", "items": _notion_episodes[1]}
                items = store.list()
                _notion_episodes = (time.time(), items)
                return {"source": "notion", "error": "", "items": items}
        except Exception as exc:
            # Degrade gracefully: never take the payload down, and serve the
            # last good fetch if we have one (an outage shouldn't blank the tab).
            stale = _notion_episodes[1] if _notion_episodes else []
            return {"source": "notion", "error": str(exc), "items": stale}

    episodes_data = episodes_payload()

    # --- traces → turns (group events between turn_start and turn_end)
    events = []
    trace_errors = []
    trace_files = sorted((home / "traces").glob("*.jsonl"))
    for path in trace_files:
        try:
            lines = list(iter_trace_lines(path))
        except TraceEncodingError as exc:
            trace_errors.append({"file": path.name, "error": str(exc)})
            continue
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    turns, current, wake_scans = [], None, []
    for ev in events:
        kind = ev.get("type")
        if kind == "turn_start":
            current = {"user_message": ev.get("user_message"), "ts": ev.get("ts"),
                       "gate": None, "llm_calls": [], "tools": [], "reply": None}
        elif kind == "wake_scan":
            wake_scans.append(ev)
        elif current is not None:
            if kind == "gate":
                current["gate"] = ev
            elif kind == "route":
                current["graph"] = {"workflow": ev.get("workflow"),
                                    "route": "quick" if ev.get("target") == "quick_reply" else "full",
                                    "reason": (current.get("graph") or {}).get("reason", "")}
            elif kind == "triage":
                current.setdefault("graph", {})["reason"] = ev.get("reason", "")
            elif kind == "llm":
                current["llm_calls"].append(ev)
            elif kind == "tool":
                current["tools"].append(ev)
            elif kind == "consolidation":
                current["consolidation"] = ev
            elif kind == "turn_end":
                current["reply"] = ev.get("reply")
                current["iterations"] = ev.get("iterations")
                turns.append(current)
                current = None
    if current is not None:  # a turn that never ended = the smoking gun for hangs
        current["reply"] = "TURN NEVER FINISHED — check for a hang after this point"
        current["unfinished"] = True
        turns.append(current)

    # --- derive per-turn latency + dollar cost (the ops numbers humans feel)
    if settings.base_url or settings.provider == "openrouter":
        list_models()  # warm the per-model price cache (5-min cached fetch)
    price_in, price_out = price_for(settings.provider, settings.model or "")
    for t in turns:
        start, end = _parse_ts(t["ts"]), None
        last = t["llm_calls"][-1]["ts"] if t["llm_calls"] else None
        end = _parse_ts(last)
        t["latency_ms"] = int((end - start).total_seconds() * 1000) if start and end else None
        tin = sum(c.get("usage", {}).get("in", 0) for c in t["llm_calls"])
        tout = sum(c.get("usage", {}).get("out", 0) for c in t["llm_calls"])
        t["cost"] = tin / 1e6 * price_in + tout / 1e6 * price_out
        for x in t["tools"]:
            x["status"] = _tool_status(x.get("output", ""))
            x["summary"] = (x.get("output", "") or "").split(". ")[0][:120]

    latencies = sorted(t["latency_ms"] for t in turns if t["latency_ms"] is not None)
    total_cost = sum(t["cost"] for t in turns)

    def pct(p: float) -> int:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0

    from otto.memory import bundled_skill_dirs
    from otto.memory.procedural.loader import SkillLoader

    skills = [{"name": s.name, "description": s.description, "body": s.body,
               "path": str(s.path),
               "resources": [vars(item) for item in s.resources],
               "required_tools": list(s.required_tools),
               "warnings": list(s.warnings),
               # relative path (for reveal) + whether it lives in the editable home dir
               "rel": _rel_to_home(s.path, home),
               "editable": str((home / "skills").resolve()) in str(s.path.resolve())}
              for s in SkillLoader([*bundled_skill_dirs(), home / "skills"]).skills]

    eval_report = None
    report_path = home / "eval_report.json"
    if report_path.exists():
        eval_report = json.loads(report_path.read_text(encoding="utf-8"))

    eval_history = []
    hist_path = home / "eval_runs.jsonl"
    if hist_path.exists():
        for line in hist_path.read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                eval_history.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    eval_history.reverse()

    outbox = [{"name": p.name, "text": p.read_text(encoding="utf-8")[:400]}
              for p in sorted((home / "outbox").glob("*.txt"), reverse=True)[:20]]

    # --- PostgreSQL introspection: actual columns, rows, and indexes.
    def table_info(name):
        info = conn.execute(
            """SELECT column_name, data_type, udt_name
                 FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=%s
                ORDER BY ordinal_position""",
            (name,),
        ).fetchall()
        cols = [r["column_name"] for r in info]
        types = {r["column_name"]: (r["udt_name"] or r["data_type"]) for r in info}
        count = conn.execute(
            pg_sql.SQL("SELECT COUNT(*) AS count FROM {}").format(pg_sql.Identifier(name))
        ).fetchone()["count"]
        # up to 200 newest rows so each table has its own scrollable tab
        order = pg_sql.SQL(" ORDER BY id DESC") if "id" in cols else pg_sql.SQL("")
        query = pg_sql.SQL("SELECT * FROM {}{}").format(pg_sql.Identifier(name), order)
        sample = [dict(r) for r in conn.execute(query + pg_sql.SQL(" LIMIT 200")).fetchall()]
        return {"name": name, "columns": cols, "types": types, "count": count, "sample": sample}

    all_tables = [r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=current_schema() ORDER BY table_name"
    ).fetchall()]
    identity = conn.execute(
        "SELECT current_database() AS database, current_schema() AS schema"
    ).fetchone()
    size = conn.execute(
        """SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) AS size
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relkind IN ('r', 'm')"""
    ).fetchone()["size"]
    indexes = [r["indexname"] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() ORDER BY indexname"
    ).fetchall()]
    db_info = {
        "path": f"{identity['database']} / {identity['schema']}",
        "database": identity["database"],
        "schema": identity["schema"],
        "size": size,
        "tables": [table_info(n) for n in all_tables if n != "otto_meta"],
        "fts": [name for name in indexes if name.endswith("_search_idx")],
        "indexes": indexes,
        "all_tables": all_tables,
    }

    # Peek at the shared agent WITHOUT building one — a page load should never
    # pay for an agent nobody has chatted with yet.
    live = browser_agent.current()
    current_session = live.session.session_id if live is not None else dash_session()
    from otto.tasks import TaskStore

    current_tasks = TaskStore(conn).list(current_session)

    # --- graph workflows: topology straight from the engine (never hand-drawn,
    # so the picture can't drift) + quick/full split from the trace events
    from otto.graph.workflows.gather import gather_topology
    from otto.graph.workflows.triage import triage_topology
    graph_routes = [e.get("target") for e in events if e.get("type") == "route"]
    # The last few completed runs, newest first. Overview needs this because the
    # two workflows are two different JOBS with different triggers — triage runs
    # itself on every message, gather runs when you ask — so "which chart is
    # relevant right now" is a question only the trace can answer. Rendering a
    # fixed workflow there showed triage forever, seconds after a gather ran.
    graph_runs = [{"workflow": e.get("workflow"), "ms": e.get("ms"),
                   "at": e.get("ts"), "steps": e.get("steps"),
                   "path": e.get("path") or [], "error": e.get("error")}
                  for e in events if e.get("type") == "graph_end"][-8:][::-1]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "home": str(home.resolve()),
        "provider": settings.provider,
        "model": settings_info()["model"],
        "stats": {
            "turns": len(turns),
            "tool_calls": sum(len(t["tools"]) for t in turns),
            "tool_errors": sum(1 for t in turns for x in t["tools"] if x["status"] == "error"),
            "gate_skips": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "skip"),
            "gate_retrieves": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "retrieve"),
            "tokens_in": sum(c.get("usage", {}).get("in", 0) for t in turns for c in t["llm_calls"]),
            "tokens_out": sum(c.get("usage", {}).get("out", 0) for t in turns for c in t["llm_calls"]),
            "cost": round(total_cost, 4),
            "latency_avg": int(sum(latencies) / len(latencies)) if latencies else 0,
            "latency_p95": pct(0.95),
            "trace_files": len(trace_files),
        },
        "turns": turns[::-1][:50],
        "wake_scans": wake_scans[::-1][:25],
        # last raw trace lines, so Ops shows traces inline (no folder needed)
        "trace_tail": [{"type": e.get("type"), "ts": e.get("ts"),
                        "detail": (e.get("user_message") or e.get("decision") or e.get("tool")
                                   or e.get("reply") or "")}
                       for e in events[-18:]][::-1],
        "trace_file": (trace_files[-1].name if trace_files else None),
        "trace_errors": trace_errors,
        "facts": rows("SELECT id, subject, content, source, created_at FROM facts ORDER BY id DESC"),
        "episodes": episodes_data["items"],
        "episodes_source": episodes_data["source"],
        "episodes_error": episodes_data["error"],
        "soul": (home / "SOUL.md").read_text(encoding="utf-8") if (home / "SOUL.md").exists() else "",
        "chat_pending": conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated=false").fetchone()[0],
        "chat_log": rows("SELECT role, content, consolidated, source, session_id, created_at FROM chat_log ORDER BY id DESC LIMIT 80")[::-1],
        "sessions": session_list(conn),
        "current_session": current_session,
        "tasks": current_tasks,
        "consolidate_every": settings.consolidate_every,
        "calendar": rows('SELECT title, start, "end", attendees, created_at FROM calendar_events ORDER BY start'),
        "knowledge": rows(
            "SELECT id, title, source, mime_type, chunk_count, created_at "
            "FROM knowledge_documents ORDER BY created_at DESC"
        ),
        "outbox": outbox,
        "skills": skills,
        "eval_report": eval_report,
        "eval_history": eval_history,
        "graph": {
            # NOTE: `enabled` gates TRIAGE ONLY — the per-message front door.
            # `otto gather` is a routine you run yourself and ignores this flag
            # entirely, so the UI must not say "off = no graphs run".
            "enabled": settings.graph_workflows,
            "workflows": [triage_topology(), gather_topology()],
            "runs": graph_runs,
            "stats": {"quick": sum(1 for t in graph_routes if t == "quick_reply"),
                      "full": sum(1 for t in graph_routes if t == "full_agent")},
        },
        "db": db_info,
        "settings": settings_info(),
        "tools": tools_info(),
        "usage": usage_summary(home),
    }
    conn.close()
    return payload


def _rel_to_home(path, home) -> str:
    """Path relative to OTTO_HOME if it lives there, else the repo-relative
    'skills/...' path — either way something reveal_path can open."""
    try:
        return str(path.resolve().relative_to(home.resolve()))
    except ValueError:
        return str(path)


def session_list(conn) -> list[dict]:
    """One row per conversation for the chat-history picker: id, its first user
    message (the title), message count, newest first. Sessions are just a
    session_id label on chat_log rows — the same table, no new storage."""
    groups = conn.execute(
        """SELECT session_id, COUNT(*) AS messages, MAX(created_at) AS last_at
           FROM chat_log GROUP BY session_id ORDER BY last_at DESC"""
    ).fetchall()
    out = []
    for g in groups:
        sid = g["session_id"]
        first = conn.execute(
            "SELECT content FROM chat_log WHERE session_id=%s AND role='user' ORDER BY id LIMIT 1",
            (sid,),
        ).fetchone()
        last = conn.execute(
            "SELECT role, content FROM chat_log WHERE session_id=%s ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
        sources = [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM chat_log WHERE session_id=%s", (sid,)).fetchall()]
        preview = ""
        if last:
            preview = ("you: " if last["role"] == "user" else "otto: ") + last["content"][:80]
        out.append({"id": sid,
                    "title": (first["content"][:60] if first else "(empty)"),
                    "last": preview,
                    "sources": sources,
                    "messages": g["messages"],
                    "last_at": g["last_at"]})
    return out


# A tool's origin, for grouping in the Tools tab (name → category).
_FLAGSHIP = {"create_event", "list_events", "save_note", "send_message"}
_SELFMGMT = {
    "load_skill", "load_skill_resource", "run_skill_script", "copy_skill_asset",
    "manage_memory", "update_soul", "create_skill",
}
_KNOWLEDGE = {"search_knowledge"}
_TASKS = {"task_create", "task_update", "task_get", "task_list"}
_SUBAGENTS = {"agent_spawn", "delegate_task"}
_APPLE = {"read_apple_calendar", "read_apple_mail", "create_reminder", "create_note"}
_WEB = {"search_web"}
_GENERAL = {
    "read_file", "write_file", "apply_patch", "list_files", "search_files",
    "run_command", "python", "fetch_url",
}


def _tool_source(name: str, mcp_servers: list[str]) -> str:
    if name in _FLAGSHIP:
        return "flagship"
    if name in _WEB:
        return "web"
    if name in _GENERAL:
        return "general"
    if name in _SELFMGMT:
        return "self-management"
    if name in _KNOWLEDGE:
        return "knowledge"
    if name in _TASKS:
        return "tasks"
    if name in _SUBAGENTS:
        return "subagents"
    if name in _APPLE:
        return "apple"
    if any(name.startswith(f"{s}_") for s in mcp_servers):
        return "mcp"
    return "other"


def tools_info() -> dict:
    """The agent's available tools + any configured MCP servers — so the Tools
    tab shows CAPABILITIES, not just the artifacts tool calls produced. Reflects
    the live agent's registry when one exists (exact), else builds a display-only
    catalog (no MCP subprocess is spawned just to render the page)."""
    settings = load_settings()
    settings.ensure_home()
    mcp = {"configured": False, "servers": [], "live": False}
    mcp_path = settings.home / "mcp.json"
    if mcp_path.exists():
        mcp["configured"] = True
        try:
            mcp["servers"] = [s.get("name", "?") for s in json.loads(mcp_path.read_text(encoding="utf-8")).get("servers", [])]
        except (json.JSONDecodeError, OSError):
            pass

    catalog = []
    display_conn = None
    live = browser_agent.current()
    if live is not None:
        mcp["live"] = getattr(live, "mcp_bridge", None) is not None
        tools = list(live.tools._tools.values())
    else:
        # Display-only: same tools minus MCP (building the real registry would
        # start MCP servers, which we don't want on a 5-second poll).
        from otto.memory import Memory
        from otto.tasks import TaskStore
        from otto.tools import (
            calendar,
            general,
            knowledge,
            memory_admin,
            messages,
            notes,
            search,
            tasks,
        )

        conn = connect(settings.home)
        display_conn = conn
        try:
            # Notion mode: reuse the dashboard's one cached client instead of
            # letting Memory() build a fresh one per poll (issue #20).
            episode_store = None
            if settings.episodic_store == "notion":
                with _notion_lock:
                    episode_store = _get_notion_store()
            mem = Memory(conn, settings, None, episode_store=episode_store)
        except Exception:
            # A misconfigured optional Notion backend must not take
            # the dashboard down — drop the memory-admin tools from the
            # display-only catalog instead.
            mem = None
        tools = general.make_tools(settings) + [calendar.make_tool(
                     conn,
                     settings.home,
                     apple_calendar=settings.apple_calendar,
                     google_calendar=settings.google_calendar,
                     google_calendar_id=settings.google_calendar_id,
                 ),
                 calendar.make_list_tool(conn),
                 notes.make_tool(conn), messages.make_tool(settings.home),
                 search.make_tool(),
                 memory_admin.make_update_soul_tool(settings)]
        task_store = TaskStore(conn)
        tools += tasks.make_tools(task_store, lambda: dash_session())
        if mem is not None:
            tools += [memory_admin.make_load_skill_tool(mem),
                      memory_admin.make_load_skill_resource_tool(mem),
                      memory_admin.make_run_skill_script_tool(mem, settings),
                      memory_admin.make_copy_skill_asset_tool(mem, settings),
                      memory_admin.make_manage_memory_tool(mem),
                      memory_admin.make_create_skill_tool(settings, mem)]
            if mem.knowledge.has_documents():
                tools.append(knowledge.make_tool(mem.knowledge))
        if settings.apple_tools:
            from otto.tools import apple

            tools += apple.make_tools()
        if settings.experimental:
            # Mirror build_registry: without this the catalog LIES after you
            # flip the experimental toggle — delegate_task is missing until the
            # first chat turn builds the real agent, so it looks like the
            # switch did nothing.
            from otto.tools import experimental as experimental_tools

            tools += experimental_tools.make_tools(settings)
        # Catalog-only construction: the closure is never executed here, so a
        # model client is unnecessary. This keeps the pre-chat Tools page honest
        # without starting an agent or an MCP subprocess.
        from otto.permissions import PermissionPolicy
        from otto.tools import subagents
        from otto.tools.registry import ToolRegistry

        display_registry = ToolRegistry(PermissionPolicy(settings.workspace))
        for tool in tools:
            display_registry.register(tool)
        tools.append(
            subagents.make_tool(
                settings, None, display_registry, task_store, lambda: dash_session()
            )
        )
    for t in tools:
        catalog.append({"name": t.name, "description": t.description,
                        "source": _tool_source(t.name, mcp["servers"])})
    catalog.sort(key=lambda c: (c["source"], c["name"]))
    from otto.tools.experimental import PLANNED

    if display_conn is not None:
        display_conn.close()
    return {"catalog": catalog, "mcp": mcp, "apple_on": settings.apple_tools,
            "planned": PLANNED}   # whiteboard boxes not wired in yet (coming soon)


def run_query(payload: dict) -> dict:
    """A tiny PostgreSQL console in a read-only transaction, capped at 200 rows."""
    sql = (payload.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        return {"error": "Type a SELECT query."}
    low = sql.lower()
    if not (low.startswith(("select", "with"))):
        return {"error": "Only SELECT (or WITH … SELECT) queries are allowed."}
    if ";" in sql:
        return {"error": "One statement at a time (no semicolons)."}
    settings = load_settings()
    settings.ensure_home()
    c = None
    try:
        c = connect(settings.home)
        c.execute("BEGIN READ ONLY")
        c.execute("SET LOCAL statement_timeout = '3s'")
        cur = c.execute(sql)
        cols = [d.name for d in cur.description] if cur.description else []
        data = [[str(r[i]) if r[i] is not None else "" for i in range(len(cols))]
                for r in cur.fetchmany(200)]
        return {"columns": cols, "rows": data}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if c is not None:
            c.close()


_whisper = None
_whisper_lock = threading.Lock()


def transcribe_audio(raw: bytes) -> dict:
    """Server-side speech-to-text for the dashboard mic button — the SAME local
    Whisper (`make voice` uses it), so voice works in the browser without any
    cloud. Needs the [voice] extra. Returns {text} or a friendly {error}."""
    if not raw:
        return {"error": "no audio received"}
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"error": "voice isn't installed — run: pip install -e '.[voice]'"}
    global _whisper
    import os as _os
    import tempfile

    with _whisper_lock:
        if _whisper is None:
            _whisper = WhisperModel(os.getenv("OTTO_WHISPER_MODEL", "base"), compute_type="int8")
    # the browser sends WAV (PCM) — Whisper/PyAV decode it reliably (WebM/Opus
    # from MediaRecorder often fails to decode).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(raw)
    try:
        segments, _ = _whisper.transcribe(tmp.name)
        return {"text": " ".join(s.text for s in segments).strip()}
    except Exception as exc:
        return {"error": f"transcription failed: {exc}"}
    finally:
        try:
            _os.unlink(tmp.name)
        except OSError:
            pass


def _thread_history(conn, sid: str) -> list[dict]:
    """The ONE way to load a thread for the chat dock: role + content + the
    per-turn meta (gate/stats/tools/model) so every card renders in full.
    id '__all__' returns the whole cross-thread timeline (like the Loop tab,
    but as chat). Every history-loading path goes through here so they can't
    drift apart (they used to: 'switch' dropped meta and showed only text)."""
    if sid == "__all__":
        rows = conn.execute(
            "SELECT role, content, meta FROM chat_log ORDER BY id DESC LIMIT 200"
        ).fetchall()[::-1]
    else:
        rows = conn.execute(
            "SELECT role, content, meta FROM chat_log WHERE session_id=%s ORDER BY id",
            (sid,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"],
             "meta": (json.loads(r["meta"]) if isinstance(r["meta"], str) else r["meta"])}
            for r in rows]


def session_action(payload: dict) -> dict:
    """Chat history control: start a new conversation, switch to a past one, or
    read a conversation's history (read-only, for the live inbox). Sessions live
    in chat_log."""
    action = payload.get("action")
    if action == "history":
        # read-only view of a conversation — never touches the agent, so the
        # dashboard can poll it live (e.g. to show new gateway messages arrive).
        settings = load_settings()
        settings.ensure_home()
        conn = connect(settings.home)
        sid = payload.get("id") or "default"
        return {"ok": True, "session_id": sid, "history": _thread_history(conn, sid)}
    with agent_lock:
        agent = get_agent()
        if action == "new":
            sid = datetime.now().strftime("s-%Y%m%d-%H%M%S")
            agent.session.start_new(sid)
            return {"ok": True, "session_id": sid, "history": []}
        if action == "switch":
            sid = payload.get("id") or "default"
            agent.session.switch(sid)
            # Same meta-rich rows as the read-only "history" action, so a
            # switched thread renders its full turn cards (gate/stats/tools/
            # model) — not just the text. (These two paths used to disagree.)
            return {"ok": True, "session_id": sid, "history": _thread_history(agent.conn, sid)}
    return {"error": f"unknown action {action}"}


def _editor_cmd() -> list[str] | None:
    """The user's code editor CLI: $OTTO_EDITOR, then cursor, then code."""

    custom = os.getenv("OTTO_EDITOR")
    if custom and shutil.which(custom):
        return [custom]
    for cli in ("cursor", "code"):
        if shutil.which(cli):
            return [cli]
    return None


def reveal_path(rel: str) -> dict:
    """Open a file/folder under OTTO_HOME — in the user's code editor if one
    is on PATH (cursor/code/$OTTO_EDITOR), otherwise reveal in Finder.
    Restricted to paths inside OTTO_HOME."""
    import subprocess
    import sys

    settings = load_settings()
    settings.ensure_home()
    home = settings.home.resolve()
    target = (home / (rel or ".")).resolve()
    if target != home and home not in target.parents:
        return {"error": "path is outside the .otto home"}
    if not target.exists():
        return {"error": f"not found: {target}"}

    editor = _editor_cmd()
    if editor and target.is_file():
        subprocess.run([*editor, str(target)], check=False)
        return {"ok": True, "opened_in": editor[0], "path": str(target)}
    if sys.platform != "darwin":
        return {"error": f"no editor found and reveal is macOS-only — the path is {target}"}
    subprocess.run(
        ["open", "-R", str(target)] if target.is_file() else ["open", str(target)],
        check=False,
    )
    return {"ok": True, "revealed": str(target)}


def memory_action(payload: dict) -> dict:
    """Human CRUD on memory from the dashboard: update/delete facts & episodes,
    rewrite SOUL.md. Writes the same PostgreSQL schema the agent uses; changes
    are live for the next agent turn."""
    from otto.memory.episodic.store import PostgresEpisodeStore
    from otto.memory.semantic.store import PostgresFactStore

    settings = load_settings()
    settings.ensure_home()
    action = payload.get("action")
    if action == "save_soul":
        text = (payload.get("content") or "").strip()
        if not text:
            return {"error": "SOUL cannot be empty"}
        (settings.home / "SOUL.md").write_text(text + "\n")
        return {"ok": True}
    if action == "save_skill":
        # Edit any loaded SKILL.md by hand (same file the agent's create_skill
        # writes) — repo skills and home skills alike. Sandboxed to the two
        # skills folders; validates the frontmatter before writing.
        from pathlib import Path

        from otto.memory import bundled_skill_dirs
        from otto.memory.procedural.loader import _parse_text

        text = (payload.get("content") or "").strip()
        dest = Path(payload.get("path") or "").resolve()
        allowed = [d.resolve() for d in bundled_skill_dirs()] + [(settings.home / "skills").resolve()]
        if dest.name != "SKILL.md" or not any(a in dest.parents for a in allowed):
            return {"error": "can only edit SKILL.md files inside the skills folders"}
        if _parse_text(text, dest) is None:
            return {"error": "invalid SKILL.md — needs a name and description in the frontmatter"}
        dest.write_text(text.rstrip() + "\n", encoding="utf-8")
        return {"ok": True}

    conn = connect(settings.home)
    facts, episodes = PostgresFactStore(conn), PostgresEpisodeStore(conn)
    if action == "delete_episode" and settings.episodic_store == "notion":
        global _notion_episodes
        with _notion_lock:
            ok = _get_notion_store().delete(str(payload.get("id", "")))
            # bust the TTL cache so the next collect() refetches — otherwise a
            # deleted episode would linger on the page for up to 30s
            _notion_episodes = None
        return {"ok": ok}
    try:
        rid = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return {"error": "bad id"}
    if action == "update_fact":
        return {"ok": facts.update(rid, payload.get("content", ""), payload.get("subject") or None)}
    if action == "delete_fact":
        return {"ok": facts.delete(rid)}
    if action == "delete_episode":
        return {"ok": episodes.delete(rid)}
    return {"error": f"unknown action {action}"}




def events_since(cursor):
    """New trace events past `cursor` (a line count in today's trace file).
    Any gateway — browser, CLI, voice, Discord, or WhatsApp — appends to this same file,
    so the live diagram lights up for all of them. cursor=None returns just
    the current tail so the browser starts fresh instead of replaying history."""
    settings = load_settings()
    settings.ensure_home()
    path = settings.home / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    if not path.exists():
        return {"events": [], "cursor": 0}
    try:
        lines = list(iter_trace_lines(path))
    except TraceEncodingError as exc:
        return {"events": [], "cursor": 0, "error": str(exc)}
    if cursor is None or cursor < 0 or cursor > len(lines):
        return {"events": [], "cursor": len(lines)}
    out = []
    for ln in lines[cursor:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return {"events": out, "cursor": len(lines)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, *, no_cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The frontend files (app.js/style.css) change as we develop; without
        # this the browser serves a stale cached copy and edits look "missing".
        if no_cache:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            self._send(json.dumps(collect(), default=str).encode(), "application/json")
        elif self.path == "/api/compare/history":
            runs = compare_history.load_runs(load_settings().home)
            self._send(json.dumps(history_response(runs)).encode(), "application/json")
        elif self.path.startswith("/api/models"):
            from urllib.parse import parse_qs, urlparse

            prov = parse_qs(urlparse(self.path).query).get("provider", [None])[0]
            self._send(json.dumps(list_models(prov)).encode(), "application/json")
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse

            raw = parse_qs(urlparse(self.path).query).get("cursor", [None])[0]
            cursor = int(raw) if raw and raw.lstrip("-").isdigit() else None
            self._send(json.dumps(events_since(cursor)).encode(), "application/json")
        elif self.path.startswith("/api/reveal"):
            from urllib.parse import parse_qs, unquote, urlparse

            rel = unquote(parse_qs(urlparse(self.path).query).get("path", [""])[0])
            self._send(json.dumps(reveal_path(rel)).encode(), "application/json")
        elif self.path.startswith("/static/"):
            self._serve_static(self.path)
        else:
            self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def _serve_static(self, path: str) -> None:  # the frontend files
        name = path.split("/static/", 1)[1].split("?")[0]
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {".css": "text/css", ".js": "text/javascript",
                 ".html": "text/html; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        self._send(target.read_bytes(), ctype, no_cache=True)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # /api/voice takes a raw audio blob, not JSON — handle it first.
        if self.path == "/api/voice":
            raw = self.rfile.read(length)
            self._send(json.dumps(transcribe_audio(raw)).encode(), "application/json")
            return
        # /api/chat/stream streams harness events (SSE) as the turn runs.
        if self.path == "/api/chat/stream":
            payload = json.loads(self.rfile.read(length) or "{}")
            message = (payload.get("message") or "").strip()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser navigated away mid-stream — fine

            if not message:
                emit("done", {"error": "empty message"})
                return
            try:
                chat_stream(message, emit, interactive=True)
            except Exception as exc:  # surface as a terminal event, don't 500
                emit("done", {"error": f"{type(exc).__name__}: {exc}"})
            return
        # /api/compare/stream races several models, emitting each result as it lands.
        if self.path == "/api/compare/stream":
            payload = json.loads(self.rfile.read(length) or "{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                compare_stream((payload.get("message") or "").strip(), payload.get("models") or [],
                               emit, judge=bool(payload.get("judge")), coding=bool(payload.get("coding")),
                               judge_spec=(payload.get("judge_model") or ""), apple=bool(payload.get("apple")))
            except Exception as exc:
                emit("done", {"error": f"{type(exc).__name__}: {exc}"})
            return
        if self.path == "/api/graph/stream":
            payload = json.loads(self.rfile.read(length) or "{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            graph_stream(payload, emit)
            return
        routes = {"/api/chat": None, "/api/permission": permission_action,
                  "/api/memory": memory_action, "/api/settings": apply_settings,
                  "/api/query": run_query, "/api/session": session_action, "/api/pin": pin_action,
                  "/api/compare/clear": compare_clear,
                  "/api/compare/regrade": compare_regrade, "/api/compare/delete_run": compare_delete_run}
        if self.path not in routes:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.loads(self.rfile.read(length) or "{}")
        try:
            if self.path == "/api/chat":
                message = (payload.get("message") or "").strip()
                out = chat(message) if message else {"error": "empty message"}
            else:
                out = routes[self.path](payload)
        except Exception as exc:  # surface, don't 500 — the browser shows it
            out = {"error": f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(out, default=str).encode(), "application/json")

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> None:
    # Port precedence: OTTO_DASHBOARD_PORT, then the conventional PORT (used by
    # deploy platforms and IDE preview panes), then 7777. If it's taken, walk on.
    base = int(os.getenv("OTTO_DASHBOARD_PORT") or os.getenv("PORT") or PORT)
    for port in range(base, base + 10):  # walk past a busy port instead of crashing
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            print(f"port {port} busy, trying {port + 1}…")
            continue
        try:
            from otto.gateway.discord import start_in_background as start_discord

            if start_discord():
                print("Discord gateway → listening in the background (server messages land here too)")
        except Exception as exc:  # noqa: BLE001 — never let a gateway block the dashboard
            print(f"(discord) not started: {exc}")
        # Each gateway gets its OWN try: a Discord failure must not skip WhatsApp.
        try:
            from otto.gateway.whatsapp import start_in_background as wa_background

            if wa_background():
                print("WhatsApp gateway → listening in the background (webhook on port 5000)")
        except Exception as exc:  # noqa: BLE001 — never let a gateway block the dashboard
            print(f"(whatsapp) not started: {exc}")
        print(f"Otto dashboard → http://localhost:{port}  (Ctrl-C to stop)")
        server.serve_forever()
        return
    raise SystemExit(f"no free port in {base}–{base + 9}")


if __name__ == "__main__":
    main()
