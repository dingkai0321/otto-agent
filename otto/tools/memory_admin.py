"""Tools that let the agent manage its OWN memory — so it feels like a personal
assistant that learns, not a black box. Seven tools:

  load_skill     — load one full SKILL.md after seeing the compact catalog
  manage_memory  — search / update / delete facts and episodes (the CRUD)
  update_soul    — append a durable behaviour rule to SOUL.md (its persona)
  create_skill   — write a new SKILL.md, so the agent builds its own procedures
  load_skill_resource — read one referenced file from an active skill
  run_skill_script    — execute an active skill's bundled script with approval
  copy_skill_asset    — copy an active skill template/asset into the workspace

load_skill implements progressive disclosure: the catalog is already in the
system prompt, and the tool returns one full SKILL.md as a tool result.
Everything writes to the same local files the dashboard shows; nothing leaves
the machine. update_soul is append-only (the agent can't delete its own honesty
rules); a human does full rewrites in the dashboard.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from otto.memory import bundled_skill_dirs
from otto.memory.procedural.loader import _parse_text, routing_warnings
from otto.tools.registry import Tool

SOUL_MAX = 8000
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def make_load_skill_tool(memory) -> Tool:
    def load_skill(name: str, *, _hooks=None) -> str:
        skill = memory.skills.load(name)
        if skill is None:
            available = ", ".join(s.name for s in memory.skills.skills) or "none"
            return f"Skill not found: {name}. Available skills: {available}"
        if _hooks is not None:
            _hooks.state.setdefault("active_skills", set()).add(skill.name)
            _hooks.state.setdefault("active_skill_contents", {})[skill.name] = skill.content
        resources = "\n".join(
            f"- {item.path} ({item.kind}, {item.size} bytes)" for item in skill.resources
        ) or "- (none)"
        tools = ", ".join(skill.required_tools) or "(no skill-gated tools)"
        return (
            f"Loaded skill '{skill.name}'. Follow these instructions for the current task:\n\n"
            f"{skill.content}\n\n"
            f"Skill root: {skill.root}\n"
            f"Activated tools: {tools}\n"
            f"Available bundled resources (load only when needed with "
            f"load_skill_resource):\n{resources}"
        )

    return Tool(
        name="load_skill",
        description=(
            "Load the full SKILL.md instructions for one skill listed in the system "
            "prompt's skill catalog. Call this when a listed skill is relevant, using "
            "its exact name. The returned instructions apply to the current task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name from the catalog"}
            },
            "required": ["name"],
        },
        fn=load_skill,
        wants_hooks=True,
    )


def _active_skill(memory, name: str, hooks):
    skill = memory.skills.load(name)
    if skill is None:
        return None, f"Skill not found: {name}."
    if hooks is not None and skill.name not in hooks.state.get("active_skills", set()):
        return None, f"Skill '{skill.name}' is not active — call load_skill first."
    return skill, ""


def make_load_skill_resource_tool(memory) -> Tool:
    def load_skill_resource(
        name: str,
        path: str,
        offset: int = 0,
        max_chars: int = 20_000,
        *,
        _hooks=None,
    ) -> str:
        skill, error = _active_skill(memory, name, _hooks)
        if error:
            return f"Error: {error}"
        try:
            resource = skill.resolve_resource(path)
        except (ValueError, FileNotFoundError) as exc:
            return f"Error: cannot load skill resource — {exc}."
        relative = resource.relative_to(skill.root).as_posix()
        kind = relative.split("/", 1)[0]
        if kind in {"assets", "templates"}:
            return (
                f"Skill asset '{relative}' is not injected as text. "
                f"Path: {resource} ({resource.stat().st_size} bytes). "
                "Use copy_skill_asset when it is needed in the workspace."
            )
        try:
            text = resource.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Skill resource is binary and cannot enter context: {resource}"
        start = max(0, int(offset or 0))
        limit = max(1_000, min(int(max_chars or 20_000), 50_000))
        fragment = text[start:start + limit]
        end = start + len(fragment)
        suffix = f" More remains; continue at offset={end}." if end < len(text) else ""
        return (
            f"Loaded resource '{relative}' from skill '{skill.name}' "
            f"(chars {start}:{end} of {len(text)}).{suffix}\n\n{fragment}"
        )

    return Tool(
        name="load_skill_resource",
        description=(
            "Read one referenced text file from a skill already activated with load_skill. "
            "Use only for the specific reference or script source needed now; assets/templates "
            "return metadata rather than entering context. Supports offset pagination."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact active skill name"},
                "path": {"type": "string", "description": "Package-relative resource path"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
            },
            "required": ["name", "path"],
        },
        fn=load_skill_resource,
        wants_hooks=True,
    )


def make_run_skill_script_tool(memory, settings) -> Tool:
    def run_skill_script(
        name: str,
        path: str,
        args: list[str] | None = None,
        timeout: int = 30,
        *,
        _hooks=None,
    ) -> str:
        skill, error = _active_skill(memory, name, _hooks)
        if error:
            return f"Error: {error}"
        try:
            script = skill.resolve_resource(path)
        except (ValueError, FileNotFoundError) as exc:
            return f"Error: cannot run skill script — {exc}."
        relative = script.relative_to(skill.root)
        if not relative.parts or relative.parts[0] != "scripts":
            return "Error: only files under the skill's scripts/ directory are executable."
        arguments = [str(value) for value in (args or [])]
        if len(arguments) > 32 or any(len(value) > 2_000 for value in arguments):
            return "Error: script arguments exceed the safety limit."
        if script.suffix.lower() == ".py":
            command = [sys.executable, str(script), *arguments]
        elif script.suffix.lower() in {".sh", ".bash"}:
            command = ["/bin/sh", str(script), *arguments]
        else:
            return "Error: only .py, .sh, and .bash skill scripts are executable."
        try:
            completed = subprocess.run(
                command,
                cwd=Path(settings.workspace).resolve(),
                capture_output=True,
                text=True,
                timeout=max(1, min(int(timeout or 30), 120)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "Error: skill script timed out."
        output = (completed.stdout + completed.stderr).strip()
        if len(output) > 20_000:
            output = output[:20_000] + "\n[output truncated]"
        return f"Skill script exited {completed.returncode}.\n{output or '(no output)'}"

    return Tool(
        name="run_skill_script",
        description=(
            "Run a .py/.sh script bundled under scripts/ in an already active skill. "
            "Requires explicit user approval and runs without shell interpolation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["name", "path"],
        },
        fn=run_skill_script,
        wants_hooks=True,
        permission="ask",
        permission_reason="executing code bundled with an Agent Skill",
    )


def make_copy_skill_asset_tool(memory, settings) -> Tool:
    def copy_skill_asset(name: str, path: str, destination: str, *, _hooks=None) -> str:
        skill, error = _active_skill(memory, name, _hooks)
        if error:
            return f"Error: {error}"
        try:
            source = skill.resolve_path(path, allow_directory=True)
        except (ValueError, FileNotFoundError) as exc:
            return f"Error: cannot copy skill asset — {exc}."
        relative = source.relative_to(skill.root)
        if not relative.parts or relative.parts[0] not in {"assets", "templates"}:
            return "Error: copy_skill_asset only accepts assets/ or templates/ files."
        workspace = Path(settings.workspace).expanduser().resolve()
        target = Path(destination).expanduser()
        target = target.resolve() if target.is_absolute() else (workspace / target).resolve()
        if not target.is_relative_to(workspace):
            return f"Error: destination must stay inside workspace {workspace}."
        if target.exists():
            return f"Error: destination already exists: {target}."
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return f"Copied skill asset to {target}."

    return Tool(
        name="copy_skill_asset",
        description=(
            "Copy one file from an active skill's assets/ or templates/ directory into the "
            "workspace without overwriting. Requires explicit user approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["name", "path", "destination"],
        },
        fn=copy_skill_asset,
        wants_hooks=True,
        permission="ask",
        permission_reason="copying a bundled Skill asset into the workspace",
    )


def make_manage_memory_tool(memory) -> Tool:
    facts = memory.facts
    episodes = memory.episodes

    def manage_memory(action: str, kind: str = "fact", id: int = 0,
                      query: str = "", content: str = "", subject: str = "") -> str:
        action = (action or "").lower()
        if action == "search":
            if kind == "episode":
                rows = episodes.list(20)
                if query:
                    rows = [r for r in rows if query.lower() in r["summary"].lower()]
                return "\n".join(f"#{r['id']} ({r['happened_at']}) {r['summary']}" for r in rows[:8]) or "no episodes"
            rows = facts.search_with_ids(query, 8) if hasattr(facts, "search_with_ids") else []
            return "\n".join(f"#{r['id']} [{r['subject']}] {r['content']}" for r in rows) or "no matching facts"
        if action == "update":
            if kind != "fact":
                return "Only facts can be updated (episodes are historical)."
            ok = facts.update(int(id), content, subject or None)
            return f"Updated fact #{id}." if ok else f"No fact with id {id}."
        if action == "delete":
            if kind == "episode":
                # PostgreSQL ids are ints; notion page ids are UUID strings — coerce by shape.
                rid = int(id) if str(id).isdigit() else str(id)
                return f"Deleted episode #{id}." if episodes.delete(rid) else f"No episode with id {id}."
            return f"Deleted fact #{id}." if facts.delete(int(id)) else f"No fact with id {id}."
        return "action must be one of: search, update, delete"

    return Tool(
        name="manage_memory",
        description=(
            "Search, correct, or delete the user's long-term memory (facts and episodes). "
            "ALWAYS search first to get the id, then update or delete that id. "
            "Use when the user says something you remember is wrong or should be forgotten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "update", "delete"]},
                "kind": {"type": "string", "enum": ["fact", "episode"], "description": "default fact"},
                "id": {"type": ["integer", "string"],
                       "description": "row id (from a prior search); a number for PostgreSQL, a page id string when the notion backend is active"},
                "query": {"type": "string", "description": "keywords for search"},
                "content": {"type": "string", "description": "new text for update"},
                "subject": {"type": "string", "description": "optional new subject for a fact update"},
            },
            "required": ["action"],
        },
        fn=manage_memory,
    )


def make_update_soul_tool(settings) -> Tool:
    from otto.runtime.session import load_soul

    def update_soul(rule: str) -> str:
        rule = rule.strip().lstrip("-").strip()
        if not rule:
            return "Nothing to add."
        path = settings.home / "SOUL.md"
        text = load_soul(settings)  # ensures the file exists
        if len(text) > SOUL_MAX:
            return "SOUL.md is at its size limit — edit it in the dashboard instead."
        if "## Learned rules" not in text:
            text = text.rstrip() + "\n\n## Learned rules\n"
        text = text.rstrip() + f"\n- {rule}\n"
        path.write_text(text, encoding="utf-8")
        return f"Noted, I'll remember to: {rule}"

    return Tool(
        name="update_soul",
        description=(
            "Save a durable rule about how you should behave for this user (their "
            "preferences and standing instructions). Appends to your persona; takes "
            "effect next turn. Use when the user tells you how they want you to act."
        ),
        input_schema={
            "type": "object",
            "properties": {"rule": {"type": "string", "description": "one behaviour rule, imperative"}},
            "required": ["rule"],
        },
        fn=update_soul,
    )


def make_create_skill_tool(settings, memory) -> Tool:
    def create_skill(
        name: str,
        description: str,
        body: str,
        required_tools: list[str] | None = None,
    ) -> str:
        name = (name or "").strip().lower().replace(" ", "-")
        if not _SLUG.match(name):
            return "Skill name must be a short slug like 'weekly-review' (lowercase, hyphens)."
        dest = settings.home / "skills" / name / "SKILL.md"
        # never silently overwrite an existing skill (built-in or user)
        if dest.exists() or any((d / name / "SKILL.md").exists() for d in bundled_skill_dirs()):
            return f"A skill named '{name}' already exists — pick another name."
        required = list(dict.fromkeys(str(value).strip() for value in (required_tools or [])))
        if len(required) > 20 or any(
            not value or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
            for value in required
        ):
            return "required_tools must contain at most 20 valid registered tool names."
        text = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{body.strip()}\n"
        if _parse_text(text, dest) is None:
            return "That didn't validate — description must be present and non-trivial."
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        if required:
            (dest.parent / "otto.json").write_text(
                json.dumps({"required_tools": required}, indent=2) + "\n",
                encoding="utf-8",
            )
        memory.skills.refresh()  # live this session
        warnings = routing_warnings(name, description.strip(), body.strip())
        warning = f" Routing warnings: {'; '.join(warnings)}" if warnings else ""
        return (
            f"Created skill '{name}'. It will trigger on: {description.strip()}."
            f"{warning}"
        )

    return Tool(
        name="create_skill",
        description=(
            "Write a new reusable skill (a SKILL.md the agent loads when relevant) so you "
            "can repeat a workflow the user taught you. Only call this after the user agrees. "
            "body = step-by-step instructions; description = when to use it (include trigger words)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short slug, e.g. weekly-review"},
                "description": {
                    "type": "string",
                    "description": (
                        "Routing condition: what it does, when to use it, and when not to use it"
                    ),
                },
                "body": {"type": "string", "description": "the step-by-step instructions (markdown)"},
                "required_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": (
                        "Optional specialist tools whose schemas appear only after this skill loads"
                    ),
                },
            },
            "required": ["name", "description", "body"],
        },
        fn=create_skill,
    )
