---
name: your-skill-name
description: Describe what this skill does and when to use it, using phrases people actually say. Also state when not to use it and include likely confusing cases as negative triggers.
---

<!--
To contribute: copy this file to skills/community/<your-skill-name>/SKILL.md
and open a PR. CI checks the frontmatter (name + description required — the
official Anthropic Agent Skills format). Keep the body under ~60 lines:
skills are loaded into the prompt only when they match, but shorter is better.
-->

<!-- Optional package resources live beside SKILL.md:
references/ for details loaded with load_skill_resource; scripts/ for approved
deterministic execution; assets/ or templates/ for files copied into the
workspace. A sibling otto.json may declare specialist schemas that appear only
after activation: {"required_tools": ["tool_name"]}. -->

## Instructions

Step-by-step guidance for the model. Be concrete: name the tools to call
(`create_event`, `save_note`, `send_message`), the defaults to assume, and
the tone to take.

## Edge cases

| Situation | Do |
|---|---|
| Something ambiguous | Ask one clarifying question |
