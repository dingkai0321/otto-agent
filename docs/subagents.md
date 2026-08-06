# Otto Subagents

Otto has two complementary child-agent paths:

- `agent_spawn` runs a Otto-native, read-only specialist for research, planning,
  review, or another bounded investigation.
- `delegate_task` runs the external pi coding agent for file edits, commands,
  tests, and implementation work. It remains opt-in through Experimental.

## Native child contract

`agent_spawn` receives a self-contained prompt because the child does not see
the parent's messages. It starts a fresh `messages` list and its own bounded
agent loop. Only the final conclusion returns as the parent tool result;
intermediate calls remain in child telemetry and a record under
`OTTO_HOME/subagents/`.

Four roles define distinct instructions and allowlisted tools:

| Role | Job | Typical tools |
|---|---|---|
| `general` | bounded non-coding investigation | web, knowledge, calendar, task reads |
| `researcher` | gather and synthesize evidence | web, knowledge, GitHub reads |
| `planner` | dependencies, risks, verification plan | knowledge and task reads |
| `reviewer` | independent findings and acceptance review | GitHub, knowledge, task reads |

Child registries never contain `agent_spawn`, `delegate_task`, `task_create`, or
`task_update`, so recursive spawning and child-side task mutation are impossible
at the harness boundary.

## Permissions

Starting a child requires approval because it spends additional model calls and
may use tools. The child receives a fork of the run-scoped HookManager, including
the gateway approval hook and hard permission policy. Every child tool call
therefore passes through the same `PreToolUse` and `PermissionRequest` pipeline
as a parent call.

`SubagentStart`, `SubagentEvent`, and `SubagentStop` carry the child id, role, and
depth to traces and the dashboard. Ordinary child lifecycle events are also
tagged with `subagent_id` so usage and failures remain attributable.

## Task handoff

Pass a pending `task_id` to bind delegation to the durable task system:

```text
pending --atomic claim(owner=child id)--> in_progress
    ├── child succeeds + TaskCompleted allows --> completed
    └── error / iteration limit / hook blocks --> pending (owner cleared)
```

The claim uses PostgreSQL row locks and the same dependency checks as ordinary
`task_update`. A blocked task cannot be delegated. `TaskCompleted` remains the
quality gate, so a child cannot bypass tests or acceptance hooks.

## Boundaries

Native children are synchronous: the parent waits for their final conclusion.
They are intentionally read-only and best for attention isolation. Coding and
workspace mutation stay with pi, whose separate sandbox and execution model are
already designed for that job.
