---
paths:
  - "README.md"
  - "gca-mcp/README.md"
  - "playground/*/GAME_DESIGN.md"
  - ".claude/rules/**/*.md"
  - ".claude/agents/**/*.md"
  - ".claude/skills/game-pipeline/SKILL.md"
  - ".claude/skills/game-pipeline/dispatch-contract.md"
  - ".claude/skills/game-pipeline/references/iteration-procedures.md"
  - ".claude/skills/game-pipeline/references/design/gdd-template.md"
  - ".claude/skills/game-pipeline/references/quality-profiles.md"
  - ".claude/skills/game-pipeline/references/routing/genres.md"
---

# Documentation Contract Rules

This Rule provides non-blocking maintenance guidance only. Runtime enforcement
remains in tool allowlists, hooks, MCP handlers, gates, and workspace isolation.

- Keep one canonical authority per concern. Consumer documents link to that authority
  instead of copying schemas, inventories, thresholds, matrices, or workflow
  checklists.
- MCP ToolDefs own public tool schemas. The GDD template owns section semantics;
  `.claude/skills/game-pipeline/references/quality-profiles.md` owns art, UI, VFX,
  level-capability, and waiver policy; and
  `.claude/skills/game-pipeline/references/routing/genres.md` owns startup terminology.
- `.claude/skills/game-pipeline/dispatch-contract.md` owns subagent dispatch fields,
  returns, artifact ownership, and continuation policy. Documentation describes
  boundaries but does not grant access; tool allowlists, hooks, MCP handlers, and
  isolation enforce them.
- A change to behavior, schema, authority, ownership, or gate semantics must update
  its implementation, consumers, examples, references, and focused tests in the
  same change. Editorial-only changes do not require behavior tests.
- Asset and VFX documentation must follow the selected quality profile and canonical
  MCP contracts; never present one profile or a manual fallback as the universal path.
- Distinguish evidence producers from gate decisions and visual judgments from
  deterministic runtime checks.
- Do not `@`-import large runbooks into root instructions. Use ordinary paths and
  load the authority only when relevant.
- After renaming a path or canonical term, search the repository for stale references
  and add or update a focused contract test when behavior or validation is affected.
