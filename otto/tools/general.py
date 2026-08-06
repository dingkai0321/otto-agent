"""Small, general execution surface for Skill-driven work.

File operations are strictly workspace-bound. ``run_command`` is an approved
host terminal (not mislabeled as a sandbox). ``python`` fails closed unless an
OS isolation backend is available. ``fetch_url`` blocks local/private targets.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from otto.tools.registry import Tool

MAX_FILE_CHARS = 1_000_000
MAX_TOOL_OUTPUT = 30_000
MAX_FETCH_BYTES = 200_000


class WorkspaceError(ValueError):
    """A requested path escapes or violates the configured workspace."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def resolve(self, raw: str = ".", *, kind: str = "any") -> Path:
        raw = str(raw or ".").strip()
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"cannot resolve path {raw!r}: {exc}") from exc
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"path must stay inside workspace {self.root}")
        if kind == "file" and (not resolved.exists() or not resolved.is_file()):
            raise WorkspaceError(f"file does not exist: {raw}")
        if kind == "dir" and (not resolved.exists() or not resolved.is_dir()):
            raise WorkspaceError(f"directory does not exist: {raw}")
        return resolved

    def display(self, path: Path) -> str:
        return "." if path == self.root else str(path.relative_to(self.root))


def _bounded(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output truncated at {limit} characters]"


def _atomic_write(path: Path, content: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode & 0o777 if path.exists() else None
    with tempfile.NamedTemporaryFile(
        "w", encoding=encoding, dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(content)
    if previous_mode is not None:
        temp_path.chmod(previous_mode)
    os.replace(temp_path, path)


def _run_process(
    command: list[str], *, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> tuple[int | None, str, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except (OSError, ProcessLookupError):
            process.kill()
        stdout, stderr = process.communicate()
        return None, stdout, stderr


def _clean_env(temp_dir: Path) -> dict[str, str]:
    """Do not implicitly expose provider keys or other host secrets to commands."""
    keep = ("PATH", "LANG", "LC_ALL", "TERM")
    env = {name: os.environ[name] for name in keep if os.environ.get(name)}
    env["TMPDIR"] = str(temp_dir)
    env["OTTO_TOOL_EXECUTION"] = "1"
    return env


def _macos_python_command(script: Path, workspace: Path, run_dir: Path) -> list[str] | None:
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        return None

    def literal(path: Path | str) -> str:
        return str(path).replace('"', '\\"')

    readable = {
        workspace,
        run_dir,
        Path(sys.executable).resolve().parent,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path("/System/Library"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/Library/Frameworks"),
        Path("/private/var/db/timezone"),
    }
    # Seatbelt checks every directory component while resolving an allowed
    # subtree. Grant metadata reads to ancestors only; without this Python
    # aborts during path initialisation on a denied read of ``/``. These are
    # literals, not subpaths, so sibling files remain unreadable.
    ancestors: set[Path] = set()
    for allowed in readable:
        parent = allowed.parent
        while parent != parent.parent:
            ancestors.add(parent)
            parent = parent.parent
        ancestors.add(Path("/"))
    clauses = "\n".join(
        f'    (subpath "{literal(path)}")' for path in sorted(readable, key=str)
        if path.exists()
    )
    ancestor_clauses = "\n".join(
        f'    (literal "{literal(path)}")'
        for path in sorted(ancestors, key=str)
        if path.exists()
    )
    profile = f"""(version 1)
(deny default)
(allow process*)
(allow process-info*)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-read*
{clauses}
{ancestor_clauses}
    (literal "/dev/null")
    (literal "/dev/random")
    (literal "/dev/urandom")
    (literal "/private/etc/localtime"))
(allow file-map-executable
{clauses})
(allow file-write* (subpath "{literal(run_dir)}"))
(allow file-write-data (literal "/dev/null"))
"""
    profile_path = run_dir / "sandbox.sb"
    profile_path.write_text(profile, encoding="utf-8")
    return [sandbox, "-f", str(profile_path), sys.executable, "-I", "-S", str(script)]


def _linux_python_command(script: Path, workspace: Path, run_dir: Path) -> list[str] | None:
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        return None
    command = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-net",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += [
        "--ro-bind", str(workspace), str(workspace),
        "--bind", str(run_dir), str(run_dir),
        "--chdir", str(run_dir),
        sys.executable, "-I", "-S", str(script),
    ]
    return command


def _python_command(script: Path, workspace: Path, run_dir: Path) -> tuple[str, list[str]] | None:
    if sys.platform == "darwin":
        command = _macos_python_command(script, workspace, run_dir)
        return ("macos-sandbox-exec", command) if command else None
    if sys.platform.startswith("linux"):
        command = _linux_python_command(script, workspace, run_dir)
        return ("linux-bubblewrap", command) if command else None
    return None


def _public_http_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http:// or https:// and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are not allowed")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError(f"hostname could not be resolved: {exc}") from exc
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified
        ):
            raise ValueError(f"local or non-public network target is blocked: {raw}")
    return parsed.geturl()


class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe = _public_http_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, safe)


def make_tools(settings, *, url_opener=None) -> list[Tool]:
    workspace = Workspace(Path(settings.workspace))

    def read_file(path: str, offset: int = 0, max_chars: int = 50_000,
                  encoding: str = "utf-8") -> str:
        target = workspace.resolve(path, kind="file")
        if target.is_symlink():
            raise WorkspaceError("symlink files are not readable through this tool")
        limit = max(1, min(int(max_chars or 50_000), 200_000))
        start = max(0, int(offset or 0))
        try:
            text = target.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            return f"Error: {workspace.display(target)} is not valid {encoding} text: {exc}."
        chunk = text[start:start + limit]
        suffix = (
            f"\n[next offset: {start + len(chunk)}]"
            if start + len(chunk) < len(text) else ""
        )
        return (
            f"File: {workspace.display(target)} (chars {start}:{start + len(chunk)} of "
            f"{len(text)})\n{chunk}{suffix}"
        )

    def write_file(path: str, content: str, overwrite: bool = False,
                   encoding: str = "utf-8") -> str:
        target = workspace.resolve(path)
        if target.exists() and target.is_dir():
            return "Error: destination is a directory."
        if target.exists() and not overwrite:
            return "Error: file already exists; set overwrite=true or use apply_patch."
        if len(content) > MAX_FILE_CHARS:
            return f"Error: content exceeds {MAX_FILE_CHARS} characters."
        _atomic_write(target, content, encoding)
        return f"Wrote {len(content)} characters to {workspace.display(target)}."

    def apply_patch(path: str, old_text: str, new_text: str,
                    replace_all: bool = False, encoding: str = "utf-8") -> str:
        target = workspace.resolve(path, kind="file")
        if not old_text:
            return "Error: old_text must not be empty."
        text = target.read_text(encoding=encoding)
        matches = text.count(old_text)
        if matches == 0:
            return "Error: old_text was not found; read the current file before patching."
        if matches > 1 and not replace_all:
            return f"Error: old_text matches {matches} locations; provide a unique block."
        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        if len(updated) > MAX_FILE_CHARS:
            return f"Error: patched content exceeds {MAX_FILE_CHARS} characters."
        _atomic_write(target, updated, encoding)
        changed = matches if replace_all else 1
        return f"Patched {workspace.display(target)} ({changed} replacement(s))."

    def list_files(path: str = ".", pattern: str = "*", recursive: bool = False,
                   include_hidden: bool = False, max_results: int = 200) -> str:
        root = workspace.resolve(path, kind="dir")
        limit = max(1, min(int(max_results or 200), 1_000))
        found = []
        iterator = root.rglob("*") if recursive else root.iterdir()
        for item in iterator:
            relative = item.relative_to(workspace.root)
            if not include_hidden and any(part.startswith(".") for part in relative.parts):
                continue
            if not fnmatch.fnmatch(item.name, pattern or "*"):
                continue
            kind = "symlink" if item.is_symlink() else ("dir" if item.is_dir() else "file")
            size = item.stat().st_size if kind == "file" else 0
            found.append(f"{kind}\t{size}\t{relative}")
            if len(found) >= limit:
                break
        return _bounded(
            f"Workspace: {workspace.root}\n" + ("\n".join(found) or "(no matches)")
        )

    def search_files(query: str, path: str = ".", glob: str = "*", regex: bool = False,
                     case_sensitive: bool = False, max_results: int = 200) -> str:
        root = workspace.resolve(path, kind="dir")
        if not query or len(query) > 1_000:
            return "Error: query must contain 1-1000 characters."
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            return f"Error: invalid regular expression: {exc}."
        limit = max(1, min(int(max_results or 200), 1_000))
        matches = []
        for file in root.rglob("*"):
            if len(matches) >= limit:
                break
            if file.is_symlink() or not file.is_file() or not fnmatch.fnmatch(file.name, glob or "*"):
                continue
            relative = file.relative_to(workspace.root)
            if any(part in {".git", ".otto", "node_modules", "__pycache__"} for part in relative.parts):
                continue
            try:
                if file.stat().st_size > 2_000_000:
                    continue
                with file.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if pattern.search(line):
                            matches.append(
                                f"{relative}:{line_number}: {_bounded(line.rstrip(), 500)}"
                            )
                            if len(matches) >= limit:
                                break
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(matches) or "No matches found."

    def run_command(command: str, cwd: str = ".", timeout: int = 30) -> str:
        if not command.strip() or len(command) > 20_000:
            return "Error: command must contain 1-20000 characters."
        workdir = workspace.resolve(cwd, kind="dir")
        deadline = max(1, min(int(timeout or 30), 120))
        temp_dir = Path(settings.home) / "command-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        shell = "/bin/sh" if Path("/bin/sh").exists() else (shutil.which("sh") or "sh")
        code, stdout, stderr = _run_process(
            [shell, "-c", command],
            cwd=workdir,
            timeout=deadline,
            env=_clean_env(temp_dir),
        )
        if code is None:
            return _bounded(
                f"Command timed out after {deadline}s in {workspace.display(workdir)}.\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        return _bounded(
            f"Command exited {code} in {workspace.display(workdir)}.\n"
            f"stdout:\n{stdout or '(empty)'}\nstderr:\n{stderr or '(empty)'}"
        )

    def python(code: str, timeout: int = 30) -> str:
        if not code.strip() or len(code) > 100_000:
            return "Error: code must contain 1-100000 characters."
        run_dir = Path(settings.home).expanduser().resolve() / "python-runs" / uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=False)
        script = run_dir / "main.py"
        script.write_text(code, encoding="utf-8")
        backend = _python_command(script, workspace.root, run_dir)
        if backend is None:
            return (
                "Error: no supported Python sandbox is installed. Otto requires "
                "sandbox-exec on macOS or bubblewrap (bwrap) on Linux and fails closed."
            )
        backend_name, command = backend
        deadline = max(1, min(int(timeout or 30), 120))
        process_code, stdout, stderr = _run_process(
            command,
            cwd=run_dir,
            timeout=deadline,
            env=_clean_env(run_dir),
        )
        artifacts = [
            str(path.relative_to(run_dir)) for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name not in {"main.py", "sandbox.sb"}
        ]
        status = (
            f"timed out after {deadline}s" if process_code is None else f"exited {process_code}"
        )
        return _bounded(
            f"Sandboxed Python ({backend_name}) {status}.\n"
            f"Run directory: {run_dir}\n"
            f"Artifacts: {', '.join(artifacts) if artifacts else '(none)'}\n"
            f"stdout:\n{stdout or '(empty)'}\nstderr:\n{stderr or '(empty)'}"
        )

    def fetch_url(url: str, max_bytes: int = 100_000, timeout: int = 20) -> str:
        safe_url = _public_http_url(url)
        byte_limit = max(1_000, min(int(max_bytes or 100_000), MAX_FETCH_BYTES))
        deadline = max(1, min(int(timeout or 20), 60))
        opener = url_opener or build_opener(_SafeRedirects())
        request = Request(
            safe_url,
            headers={"User-Agent": "OttoAgent/0.1 (+local read-only fetch)"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=deadline) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(byte_limit + 1)
                final_url = _public_http_url(response.geturl())
        except (HTTPError, URLError, OSError) as exc:
            return f"Error: fetch failed: {exc}."
        truncated = len(data) > byte_limit
        data = data[:byte_limit]
        charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
        charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
        text = data.decode(charset, errors="replace")
        return _bounded(
            f"URL: {final_url}\nContent-Type: {content_type or '(unknown)'}\n"
            f"Bytes: {len(data)}{' (truncated)' if truncated else ''}\n{text}"
        )

    return [
        Tool(
            "read_file",
            "Read a UTF-8 text file inside the workspace with offset pagination. "
            "Use for exact source/config/document contents; do not use for directories.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
                    "encoding": {"type": "string"},
                },
                "required": ["path"],
            },
            read_file,
        ),
        Tool(
            "write_file",
            "Create or deliberately overwrite one text file inside the workspace. "
            "Use apply_patch for small edits to an existing file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                    "encoding": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            write_file,
            permission="ask",
            permission_reason="writing a workspace file",
        ),
        Tool(
            "apply_patch",
            "Atomically replace one exact text block in an existing workspace file. "
            "Read the file first; old_text must be unique unless replace_all=true.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                    "encoding": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            apply_patch,
            permission="ask",
            permission_reason="modifying a workspace file",
        ),
        Tool(
            "list_files",
            "List files and directories inside the workspace with optional recursion and glob. "
            "Use before reading when the exact path is unknown.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "include_hidden": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            },
            list_files,
        ),
        Tool(
            "search_files",
            "Search text files inside the workspace by literal text or regular expression. "
            "Returns file, line number, and matching line; skips binary/vendor state.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["query"],
            },
            search_files,
        ),
        Tool(
            "run_command",
            "Run a shell command on the HOST inside a workspace directory. This is not a "
            "sandbox: use only when file tools or sandboxed Python are insufficient. "
            "Environment secrets are removed; approval, timeout, and hard-deny rules apply.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["command"],
            },
            run_command,
            permission="ask",
            permission_reason="running an arbitrary host shell command",
        ),
        Tool(
            "python",
            "Execute standard-library Python in an OS sandbox with no network, read-only "
            "workspace access, and writes limited to a separate run directory. Fails closed "
            "when sandbox-exec/bubblewrap is unavailable.",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["code"],
            },
            python,
            permission="ask",
            permission_reason="executing model-generated Python code in an OS sandbox",
        ),
        Tool(
            "fetch_url",
            "Fetch one public HTTP(S) URL as bounded text. Blocks credentials, localhost, "
            "private/link-local addresses, and unsafe redirects. Use search_web to discover URLs.",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
                },
                "required": ["url"],
            },
            fetch_url,
        ),
    ]
