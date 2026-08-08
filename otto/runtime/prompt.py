"""Prompt assembly with a real stable system prefix and dynamic turn context.

The two sections intentionally travel through different API fields:

* ``stable_prefix`` becomes ``system`` and contains only SOUL + Skill metadata.
* ``dynamic_context`` becomes a temporary tail message immediately before the
  Agent Status Bar for this LLM call.

That keeps the installed Skill catalog discoverable without putting changing
retrieval or hook state into the cache-friendly system prefix. Time, task
progress, environment, and loop counters belong to the trailing Agent Status
Bar assembled by :mod:`otto.runtime.status`.
Tool schemas are assembled separately by ``ToolRegistry`` and passed through
the provider's native ``tools`` request field.
"""

from __future__ import annotations

from dataclasses import dataclass

from otto.hooks import HookManager


@dataclass(frozen=True)
class PromptBundle:
    stable_prefix: str
    dynamic_context: str

    @property
    def system(self) -> str:
        """The stable system prompt; dynamic context must not be folded into it."""
        return self.stable_prefix


class PromptAssembler:
    """Build stable system instructions separately from per-turn context."""

    def __init__(self, settings, memory=None) -> None:
        self.settings = settings
        self.memory = memory

    def build(self, user_message: str, soul: str,
              hooks: HookManager | None = None) -> PromptBundle:
        stable = [soul]
        if self.memory is not None:
            catalog = self.memory.skill_catalog()
            if catalog:
                stable.append(
                    "Available skills (metadata only):\n"
                    + catalog
                    + "\nWhen a skill is relevant, call `load_skill` with its exact name "
                      "before following its instructions. Loading a skill may expose its "
                      "specialist tools on the next loop iteration. Read only specifically "
                      "needed bundled files with `load_skill_resource`."
                )

        dynamic = []
        if self.memory is not None:
            retrieved = self.memory.gated_retrieve(user_message, hooks=hooks)
            if retrieved:
                dynamic.append("Relevant memory:\n" + retrieved)
        return PromptBundle("\n\n".join(stable), "\n\n".join(dynamic))
