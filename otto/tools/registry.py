"""Tool registry — the 'Agentic Tools' box on the whiteboard.

A tool is three things: a name+description the model reads, a JSON schema for
its arguments, and a Python function that runs. That's it. (Registry pattern
adapted from launch-agentic-rag's app/agents/tools/registry.py.)
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from otto.hooks import HookEvent, HookManager, build_hooks, permission_hook
from otto.permissions import PermissionBehavior, PermissionPolicy

_SENSITIVE_ARG_PARTS = ("token", "secret", "password", "api_key", "authorization")


def _diagnostic_args(value: Any, key: str = "") -> Any:
    """Preserve error-shaping arguments while redacting likely credentials."""
    if any(part in key.lower() for part in _SENSITIVE_ARG_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _diagnostic_args(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_diagnostic_args(item, key) for item in value]
    return value


def _repair_hint(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "Verify the path, inspect the current workspace, and use an existing path."
    if isinstance(exc, PermissionError):
        return "Check the workspace boundary and request permission for the exact target."
    if isinstance(exc, TimeoutError):
        return "Inspect partial progress, reduce the operation, or use a bounded alternative."
    if isinstance(exc, (TypeError, ValueError)):
        return "Recheck the tool schema and argument types before retrying."
    return "Inspect the exception and arguments, then change strategy before retrying."


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]  # tools return a string the model observes
    # A long-running tool can opt in to the run-scoped HookManager while it
    # works. The private `_hooks` keyword never appears in the model schema.
    wants_hooks: bool = False
    # Most tools pass through to the shared policy. Adapters that know they can
    # cause external side effects (for example an MCP destructiveHint) may ask
    # explicitly; the policy's hard-deny rules still take precedence.
    permission: PermissionBehavior | Literal["passthrough"] = "passthrough"
    permission_reason: str = ""

    def to_api(self) -> dict[str, Any]:
        """The shape the Messages API expects in its `tools=` parameter."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self, permission_policy: PermissionPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.permission_policy = permission_policy
        # Skill manifests may hide specialist schemas until their skill is
        # activated. Implementations stay registered, so activation changes
        # prompt surface rather than rebuilding objects or weakening policy.
        self._skill_tools: dict[str, set[str]] = {}
        self._gated_tools: set[str] = set()
        self._active_skill_tools: set[str] = set()
        self._skill_requirements_provider: Callable[[], dict[str, set[str]]] | None = None

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def configure_skill_tools(
        self,
        requirements: dict[str, set[str]] | Callable[[], dict[str, set[str]]],
    ) -> None:
        if callable(requirements):
            self._skill_requirements_provider = requirements
            requirements = requirements()
        self._skill_tools = {name: set(tools) for name, tools in requirements.items()}
        self._gated_tools = set().union(*self._skill_tools.values()) if self._skill_tools else set()

    def _refresh_skill_tools(self) -> None:
        if self._skill_requirements_provider is None:
            return
        requirements = self._skill_requirements_provider()
        self._skill_tools = {name: set(tools) for name, tools in requirements.items()}
        self._gated_tools = set().union(*self._skill_tools.values()) if self._skill_tools else set()

    def gate_until_skill_active(self, *tool_names: str) -> None:
        """Hide generic Skill resource actions until at least one Skill is active."""
        self._active_skill_tools.update(tool_names)

    def schemas(self, active_skills: set[str] | None = None) -> list[dict[str, Any]]:
        """Return the current model surface; None means full introspection catalog."""
        self._refresh_skill_tools()
        if active_skills is None:
            return [tool.to_api() for tool in self._tools.values()]
        enabled = set().union(
            *(self._skill_tools.get(name, set()) for name in active_skills)
        ) if active_skills else set()
        return [
            tool.to_api() for name, tool in self._tools.items()
            if (
                name not in self._gated_tools or name in enabled
            ) and (
                name not in self._active_skill_tools or bool(active_skills)
            )
        ]

    def subset(self, names: set[str]) -> ToolRegistry:
        """A restricted registry sharing implementations and the safety policy."""
        child = ToolRegistry(self.permission_policy)
        for name in sorted(names):
            tool = self._tools.get(name)
            if tool is not None:
                child.register(tool)
        child._skill_tools = {
            skill: {name for name in tool_names if name in child._tools}
            for skill, tool_names in self._skill_tools.items()
        }
        child._gated_tools = {name for name in self._gated_tools if name in child._tools}
        child._active_skill_tools = {
            name for name in self._active_skill_tools if name in child._tools
        }
        child._skill_requirements_provider = self._skill_requirements_provider
        return child

    def execute(self, name: str, args: dict[str, Any], notify=None, approver=None,
                hooks: HookManager | None = None) -> str:
        """Run one tool call safely: the model observes errors as text instead
        of crashing the loop (execute_tool_safely pattern).

        PreToolUse and PostToolUse are the only lifecycle boundaries here.
        ``notify`` remains as a compatibility adapter for direct registry users;
        the agent passes its run-scoped HookManager instead.
        """
        manager = hooks or build_hooks(self.permission_policy, notify)
        tool = self._tools.get(name)
        if tool is None:
            output = f"Error: unknown tool '{name}'"
            manager.trigger(HookEvent.TOOL_FAILURE, tool=name, args=args, output=output)
            return output
        if self.permission_policy is not None and not manager.has_hook(
            HookEvent.PRE_TOOL_USE, "permission_policy"
        ):
            manager.register(
                HookEvent.PRE_TOOL_USE,
                permission_hook(self.permission_policy),
                name="permission_policy",
                priority=-10_000,
                critical=True,
                terminal=True,
            )
        supplied_args = args
        before = manager.trigger(
            HookEvent.PRE_TOOL_USE,
            tool=name,
            args=dict(args),
            declared_permission=tool.permission,
            declared_reason=tool.permission_reason,
        )
        args = before.data.get("args", args)
        if not isinstance(args, dict):
            output = "Error: blocked by hook — tool args must remain an object."
            manager.trigger(HookEvent.TOOL_FAILURE, tool=name, args={}, output=output)
            return output
        if args is not supplied_args:
            supplied_args.clear()
            supplied_args.update(args)
            args = supplied_args
        if before.block_reason:
            output = f"Error: blocked by hook — {before.block_reason}."
            manager.trigger(HookEvent.TOOL_FAILURE, tool=name, args=args, output=output)
            return output
        if before.permission != "passthrough":
            reason = before.permission_reason or "permission policy"
            event = {"tool": name, "args": args, "decision": before.permission,
                     "reason": reason}
            manager.trigger(HookEvent.PERMISSION_DECISION, **event)
            if before.permission == "deny":
                output = f"Error: permission denied — {reason}."
                manager.trigger(HookEvent.TOOL_FAILURE, tool=name, args=args, output=output)
                return output
            if before.permission == "ask":
                requested = manager.trigger(
                    HookEvent.PERMISSION_REQUEST, tool=name, args=args, reason=reason
                )
                hook_decided = requested.permission in ("allow", "deny")
                if hook_decided:
                    allowed = requested.permission == "allow"
                else:
                    allowed = bool(approver and approver(name, args, reason))
                manager.trigger(
                    HookEvent.PERMISSION_DECISION,
                    **{**event, "decision": "allow" if allowed else "deny", "requested": True},
                )
                if not allowed:
                    suffix = (
                        "" if (approver or hook_decided)
                        else " (this gateway has no interactive approver)"
                    )
                    output = f"Error: permission denied — {reason}{suffix}."
                    manager.trigger(HookEvent.TOOL_FAILURE, tool=name, args=args, output=output)
                    return output
        try:
            if tool.wants_hooks:
                output = tool.fn(**args, _hooks=manager)
            else:
                output = tool.fn(**args)
        except Exception as exc:  # surface, don't crash — the model can retry
            args_json = json.dumps(
                _diagnostic_args(args), ensure_ascii=False, indent=2, default=str
            )[:4_000]
            stack = traceback.format_exc(limit=8)[-6_000:]
            output = (
                f"Error running {name}:\n"
                f"Type: {type(exc).__name__}\n"
                f"Message: {exc}\n"
                f"Arguments (credential-like values redacted):\n{args_json}\n"
                f"Traceback (bounded):\n{stack}\n"
                f"Suggested recovery: {_repair_hint(exc)}"
            )
            after = manager.trigger(
                HookEvent.TOOL_FAILURE, tool=name, args=args, output=output, error=repr(exc)
            )
            return str(after.data.get("output", output))
        after = manager.trigger(HookEvent.POST_TOOL_USE, tool=name, args=args, output=output)
        return str(after.data.get("output", output))
