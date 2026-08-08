"""DETERMINISTIC EVAL — skills use catalog-first, body-on-demand loading."""

from pathlib import Path
from types import SimpleNamespace

from evals.helpers import ScriptedClient, make_otto, response, text_block, tool_block
from otto.config import Settings
from otto.hooks import HookManager
from otto.loop.agent import run_loop
from otto.memory.procedural.loader import SkillLoader
from otto.runtime.context import ContextManager
from otto.runtime.session import Session
from otto.tools.memory_admin import (
    make_copy_skill_asset_tool,
    make_load_skill_resource_tool,
    make_load_skill_tool,
    make_run_skill_script_tool,
)
from otto.tools.registry import Tool, ToolRegistry


def _write_skill(root: Path, name: str = "demo", description: str = "Use for demo work",
                 body: str = "SECRET PROCEDURE BODY") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_catalog_contains_metadata_but_not_skill_body(tmp_path):
    _write_skill(tmp_path)
    loader = SkillLoader([tmp_path])

    catalog = loader.catalog()

    assert "`demo`: Use for demo work" in catalog
    assert "SECRET PROCEDURE BODY" not in catalog


def test_load_skill_uses_exact_registered_name_and_returns_full_file(tmp_path):
    _write_skill(tmp_path)
    memory = SimpleNamespace(skills=SkillLoader([tmp_path]))
    tool = make_load_skill_tool(memory)

    loaded = tool.fn(name="demo")

    assert "name: demo" in loaded
    assert "SECRET PROCEDURE BODY" in loaded
    assert tool.fn(name="../demo").startswith("Skill not found")


def test_system_prompt_has_catalog_but_not_body(tmp_path):
    _write_skill(tmp_path / "skills")

    class MemoryStub:
        def __init__(self):
            self.skills = SkillLoader([tmp_path / "skills"])

        def gated_retrieve(self, message, hooks=None):
            return ""

        def skill_catalog(self):
            return self.skills.catalog()

    settings = Settings(home=tmp_path / "home")
    settings.ensure_home()
    system = Session(settings, memory=MemoryStub()).build_system(
        "please do the demo work"
    )

    assert "Available skills (metadata only):" in system
    assert "`demo`: Use for demo work" in system
    assert "call `load_skill`" in system
    assert "SECRET PROCEDURE BODY" not in system


def test_skill_changes_are_visible_without_restart(tmp_path):
    path = _write_skill(tmp_path, description="First description", body="first body")
    loader = SkillLoader([tmp_path])
    assert "First description" in loader.catalog()

    path.write_text(
        "---\nname: demo\ndescription: Updated description\n---\n\nupdated body is longer\n",
        encoding="utf-8",
    )

    assert "Updated description" in loader.catalog()
    assert "updated body is longer" in loader.load("demo").content


def test_full_turn_loads_skill_body_through_tool_result(tmp_path):
    _write_skill(tmp_path / "skills")
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "self contained"}')]),
        response([tool_block("load_skill", {"name": "demo"})], "tool_use"),
        response([text_block("I followed the loaded skill.")]),
    ])
    app = make_otto(tmp_path, client=client)

    result = app.respond("please do the demo work")

    assert result.reply == "I followed the loaded skill."
    assert [call["tool"] for call in result.tool_calls] == ["load_skill"]
    assert "SECRET PROCEDURE BODY" in result.tool_calls[0]["output"]


def test_multiline_description_and_three_level_resource_index(tmp_path):
    root = tmp_path / "slides"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: slides\ndescription: >\n  Use when creating slides.\n"
        "  Do not use for plain text summaries.\n---\n\nRead references/layout.md.\n",
        encoding="utf-8",
    )
    (root / "references").mkdir()
    (root / "references" / "layout.md").write_text("EXACT LAYOUT RULE", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "theme.bin").write_bytes(b"\x00theme")
    (root / "otto.json").write_text(
        '{"required_tools": ["render_slides"]}\n', encoding="utf-8"
    )

    skill = SkillLoader([tmp_path]).load("slides")

    assert skill is not None
    assert "Do not use" in skill.description
    assert {item.path for item in skill.resources} == {
        "assets/theme.bin", "references/layout.md",
    }
    assert skill.required_tools == ("render_slides",)
    assert not skill.warnings


def test_skill_resources_are_active_only_paginated_and_path_safe(tmp_path):
    root = tmp_path / "demo"
    _write_skill(tmp_path, description=(
        "Use when doing demo work with references. Do not use for unrelated work."
    ))
    (root / "references").mkdir()
    (root / "references" / "details.md").write_text("A" * 3_000, encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "template.bin").write_bytes(b"\x00binary")
    memory = SimpleNamespace(skills=SkillLoader([tmp_path]))
    load = make_load_skill_tool(memory)
    resource = make_load_skill_resource_tool(memory)
    hooks = HookManager()

    assert "not active" in resource.fn(
        name="demo", path="references/details.md", _hooks=hooks
    )
    loaded = load.fn(name="demo", _hooks=hooks)
    assert "references/details.md" in loaded
    first = resource.fn(
        name="demo", path="references/details.md", max_chars=1_000, _hooks=hooks
    )
    assert "chars 0:1000 of 3000" in first and "offset=1000" in first
    assert "cannot load" in resource.fn(name="demo", path="../secret", _hooks=hooks)
    asset = resource.fn(name="demo", path="assets/template.bin", _hooks=hooks)
    assert "not injected as text" in asset and "copy_skill_asset" in asset


def test_skill_scripts_and_assets_are_bounded_to_active_package(tmp_path):
    root = tmp_path / "demo"
    _write_skill(tmp_path, description=(
        "Use when running a demo package. Do not use for unrelated work."
    ))
    (root / "scripts").mkdir()
    (root / "scripts" / "hello.py").write_text(
        "import sys\nprint('hello ' + sys.argv[1])\n", encoding="utf-8"
    )
    (root / "templates").mkdir()
    (root / "templates" / "starter.txt").write_text("starter", encoding="utf-8")
    memory = SimpleNamespace(skills=SkillLoader([tmp_path]))
    settings = SimpleNamespace(workspace=tmp_path / "workspace")
    settings.workspace.mkdir()
    hooks = HookManager()
    make_load_skill_tool(memory).fn(name="demo", _hooks=hooks)

    ran = make_run_skill_script_tool(memory, settings).fn(
        name="demo", path="scripts/hello.py", args=["otto"], _hooks=hooks
    )
    copied = make_copy_skill_asset_tool(memory, settings).fn(
        name="demo", path="templates/starter.txt", destination="out/starter.txt",
        _hooks=hooks,
    )

    assert "exited 0" in ran and "hello otto" in ran
    assert "Copied skill asset" in copied
    assert (settings.workspace / "out" / "starter.txt").read_text() == "starter"
    assert "inside workspace" in make_copy_skill_asset_tool(memory, settings).fn(
        name="demo", path="templates/starter.txt", destination="../escape.txt",
        _hooks=hooks,
    )


def test_skill_activation_changes_next_iteration_tool_schemas(tmp_path):
    root = tmp_path / "demo"
    _write_skill(tmp_path, description=(
        "Use when specialized demo work is requested. Do not use for ordinary work."
    ))
    (root / "otto.json").write_text(
        '{"required_tools": ["special_action"]}\n', encoding="utf-8"
    )
    memory = SimpleNamespace(skills=SkillLoader([tmp_path]))
    registry = ToolRegistry()
    registry.register(make_load_skill_tool(memory))
    registry.register(make_load_skill_resource_tool(memory))
    registry.register(Tool(
        "special_action", "special", {"type": "object"}, lambda: "done"
    ))
    registry.configure_skill_tools(memory.skills.tool_requirements())
    registry.gate_until_skill_active("load_skill_resource")

    class Recorder(ScriptedClient):
        def __init__(self, script):
            super().__init__(script)
            self.calls = []

        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return super()._create(**kwargs)

    client = Recorder([
        response([tool_block("load_skill", {"name": "demo"})], "tool_use"),
        response([text_block("ready")]),
    ])
    run_loop(
        client=client, model="test", system="test",
        messages=[{"role": "user", "content": "demo"}], tools=registry,
    )

    first = {tool["name"] for tool in client.calls[0]["tools"]}
    second = {tool["name"] for tool in client.calls[1]["tools"]}
    assert first == {"load_skill"}
    assert second == {"load_skill", "load_skill_resource", "special_action"}


def test_active_skill_is_reinjected_exactly_after_context_compaction(tmp_path):
    client = ScriptedClient([response([text_block("summary")])])
    context = ContextManager(
        client=client, model="small", home=tmp_path,
        context_window_tokens=1_000, max_output_tokens=100, buffer_tokens=0,
        keep_recent_messages=2, skill_reinject_budget_chars=10_000,
    )
    hooks = HookManager()
    hooks.state["active_skills"] = {"demo"}
    hooks.state["active_skill_contents"] = {
        "demo": "---\nname: demo\ndescription: route\n---\nEXACT ACTIVE RULE"
    }
    messages = [
        {"role": "user", "content": "old " + "x" * 4_000},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent answer"},
    ]

    report = context.maybe_compact(system="system", messages=messages, tools=[], hooks=hooks)

    assert report is not None
    assert "EXACT ACTIVE RULE" in messages[0]["content"]
    assert "<active_skills>" in messages[0]["content"]


def test_recent_exact_skill_result_is_not_duplicated_by_compaction(tmp_path):
    exact = "---\nname: demo\ndescription: route\n---\nEXACT ACTIVE RULE"
    client = ScriptedClient([response([text_block("summary")])])
    context = ContextManager(
        client=client, model="small", home=tmp_path,
        context_window_tokens=1_000, max_output_tokens=100, buffer_tokens=0,
        keep_recent_messages=3, skill_reinject_budget_chars=10_000,
    )
    hooks = HookManager()
    hooks.state["active_skill_contents"] = {"demo": exact}
    messages = [
        {"role": "user", "content": "old " + "x" * 4_000},
        {"role": "assistant", "content": "old answer"},
        {"role": "assistant", "content": [SimpleNamespace(
            type="tool_use", id="skill-1", name="load_skill", input={"name": "demo"}
        )]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "skill-1", "content": exact,
        }]},
        {"role": "assistant", "content": "continue"},
    ]

    report = context.maybe_compact(system="system", messages=messages, tools=[], hooks=hooks)

    assert report is not None
    assert str(messages).count("EXACT ACTIVE RULE") == 1


def test_prompt_separates_stable_skill_catalog_from_dynamic_context(tmp_path):
    _write_skill(
        tmp_path / "skills",
        description="Use when demo work is requested. Do not use for other tasks.",
    )

    class MemoryStub:
        skills = SkillLoader([tmp_path / "skills"])

        def skill_catalog(self):
            return self.skills.catalog()

        @staticmethod
        def gated_retrieve(message, hooks=None):
            return "dynamic remembered fact"

    settings = Settings(home=tmp_path / "home")
    settings.ensure_home()
    prompt = Session(settings, memory=MemoryStub()).build_prompt("demo")

    assert "Available skills" in prompt.system
    assert "Right now it is" not in prompt.system
    assert "dynamic remembered fact" not in prompt.system
    assert prompt.dynamic_context == "Relevant memory:\ndynamic remembered fact"
    assert "Your model" not in prompt.dynamic_context


def test_request_order_is_system_tools_runtime_history_current_user(tmp_path):
    registry = ToolRegistry()
    registry.register(Tool(
        "read_demo", "Read demo data", {"type": "object"}, lambda: "ok"
    ))

    class Recorder(ScriptedClient):
        def __init__(self):
            super().__init__([response([text_block("done")])])
            self.call = None

        def _create(self, **kwargs):
            self.call = kwargs
            return super()._create(**kwargs)

    client = Recorder()
    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]

    run_loop(
        client=client,
        model="test",
        system="SOUL\n\nAvailable skills (metadata only):\n`demo`: route",
        runtime_context="Relevant memory:\nfact",
        messages=messages,
        tools=registry,
    )

    assert client.call["system"].startswith("SOUL")
    assert "Right now" not in client.call["system"]
    assert client.call["tools"][0]["name"] == "read_demo"
    sent = client.call["messages"]
    assert sent[0]["content"] == "old question"
    assert sent[1]["content"] == "old answer"
    assert sent[2]["content"] == "current question"
    assert "<otto_runtime_context>" in sent[3]["content"]
    assert "Relevant memory" in sent[3]["content"]
    assert "Your model" not in sent[3]["content"]
    assert "<agent_status" in sent[4]["content"]
    # Temporary runtime context is sent to the provider but never persisted in
    # the mutable conversation trajectory.
    assert "<otto_runtime_context>" not in str(messages)
    assert "<agent_status" not in str(messages)


def test_dynamic_schema_provider_hot_reloads_manifest_changes(tmp_path):
    root = tmp_path / "demo"
    _write_skill(
        tmp_path,
        description="Use when demo work is requested. Do not use for other work.",
    )
    loader = SkillLoader([tmp_path])
    registry = ToolRegistry()
    registry.register(Tool("special_action", "special", {"type": "object"}, lambda: "ok"))
    registry.configure_skill_tools(loader.tool_requirements)
    assert {tool["name"] for tool in registry.schemas(active_skills=set())} == {
        "special_action"
    }

    (root / "otto.json").write_text(
        '{"required_tools": ["special_action"]}\n', encoding="utf-8"
    )

    assert registry.schemas(active_skills=set()) == []
    assert {tool["name"] for tool in registry.schemas(active_skills={"demo"})} == {
        "special_action"
    }
