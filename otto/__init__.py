"""otto-agent — a minimal, transparent, local-first Otto.

Four pillars, one module each:
  harness  → otto/runtime + otto/gateway  (scaffolding around the raw LLM)
  loop     → otto/loop                      (observe → reason → act → repeat)
             otto/graph                     (opt-in structure around the loop — extends this pillar)
  memory   → otto/memory                    (procedural / semantic / episodic)
  ops      → otto/ops + evals/              (trace → eval → gate → release)
"""

__version__ = "0.1.0"
