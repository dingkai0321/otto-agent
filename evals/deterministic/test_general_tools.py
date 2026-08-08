"""DETERMINISTIC EVAL — workspace tools and execution boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from otto.permissions import PermissionPolicy
from otto.tools import general
from otto.tools.registry import ToolRegistry


def _registry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(workspace=workspace, home=tmp_path / "home")
    settings.home.mkdir()
    registry = ToolRegistry(PermissionPolicy(workspace))
    for tool in general.make_tools(settings):
        registry.register(tool)
    return registry, workspace


def test_general_tool_catalog_is_small_and_complete(tmp_path):
    registry, _workspace = _registry(tmp_path)

    assert set(registry._tools) == {
        "read_file", "write_file", "apply_patch", "list_files", "search_files",
        "run_command", "python", "fetch_url",
    }
    assert registry._tools["run_command"].permission == "ask"
    assert "not a sandbox" in registry._tools["run_command"].description.lower()


def test_workspace_file_lifecycle_search_and_pagination(tmp_path):
    registry, workspace = _registry(tmp_path)
    approve = lambda *_: True

    written = registry.execute(
        "write_file",
        {"path": "src/demo.txt", "content": "alpha\nbeta\nalpha\n"},
        approver=approve,
    )
    duplicate = registry.execute(
        "write_file",
        {"path": "src/demo.txt", "content": "no"},
        approver=approve,
    )
    ambiguous = registry.execute(
        "apply_patch",
        {"path": "src/demo.txt", "old_text": "alpha", "new_text": "gamma"},
        approver=approve,
    )
    patched = registry.execute(
        "apply_patch",
        {
            "path": "src/demo.txt", "old_text": "beta", "new_text": "delta",
        },
        approver=approve,
    )

    assert written.startswith("Wrote")
    assert "already exists" in duplicate
    assert "matches 2 locations" in ambiguous
    assert patched.startswith("Patched")
    assert (workspace / "src" / "demo.txt").read_text() == "alpha\ndelta\nalpha\n"
    assert "next offset: 5" in registry.execute(
        "read_file", {"path": "src/demo.txt", "max_chars": 5}
    )
    assert "src/demo.txt" in registry.execute(
        "list_files", {"recursive": True, "pattern": "*.txt"}
    )
    assert "src/demo.txt:2: delta" in registry.execute(
        "search_files", {"query": "delta", "glob": "*.txt"}
    )


def test_workspace_escape_and_symlink_escape_fail_closed(tmp_path):
    registry, workspace = _registry(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (workspace / "link.txt").symlink_to(outside)

    direct = registry.execute("read_file", {"path": str(outside)})
    linked = registry.execute(
        "read_file", {"path": "link.txt"}, approver=lambda *_: True
    )

    assert "permission denied" in direct
    assert "path must stay inside workspace" in linked
    assert "secret" not in linked


def test_host_terminal_requires_approval_scrubs_secrets_and_hard_denies(tmp_path, monkeypatch):
    registry, _workspace = _registry(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")

    denied = registry.execute("run_command", {"command": "printf nope"})
    allowed = registry.execute(
        "run_command",
        {"command": "printf ok; test -z \"$ANTHROPIC_API_KEY\""},
        approver=lambda *_: True,
    )
    hard = registry.execute(
        "run_command", {"command": "sudo shutdown now"}, approver=lambda *_: True
    )

    assert "no interactive approver" in denied
    assert "Command exited 0" in allowed and "ok" in allowed
    assert "permission denied" in hard and "sudo" in hard


@pytest.mark.skipif(not general.shutil.which("sandbox-exec"), reason="macOS sandbox unavailable")
def test_python_uses_os_sandbox_and_writes_only_run_directory(tmp_path):
    registry, workspace = _registry(tmp_path)
    forbidden = workspace / "forbidden.txt"
    code = (
        "from pathlib import Path\n"
        "print(2 + 2)\n"
        "Path('artifact.txt').write_text('ok')\n"
        f"\ntry:\n    Path({str(forbidden)!r}).write_text('bad')\n"
        "except Exception as exc:\n    print(type(exc).__name__)\n"
    )

    output = registry.execute("python", {"code": code}, approver=lambda *_: True)

    if "sandbox_apply: Operation not permitted" in output:
        pytest.skip("outer test sandbox does not permit nested sandbox-exec")
    assert "Sandboxed Python (macos-sandbox-exec) exited 0" in output
    assert "artifact.txt" in output and "4" in output
    assert "PermissionError" in output
    assert not forbidden.exists()


def test_fetch_url_blocks_private_network_and_bounds_public_text(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="non-public"):
        general._public_http_url("http://127.0.0.1/private")

    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "text/plain; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit):
            return b"public body"

        @staticmethod
        def geturl():
            return "https://example.com/page"

    class Opener:
        @staticmethod
        def open(_request, timeout):
            assert timeout == 20
            return Response()

    monkeypatch.setattr(
        general.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(workspace=workspace, home=tmp_path / "home")
    tools = {tool.name: tool for tool in general.make_tools(settings, url_opener=Opener())}

    assert "public body" in tools["fetch_url"].fn(url="https://example.com/page")
