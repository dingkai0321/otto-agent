"""The agent's tools. Flagship-task tools (calendar/notes/messages), memory
self-management (manage_memory/update_soul/create_skill), and opt-in adapters:
Apple ecosystem (OTTO_APPLE_TOOLS=1) and MCP servers (.otto/mcp.json)."""

from __future__ import annotations

from otto.config import Settings
from otto.permissions import PermissionPolicy
from otto.tools import calendar, general, knowledge, memory_admin, messages, notes, search, tasks
from otto.tools.registry import ToolRegistry


def build_registry(conn, settings: Settings, memory=None, task_store=None,
                   task_scope=None, client=None) -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy(settings.workspace))
    # Small general execution surface used by many Skills. File tools are
    # workspace-bound; host shell/Python execution retains explicit approval.
    for tool in general.make_tools(settings):
        registry.register(tool)
    registry.register(
        calendar.make_tool(
            conn,
            settings.home,
            apple_calendar=settings.apple_calendar,
            google_calendar=settings.google_calendar,
            google_calendar_id=settings.google_calendar_id,
        )
    )
    # Read side: "what's on my calendar?" — one tool across every connected
    # source (Google when signed in, plus otto's own), so the model never has
    # to guess which calendar the user meant.
    registry.register(calendar.make_list_tool(conn, settings.home))
    registry.register(notes.make_tool(conn))
    registry.register(messages.make_tool(settings.home))
    # Web search — pairs with create_event for the multi-tool loop demo
    # ("find the World Cup games left and add them to my calendar").
    registry.register(search.make_tool())

    # Durable planning is always available. A flat list covers Todo-style
    # progress; dependencies and owners turn the same records into a task DAG.
    if task_store is not None and task_scope is not None:
        for tool in tasks.make_tools(task_store, task_scope):
            registry.register(tool)

    # Memory self-management — the agent can correct/forget memory, learn rules,
    # and author its own skills (feels like a personal agent, not a black box).
    if memory is not None:
        registry.register(memory_admin.make_load_skill_tool(memory))
        registry.register(memory_admin.make_load_skill_resource_tool(memory))
        registry.register(memory_admin.make_run_skill_script_tool(memory, settings))
        registry.register(memory_admin.make_copy_skill_asset_tool(memory, settings))
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))
        if memory.knowledge.has_documents():
            registry.register(knowledge.make_tool(memory.knowledge))

    # Experimental tools — off by default; opt in with OTTO_EXPERIMENTAL=1.
    # delegate_task (sub-agents via pi) is live; browser/cron remain skeletons.
    # The approved host terminal is a core general tool above.
    #
    # Trust settings.experimental ALONE. load_settings() already defaults it from
    # OTTO_EXPERIMENTAL, so re-checking the env here would let the global switch
    # override an explicit False — and the arena passes experimental=False for
    # every non-coding race. Once the dashboard could write OTTO_EXPERIMENTAL=1,
    # that OR silently forced delegate_task into races that never asked for it.
    if getattr(settings, "experimental", False):
        from otto.tools import experimental

        for t in experimental.make_tools(settings):
            registry.register(t)

    # Apple ecosystem readers/writers (opt-in; first use triggers macOS prompts).
    if settings.apple_tools:
        from otto.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # Read-only GitHub via the gh CLI (opt-in; uses gh's own auth, no token here).
    if getattr(settings, "gh_tool", False):
        from otto.tools import github

        registry.register(github.make_tool(default_repo=getattr(settings, "gh_repo", "")))

    # MCP servers (opt-in via .otto/mcp.json).
    mcp_config = settings.home / "mcp.json"
    if mcp_config.exists():
        try:
            from otto.tools.mcp_client import MCPBridge

            bridge = MCPBridge(mcp_config)
            for t in bridge.start():
                registry.register(t)
            registry.mcp_bridge = bridge  # so Otto.close() can stop the servers
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'otto-agent[mcp]'")

    # Native subagents are a core harness capability. Register last so their
    # role allowlists can select from the complete parent registry. The child
    # never receives agent_spawn/delegate_task, preventing recursive spawning.
    if client is not None and task_store is not None and task_scope is not None:
        from otto.tools import subagents

        registry.register(
            subagents.make_tool(settings, client, registry, task_store, task_scope)
        )

    if memory is not None:
        registry.configure_skill_tools(memory.skills.tool_requirements)
        registry.gate_until_skill_active(
            "load_skill_resource", "run_skill_script", "copy_skill_asset"
        )

    return registry
