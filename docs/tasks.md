# Otto Task System

Otto uses one PostgreSQL-backed task model for both lightweight progress and
resumable multi-agent work. A flat set of tasks behaves like a Todo list. Adding
dependencies and owners turns the same records into a directed acyclic graph.

## Why one system

The old TodoWrite teaching pattern replaces an in-memory array. It is useful for
showing progress, but it disappears on restart and cannot safely coordinate two
workers. The s12 teaching system adds persistent records, dependencies, and
ownership. Otto combines those ideas behind the current four-tool protocol:

- `task_create`: create one pending task and receive its stable numeric id.
- `task_update`: patch fields, claim/release, complete/delete, and add DAG edges.
- `task_get`: read full details and dependency state for one task.
- `task_list`: read the current session snapshot and progress counts.

## Persistence and isolation

`task_lists` scopes a list to a chat session (with a reserved `shared` scope for
future team workflows). `tasks` stores status, owner, descriptions, active-form
labels, and metadata. `task_dependencies` stores normalized `blockedBy` edges;
the reverse `blocks` relation is derived rather than duplicated.

PostgreSQL sequences provide ids that are not reused. Transactions and row locks
make claim checks atomic. A partial unique index prevents one owner from holding
two `in_progress` tasks in the same list.

## State machine

```text
pending --start/claim--> in_progress --verify/complete--> completed
   ^                           |
   +--------- release ---------+

any non-deleted state --delete--> deleted
```

A task cannot enter `in_progress` while any dependency is unfinished. Adding an
edge runs recursive cycle detection. Missing, cross-list, self, and cyclic
dependencies are rejected transactionally.

## Agent loop and context

The full task records remain outside the model context. Before each LLM call,
the code-maintained Agent Status Bar reads the current session's durable list
and injects aggregate progress plus only open task ids, titles, status, owner,
and blocking state at the end of the trajectory. The model reads details on
demand through `task_get` or `task_list`. This keeps task state alive across
restarts and context compaction without injecting descriptions and metadata.

When a turn creates or updates tasks and then stops with open work, a guarded
`Stop` hook gives it one continuation. It never nags on an unrelated later turn,
and it cannot loop indefinitely because the agent loop permits only one
hook-driven continuation.

## Quality gates and observability

`TaskCreated` can reject malformed tasks before they are stored. `TaskCompleted`
can reject completion when tests or acceptance checks fail. `TaskUpdated` is the
read-only telemetry event used by tracing and the dashboard. The Tasks page shows
the active session's durable list and dependency state.

## Agent policy

Use tasks for work with three or more meaningful steps, explicit multi-item
requests, or work that must survive a later session. Skip them for simple
questions. Keep at most one task `in_progress` per owner and complete it only
after verification.
