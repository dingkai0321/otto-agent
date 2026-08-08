# Agent Skills

Otto implements three-level progressive disclosure. Skills are procedural
memory: they explain when and how to perform a specialized workflow; tools are
the executable capabilities used by that workflow.

## Package shape

```text
skill-name/
├── SKILL.md               required metadata + core workflow
├── otto.json              optional specialist-tool activation
├── references/            detailed text loaded only when needed
├── scripts/               approved deterministic .py/.sh actions
├── assets/                source files used in output
└── templates/             files copied into the workspace
```

`SKILL.md` must contain only `name` and `description` in YAML frontmatter. Keep
the body below 500 lines and move detailed variants to `references/`.

The description is a routing condition, not marketing copy. Say what the skill
does, when to use it, and when not to use it. Include confusing negative cases.

## Runtime data flow

1. `SkillLoader` scans bundled and `$OTTO_HOME/skills` packages. Personal skills
   override bundled skills with the same declared name.
2. Only `name + description` enters the stable system-prompt prefix.
3. The model calls `load_skill(name)`. Otto injects the exact `SKILL.md`, records
   it in `active_skills`, returns a resource index, and exposes any specialist
   schemas declared by that skill for the next loop iteration.
4. `load_skill_resource` reads only the referenced UTF-8 text needed now, with
   50,000-character pagination. Paths are resolved inside the package; absolute
   paths, `..`, symlink escape, and missing files are rejected.
5. `run_skill_script` runs only `.py`, `.sh`, or `.bash` files under the active
   package's `scripts/`, without shell interpolation, after user approval.
6. `copy_skill_asset` copies an `assets/` or `templates/` file/directory into the
   workspace, never overwrites, and requires user approval.

Resource/script/asset tools are hidden until a skill is active. Child agents
start with a fresh activation set and must load their own skill.

## Dynamic tool schemas

An optional `otto.json` keeps specialist tool definitions out of every prompt:

```json
{
  "required_tools": ["render_slides", "inspect_pptx"]
}
```

The implementations remain registered behind the normal permission pipeline.
Before activation their schemas are absent from the model request; after
`load_skill`, `run_loop` recomputes schemas and exposes them on the next call.
Tools not declared by any skill remain core tools and are always visible.

## Compaction

`load_skill` results are excluded from ordinary tool-result micro-compaction.
When conversation summary or safe snipping removes their original message,
`ContextManager` reinjects the exact active `SKILL.md` under the separate
`OTTO_SKILL_REINJECT_BUDGET_CHARS` budget (default 50,000). If that budget is
exceeded, the context tells the model to call `load_skill` again instead of
trusting an approximate summary.

## Authoring and validation

Copy `skills/TEMPLATE.md` for a new package. Run:

```bash
python scripts/validate_skills.py
```

Validation checks package/name alignment, unique names, routing description
quality, body size, and optional `otto.json` shape. The runtime also hot-reloads
when `SKILL.md`, its manifest, or a bundled resource changes.
