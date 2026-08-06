"""Validate every SKILL.md in the repo — run by CI on community PRs.

Checks Agent Skills frontmatter, routing quality, package naming, optional
otto.json, name uniqueness, and a soft body-length budget. Exit 1 on failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from otto.memory.procedural.loader import _parse  # noqa: E402

MAX_BODY_LINES = 80


def main() -> None:
    problems: list[str] = []
    names: dict[str, Path] = {}

    files = sorted((REPO / "skills").rglob("SKILL.md"))
    if not files:
        problems.append("no SKILL.md files found under skills/")

    for path in files:
        rel = path.relative_to(REPO)
        skill = _parse(path)
        if skill is None:
            problems.append(f"{rel}: missing/invalid frontmatter (need `name` and `description`)")
            continue
        if skill.name in names:
            problems.append(f"{rel}: duplicate name '{skill.name}' (also in {names[skill.name]})")
        names[skill.name] = rel
        if path.parent.name != skill.name:
            problems.append(
                f"{rel}: folder '{path.parent.name}' must match skill name '{skill.name}'"
            )
        if len(skill.description.split()) < 5:
            problems.append(f"{rel}: description too short to guide selection — say when to use it")
        if len(skill.body.splitlines()) > MAX_BODY_LINES:
            problems.append(f"{rel}: body over {MAX_BODY_LINES} lines — load_skill injects it into context, keep it tight")
        for warning in skill.warnings:
            if "body exceeds" not in warning:
                problems.append(f"{rel}: {warning}")
        manifest = path.parent / "otto.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                required = data.get("required_tools", [])
                if not isinstance(required, list) or not all(
                    isinstance(name, str) and name.strip() for name in required
                ):
                    problems.append(f"{rel}: otto.json required_tools must be strings")
            except json.JSONDecodeError as exc:
                problems.append(f"{rel}: invalid otto.json — {exc}")

    if problems:
        print("skill validation FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print(f"skill validation OK — {len(names)} skill(s): {', '.join(sorted(names))}")


if __name__ == "__main__":
    main()
