# GCA Repository Instructions

## Scope

This repository contains the game-creation pipeline, the `gca` MCP server,
shared contracts, and generated Phaser 4 games under `playground/<game-slug>/`.
During a pipeline, `<GAME_DIR>` is the absolute root of one active game project,
typically `<REPO_ROOT>/playground/<game-slug>/`. MCP `game_dir` arguments and
dispatch `GAME_DIR` fields refer to the same path.
Apply the routed scoped instructions plus these global rules; keep this file free
of duplicated tool counts, thresholds, matrices, and config defaults.

## Global Invariants

- During a game pipeline, use `gca` MCP tools for asset generation,
  post-processing, browser/game verification, video analysis, and gates. Never
  invoke bundled runners, Playwright, or gates directly. Shell commands are
  limited to orchestration required by the pipeline skill; repository development
  may run lint, compile checks, focused diagnostics, and tests.
- **MUST NOT fabricate** assets, state transitions, counters, evidence,
  screenshots, or verdicts. Defaults and thresholds live in `gca.config.yaml`;
  hooks, MCP handlers, and composite gates enforce them.
- **MUST NOT hot-restart or close** the MCP server mid-pipeline. On disconnect,
  pause for user reconnection and resume from persisted evidence.
- Final assets require declared provenance and usage rights, manifest integration,
  and runtime/visual fitness.

## Sources of Truth

- `GAME_DESIGN.md` owns authored design and mechanics; during the automated
  pipeline, only design-agent writes it. Only `level_model: authored` uses
  `levels/*.json`.
- `quality_profile` controls art, UI sizing, and VFX policy.
  `assets/asset-manifest.json` owns runtime keys, final paths, shipped dimensions,
  and metadata sidecars.
- Workers write only owned artifacts and sidecars; shared authorities follow the
  single-writer contracts. Crop-seeded stills use
  `produce_static_asset({ref_image: ..., asset_type: <canonical category>})`;
  every UI/lettering/widget uses `asset_type:"ui"`. Layout evidence uses
  `materialize_layout_alignment`.
- `gca.config.yaml` owns runtime defaults. `<GAME_DIR>/.playtest/` owns pipeline
  progress and evidence.

## Task Routing

- New game creation or pipeline resume: `.claude/skills/game-pipeline/SKILL.md`.
- Agent contracts: `.claude/skills/game-pipeline/dispatch-contract.md`,
  `.claude/agents/*.md`, and `.claude/rules/*.md`.
- Design contracts: the game-pipeline skill's `references/design/gdd-template.md`
  and `references/quality-profiles.md`.
- Scoped implementation: `gca-mcp/CLAUDE.md` for MCP; `playground/CLAUDE.md` for
  generated games.

## Game-Pipeline Recovery

For interrupted game pipelines only, follow
`.claude/skills/game-pipeline/SKILL.md` → "10. Resume After Interruption".
Select one bound `<GAME_DIR>` (ask if multiple `playground/<game-slug>/`
candidates exist); validate state/resume/audit breadcrumbs against
current artifacts and in-flight work, then continue from the first incomplete
phase. Stop on authentication, quota, permission, or user-decision blockers.
