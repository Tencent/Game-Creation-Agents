---
paths:
  - "playground/*/.playtest/**/*"
  - ".claude/agents/verify-agent.md"
  - ".claude/agents/critic-agent.md"
  - ".claude/skills/game-pipeline/SKILL.md"
  - ".claude/skills/game-pipeline/dispatch-contract.md"
  - ".claude/skills/game-pipeline/references/iteration-procedures.md"
  - ".claude/skills/game-pipeline/references/verify/**/*.md"
  - ".claude/loop.md"
  - ".claude/hooks/audit-log.sh"
  - ".claude/hooks/enforce-pipeline.sh"
  - ".claude/hooks/validation/validate-state-json.py"
  - "scripts/preflight_env.py"
  - "gca.config.yaml"
  - "gca-mcp/src/gca_mcp/evidence.py"
  - "gca-mcp/src/gca_mcp/playtest_layout.py"
  - "gca-mcp/src/gca_mcp/verify_epoch.py"
  - "gca-mcp/src/gca_mcp/tools/lifecycle.py"
  - "gca-mcp/src/gca_mcp/tools/verify_tools.py"
  - "gca-mcp/src/gca_mcp/tools/video_tools.py"
  - "gca-mcp/src/gca_mcp/runners/verify/**/*"
---

# Pipeline Evidence Integrity Rules

This Rule provides non-blocking integrity guidance only. Hooks, MCP handlers, and
gates remain the enforcement points for pipeline decisions.

- Never create, edit, copy, or reuse an artifact merely to satisfy a gate. Evidence
  must come from its canonical producer and the current build and run.
- Pipeline state is written through `mcp__gca__write_pipeline_state` unless a hook
  explicitly owns the transaction. State, resume breadcrumbs, audit events,
  guarded counters, and filesystem evidence must agree before the pipeline advances.
- Evidence that carries a pipeline, build, verification, or producer identity must
  match the current operation. Do not mix run IDs or reuse evidence from an older
  input set.
- Missing, stale, malformed, contradictory, or manually substituted evidence is not
  PASS. Operational unavailability is reported as unavailable, never converted into
  fabricated success or content failure.
- `gca.config.yaml` owns runtime defaults, limits, thresholds, and retry budgets.
  Canonical ToolDefs own public tool schemas; hooks, MCP handlers, and gates consume
  those authorities and enforce evidence requirements and safety escapes. Do not
  duplicate numeric values in prose rules.
- Producer ordering, critic requirements, gate sequencing, and recovery procedures
  remain owned by the game-pipeline Skill, dispatch contract, agent specifications,
  and MCP implementations. This Rule states integrity invariants only.
- Stop-hook safety escapes report unresolved work; they never convert failed or
  incomplete evidence into successful evidence.
