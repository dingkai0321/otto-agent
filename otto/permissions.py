"""Tool permissions — the gate immediately before side effects happen.

The model can *request* any registered tool, but this module — ordinary Python,
outside the model — makes the final decision.  The order is deliberately fixed:

    hard deny -> context rules -> user approval -> execute

Most calls are harmless and pass straight through.  A dangerous shell command
is never executed, while a context-dependent operation (deleting a file,
touching a path outside the workspace, delegating unrestricted coding work)
must be approved by an interactive gateway.  Gateways without an approval UI
fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

PermissionBehavior = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    reason: str


# These are intentionally hard stops, not approval prompts.  The expressions
# cover the common spelling variants of the teaching example without pretending
# to be a complete shell sandbox (real shell isolation still belongs in an OS
# sandbox/container).
_HARD_DENY = (
    (re.compile(r"(?:^|[;&|]\s*)sudo(?:\s|$)", re.IGNORECASE), "privilege escalation with sudo"),
    (re.compile(r"(?:^|[;&|]\s*)(?:shutdown|reboot)(?:\s|$)", re.IGNORECASE), "system shutdown or reboot"),
    (re.compile(r"(?:^|[;&|]\s*)mkfs(?:\.|\s|$)", re.IGNORECASE), "filesystem formatting"),
    (re.compile(r"(?:^|[;&|]\s*)dd\s+[^\n]*\bif\s*=", re.IGNORECASE), "raw disk copy with dd"),
    (
        re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*f|-+[a-z]*f[a-z]*r)\s+(?:--\s+)?/(?:\s|$)", re.IGNORECASE),
        "recursive deletion of the filesystem root",
    ),
    (re.compile(r">\s*/dev/(?:sd[a-z]|disk\d+)(?:\s|$)", re.IGNORECASE), "direct overwrite of a disk device"),
)

_SHELL_NAMES = {
    "bash", "shell", "run_shell", "run_command", "execute_command", "terminal",
}
_FILE_NAMES = {
    "read_file", "write_file", "edit_file", "apply_patch", "list_files", "search_files",
}
_DESTRUCTIVE_SHELL = (
    re.compile(r"(?:^|[;&|]\s*)rm(?:\s|$)", re.IGNORECASE),
    re.compile(r">\s*/etc/", re.IGNORECASE),
    re.compile(r"\bchmod\s+(?:-R\s+)?777(?:\s|$)", re.IGNORECASE),
)


def _base_name(tool_name: str) -> str:
    """Recognise both built-ins and namespaced tools such as ``fs_read_file``."""
    lowered = tool_name.lower()
    for known in _SHELL_NAMES | _FILE_NAMES:
        if lowered == known or lowered.endswith(f"_{known}"):
            return known
    return lowered


def _command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _outside_workspace(raw_path: Any, workspace: Path) -> bool:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        return not candidate.resolve().is_relative_to(workspace)
    except (OSError, RuntimeError):
        # If a path cannot be resolved safely, it is not silently trusted.
        return True


class PermissionPolicy:
    """Pure, deterministic policy evaluation; prompting belongs to gateways."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.expanduser().resolve()

    def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        declared: PermissionBehavior | Literal["passthrough"] = "passthrough",
        declared_reason: str = "",
    ) -> PermissionDecision:
        base = _base_name(tool_name)
        command = _command(args) if base in _SHELL_NAMES else ""

        # Gate 1 — a hard deny always wins, including over an explicit tool
        # declaration.  No caller can turn these into an approval prompt.
        if command:
            for pattern, reason in _HARD_DENY:
                if pattern.search(command):
                    return PermissionDecision("deny", reason)
        if declared == "deny":
            return PermissionDecision("deny", declared_reason or "tool is disabled by policy")

        # Gate 2 — rules that require a human decision.
        if base in _FILE_NAMES and _outside_workspace(args.get("path"), self.workspace):
            return PermissionDecision("ask", f"access outside workspace: {args.get('path')}")
        if command and any(pattern.search(command) for pattern in _DESTRUCTIVE_SHELL):
            return PermissionDecision("ask", "potentially destructive shell command")
        if tool_name in {"delegate_task", "agent_spawn"}:
            return PermissionDecision(
                "ask",
                (
                    "delegated coding can execute shell commands and modify files"
                    if tool_name == "delegate_task"
                    else "a child agent can make additional model and tool calls"
                ),
            )
        if tool_name == "manage_memory" and str(args.get("action", "")).lower() == "delete":
            return PermissionDecision("ask", "deleting long-term memory")
        if declared == "ask":
            return PermissionDecision("ask", declared_reason or "tool requires user approval")

        # Gate 3 is performed by ToolRegistry with a gateway-provided callback.
        return PermissionDecision("allow", declared_reason or "no permission rule matched")
