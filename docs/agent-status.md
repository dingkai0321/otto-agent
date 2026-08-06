# Otto Agent Status Bar

The Agent Status Bar is a deterministic, lossy projection of current runtime
state. Otto appends it after the latest conversation/tool event on every LLM
iteration. It supplements the original trajectory and never replaces it.

```xml
<agent_status version="1" generated_by="otto_runtime">
  <time iso="2026-08-06T14:30:00+08:00" timezone="CST"/>
  <environment workspace="/workspace" process_cwd="/workspace"
               os="Darwin" shell="/bin/zsh" python="3.13.5"/>
  <loop iteration="3" max_iterations="10" remaining="7" elapsed_seconds="1.250"/>
  <tasks total="4" completed="2" in_progress="1" pending="1"
         blocked="0" progress_percent="50">
    <task id="3" status="in_progress" blocked="false" owner="otto">Run tests</task>
  </tasks>
  <tools session_calls="3" turn_calls="1" failures="0" observation_chars="4200">
    <tool name="read_file" calls="2"/>
    <repeat_warning tool="read_file" identical_calls="2"/>
    <last_tool name="read_file" status="ok"/>
  </tools>
  <active_skills><skill>coding</skill></active_skills>
</agent_status>
```

## Source of truth

- Time and elapsed duration come from the host clock.
- Workspace comes from `OTTO_WORKSPACE`; process cwd, OS/release, shell, and
  Python version come directly from the running process.
- Task progress is aggregated by PostgreSQL for the active session on every
  iteration. Counts include all completed tasks, while only a bounded set of
  open-task labels is hydrated and shown.
- Per-session and per-turn tool totals, failures, identical-call warnings, and
  observation size are maintained by code as tools execute. Tool observations
  carry `[YYYY-MM-DD HH:MM:SS ±ZZZZ] Tool call #N` prefixes. No LLM scans or
  summarizes history to produce these numbers.
- Active Skills come from run-scoped Hook state after successful `load_skill`.

## Accuracy and poisoning controls

The status is emitted as the `AgentStatus` lifecycle event, so tracing and
gateways can monitor exactly what the model received. A failed task query is
reported as unavailable rather than guessed. User-influenced task labels and
filesystem paths are length-bounded and XML-escaped; descriptions, tool
arguments, and tool outputs are never copied into the status bar. The fixed
header explicitly marks labels as untrusted data.

The bar is an ephemeral trailing message. It is included in context-budget
estimation but is not written to `Session.history`, PostgreSQL chat history, or
compaction summaries. Provider and model names are intentionally absent.

## Update strategy: replace the trailing projection

Otto uses **per-call replacement**, not persistent status accumulation. The
status object is never added to the mutable trajectory. Before each model call,
Otto appends exactly one freshly rendered user-role status message to a request
copy; the next call builds another request copy with the new status.

This is the better tradeoff for Otto because its working history is already
bounded, one loop is capped at a small number of iterations, and the status can
contain a comparatively large task/environment snapshot. Replacement only
invalidates the short suffix beginning at the previous status, while avoiding
stale TODO states, ambiguous counters, and unbounded status tokens. The stable
system, tools, original user messages, and older trajectory remain unchanged.

## Event timestamps and error observations

Original user events receive a timestamp once when submitted; that timestamp is
kept in working history and restored from PostgreSQL metadata when a session is
reopened. Every tool observation receives its execution timestamp and global
per-session call number. Timestamps are never placed in the system prompt.

Tool exceptions remain original observations rather than being reduced to the
status projection. Otto returns the exception type/message, redacted argument
JSON, a bounded traceback, and a targeted recovery suggestion. The status bar
only reports failure counts and the last tool status, so it does not duplicate
large traces or arguments.
