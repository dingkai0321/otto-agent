"""Agent Skills registry with three-level progressive disclosure.

Level 1: every SKILL.md contributes only name + description to the catalog.
Level 2: load_skill returns the selected SKILL.md and a resource index.
Level 3: referenced text, scripts, templates and assets are accessed on demand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_ROUTE_POSITIVE = ("use when", "use for", "when ", "用于", "当用户", "适用于", "触发")
_ROUTE_NEGATIVE = ("don't use", "do not use", "not for", "不用于", "不要用于", "不适用")
_RESOURCE_KINDS = {"references", "scripts", "assets", "templates"}


@dataclass(frozen=True)
class SkillResource:
    path: str
    kind: str
    size: int


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    content: str
    resources: tuple[SkillResource, ...] = ()
    required_tools: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.path.parent.resolve()

    def resolve_path(self, relative_path: str, *, allow_directory: bool = False) -> Path:
        """Resolve one package-relative path without allowing path escape."""
        raw = (relative_path or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            raise ValueError("resource path must be relative and stay inside the skill")
        target = (self.root / raw).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("resource path escapes the skill directory")
        if not target.exists() or (not allow_directory and not target.is_file()):
            raise FileNotFoundError(raw)
        return target

    def resolve_resource(self, relative_path: str) -> Path:
        return self.resolve_path(relative_path)


def _frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
    if not match:
        return None
    raw, body = match.groups()
    fields: dict[str, str] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            index += 1
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value in {">", "|", ">-", "|-"}:
            folded: list[str] = []
            index += 1
            while index < len(lines):
                continuation = lines[index]
                if continuation and not continuation[0].isspace():
                    break
                folded.append(continuation.strip())
                index += 1
            fields[key] = ("\n" if value.startswith("|") else " ").join(folded).strip()
            continue
        fields[key] = value.strip("'\"")
        index += 1
    return fields, body


def routing_warnings(name: str, description: str, body: str = "") -> tuple[str, ...]:
    """Non-fatal authoring feedback; old/community skills remain loadable."""
    warnings = []
    lowered = description.lower()
    if not _NAME.fullmatch(name):
        warnings.append("name should be lowercase letters, digits, and hyphens (max 64)")
    if len(description) < 24:
        warnings.append("description is too short to route reliably")
    if len(description) > 600:
        warnings.append("description should stay below 600 characters")
    if not any(marker in lowered for marker in _ROUTE_POSITIVE):
        warnings.append("description should say when to use the skill")
    if not any(marker in lowered for marker in _ROUTE_NEGATIVE):
        warnings.append("description should include a clear negative trigger")
    if body.count("\n") + 1 > 500:
        warnings.append("SKILL.md body exceeds 500 lines; move details to references/")
    return tuple(warnings)


def _resource_index(root: Path) -> tuple[SkillResource, ...]:
    resources = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"SKILL.md", "otto.json"}:
            continue
        relative = path.relative_to(root)
        if relative.parts[0].startswith(".") or relative.parts[0] == "agents":
            continue
        kind = relative.parts[0] if relative.parts[0] in _RESOURCE_KINDS else "reference"
        resources.append(SkillResource(relative.as_posix(), kind, path.stat().st_size))
        if len(resources) >= 200:
            break
    return tuple(resources)


def _required_tools(root: Path) -> tuple[str, ...]:
    manifest = root / "otto.json"
    if not manifest.exists():
        return ()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        tools = data.get("required_tools", [])
        if not isinstance(tools, list):
            return ()
        return tuple(dict.fromkeys(str(name).strip() for name in tools if str(name).strip()))
    except (OSError, json.JSONDecodeError):
        return ()


def _parse_text(text: str, path: Path) -> Skill | None:
    """Validate SKILL.md content (used by loader and create_skill)."""
    parsed = _frontmatter(text)
    if parsed is None:
        return None
    fields, body = parsed
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name or not description:
        return None
    root = path.parent.resolve()
    body = body.strip()
    return Skill(
        name=name,
        description=description,
        body=body,
        path=path,
        content=text,
        resources=_resource_index(root) if root.is_dir() else (),
        required_tools=_required_tools(root) if root.is_dir() else (),
        warnings=routing_warnings(name, description, body),
    )


def _parse(path: Path) -> Skill | None:
    return _parse_text(path.read_text(encoding="utf-8"), path)


class SkillLoader:
    """Scan bundled and personal packages, with personal-name precedence."""

    def __init__(self, dirs: list[Path]):
        self.dirs = dirs
        self.skills: list[Skill] = []
        self._by_name: dict[str, Skill] = {}
        self._sig: tuple = ()
        self.refresh()

    def _scan_sig(self) -> tuple:
        sig = []
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for skill_file in sorted(directory.rglob("SKILL.md")):
                root = skill_file.parent
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        stat = path.stat()
                        sig.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(sig)

    def refresh(self) -> None:
        by_name: dict[str, Skill] = {}
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for skill_file in sorted(directory.rglob("SKILL.md")):
                skill = _parse(skill_file)
                if skill:
                    by_name[skill.name] = skill
        self._by_name = by_name
        self.skills = list(by_name.values())
        self._sig = self._scan_sig()

    def _refresh_if_changed(self) -> None:
        if self._scan_sig() != self._sig:
            self.refresh()

    def catalog(self) -> str:
        """Cheap always-present layer: metadata only, never bodies/resources."""
        self._refresh_if_changed()
        return "\n".join(
            f"- `{skill.name}`: {skill.description}" for skill in self.skills
        )

    def load(self, name: str) -> Skill | None:
        self._refresh_if_changed()
        return self._by_name.get((name or "").strip())

    def tool_requirements(self) -> dict[str, set[str]]:
        self._refresh_if_changed()
        return {skill.name: set(skill.required_tools) for skill in self.skills
                if skill.required_tools}

    def validation_report(self) -> dict[str, list[str]]:
        self._refresh_if_changed()
        return {skill.name: list(skill.warnings) for skill in self.skills if skill.warnings}
