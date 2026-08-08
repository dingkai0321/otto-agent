# Prompt runtime

Otto keeps stable instructions, dynamic context, tools, and conversation state
in separate request fields. A full agent call is assembled in this order:

1. `system`: `SOUL.md` followed by the installed Skill metadata catalog
   (`name` and `description` only).
2. `tools`: currently visible tool `name`, `description`, and `input_schema`.
3. The bounded recent conversation history.
4. The current user message, prefixed once with its event timestamp.
5. During the loop, assistant tool calls and user-role tool results. A complete
   `SKILL.md` enters here only after `load_skill` is called; referenced Skill
   files are added only after `load_skill_resource` is called. Tool results have
   execution timestamps and per-session call numbers.
6. Optional framework-context message appended at the trajectory tail:
   retrieved long-term memory, `UserPromptSubmit` context, and extension-provided
   `PreLLMCall` context.
7. A code-generated `<agent_status>` user message at the absolute end:
   current time, workspace, loop budget, durable task progress, tool counts,
   repeat warnings, last tool result, and active Skills.

The framework-context message and Agent Status Bar are created again for every
LLM call. Neither is stored in `Session.history`, so retrieved facts, timestamps,
counters, and task snapshots do not accumulate. The system prompt remains the
stable `SOUL + Skill catalog` prefix. Provider and model names are not injected.
Tool definitions remain in the provider's native `tools` field rather than
being rendered into system text.

In compact form:

```text
request
├── system: SOUL → Skill name/description catalog
├── tools: name → description → input schema
└── messages
    ├── recent history
    ├── current user input
    ├── assistant/tool trajectory, including activated Skill content
    ├── optional dynamic framework context
    └── trailing Agent Status Bar (recomputed every LLM call)
```
