# Otto lifecycle hooks

Hooks are Otto's one extension surface around the stable agent loop. The loop
declares *when* a lifecycle point happens; registered callbacks decide what to
observe, validate, rewrite, block, or enrich there.

Task work adds three lifecycle boundaries: `TaskCreated` can reject a task
before storage, `TaskCompleted` can enforce verification before completion, and
`TaskUpdated` streams committed state to observers and the dashboard.
Native child runs add `SubagentStart` and `SubagentStop`; their internal tool
calls retain the same permission hooks and carry `subagent_id`, role, and depth.

```text
UserPromptSubmit
        ↓
PreLLMCall → AgentStatus → PreCompact / PostCompact → TextDelta / LLMResponse
        ↓
PreToolUse: normal hooks → terminal permission hook → read-only observers
        ↓
PermissionRequest (when needed)
        ↓
tool function
        ↓
PostToolUse / ToolFailure
        ↓
Stop
```

The implementation is [`otto/hooks.py`](../otto/hooks.py). `Otto.hooks` is the
persistent registration template. Each turn forks it, then adds that gateway's
stream observer and approval hook; turn-local callbacks therefore never leak
into later conversations.

## Registering a hook

```python
from otto.hooks import HookEvent, HookResult


def add_tenant_context(ctx):
    return HookResult(additional_context="tenant=local")


def protect_generated_files(ctx):
    path = (ctx.data.get("args") or {}).get("path", "")
    if path.endswith(".generated.py"):
        return HookResult(block_reason="generated files are read-only")


otto.hooks.register(HookEvent.USER_PROMPT_SUBMIT, add_tenant_context)
otto.hooks.register(HookEvent.PRE_TOOL_USE, protect_generated_files)
```

Callbacks receive a `HookContext` with `event`, mutable current `data`, and the
run-scoped `hooks` manager. They return `None` to observe without changing
anything, or `HookResult` with any of:

| Field | Effect |
|---|---|
| `updates` | Rewrite event data, such as `args`, `output`, `prompt`, or `system` |
| `block_reason` | Stop the current prompt/tool/LLM boundary |
| `additional_context` | Add deterministic context before an LLM call |
| `permission` | Return `allow`, `ask`, `deny`, or `passthrough` |
| `continue_prompt` | At `Stop`, inject one follow-up instruction and continue |
| `prevent_continuation` | At `Stop`, suppress a requested continuation |

Lower numeric priorities run first; equal priorities keep registration order.
Optional hook failures are collected without crashing the turn. A `critical`
hook failure blocks the protected operation.

## Safety order

`PreToolUse` has two phases:

1. normal hooks may inspect or rewrite arguments;
2. terminal hooks run after all normal hooks.

The permission policy is a critical terminal hook. It therefore evaluates the
final rewritten arguments. Permission decisions merge monotonically — `deny`
beats `ask`, which beats `allow` — so an extension cannot weaken a hard deny.
Read-only Observer adapters are terminal hooks after the permission phase.

An `ask` decision triggers `PermissionRequest`. CLI and Dashboard install a
turn-local approval hook there. Unattended gateways install none, so the
registry fails closed.

## Events

Core control events:

| Event | Purpose |
|---|---|
| `UserPromptSubmit` | rewrite/block a prompt or inject context |
| `PreLLMCall` | inspect/rewrite system and messages before inference |
| `AgentStatus` | observe the exact code-generated status snapshot sent to the model |
| `PreCompact` / `PostCompact` | gate and observe auto/manual/reactive context compaction |
| `LLMResponse` / `TextDelta` | usage tracing and streaming output |
| `PreToolUse` | argument rewrite, policy and permission |
| `PermissionRequest` / `PermissionDecision` | interactive approval lifecycle |
| `PostToolUse` / `ToolFailure` | output rewrite, redaction and telemetry |
| `Stop` | cleanup or one guarded continuation |

The same bus carries memory (`RetrievalDecision`, `ConsolidationComplete`),
graphs (`GraphStart`, `GraphNodeStart`, `GraphNodeEnd`, `GraphRoute`, `GraphEnd`),
sub-agent progress, wake-word scans, and config changes.

## Observer compatibility

Gateways still accept the old `observer(kind, event)` callback. `add_observer()`
wraps it as a read-only wildcard hook and maps canonical lifecycle names back to
the established compact events (`text`, `llm`, `tool`, `gate`, `route`, and so
on). There is no second notification pipeline: Dashboard streaming, CLI status,
JSONL tracing and OTel all consume HookManager events.

`Stop` continuation has a one-shot guard. A hook may request one extra LLM
iteration, but it cannot recursively keep the agent alive forever.
