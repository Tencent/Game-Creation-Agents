---
name: game-pipeline
description: "End-to-end pipeline that builds a new, playable Phaser 4 browser game from a natural-language idea (design, build, verify, iterate; Startup derives routing metadata). Trigger when the user asks to make, create, build, or generate a game, describes a game concept, or describes interactive mechanics, levels, or player actions, even without the word 'game'. Do NOT trigger for editing, debugging, or adding features to an existing game codebase, or for general questions about games."
---

# Game Pipeline — Main Agent

You are the pipeline orchestrator. Route work, dispatch agents, join concurrent
branches, persist state, run composite gates, and report outcomes. Do not produce
the game yourself.

## 0. Role and Authorities

The main agent may derive Startup routing metadata. It must not write game code,
GDD content, level JSON, asset prompts, or visual assets, and must not rewrite a
subagent's output.

Paths below use `<REPO_ROOT>` for the repository root,
`<SKILL_DIR>` for `<REPO_ROOT>/.claude/skills/game-pipeline/`, and
`<GAME_DIR>` for the absolute root of the game project selected for this run,
typically `<REPO_ROOT>/playground/<game-slug>/`. MCP `game_dir` arguments and
dispatch `GAME_DIR` fields refer to that same path.

Authority is intentionally split:

- This file (`<SKILL_DIR>/SKILL.md`) owns phase order, concurrency, state
  transitions, gates, and recovery.
- `<SKILL_DIR>/dispatch-contract.md` owns dispatch fields, returns, and
  retry/continuation rules.
- `<REPO_ROOT>/gca.config.yaml` owns runtime defaults, limits, and thresholds.
- `<SKILL_DIR>/references/routing/genres.md` and
  `<SKILL_DIR>/references/quality-profiles.md` own Startup routing terminology
  and capability policy.
- `<SKILL_DIR>/references/iteration-procedures.md` supplements
  "9. Iteration Loop" with detailed diagnostics.
- Agent specs under `<REPO_ROOT>/.claude/agents/` own phase-internal craft and
  tools. Do not read them; use `<SKILL_DIR>/dispatch-contract.md`.

The reference-document boundary does not prohibit reading canonical artifacts
under the bound `<GAME_DIR>`. Read only those needed to route or validate work,
including `<GAME_DIR>/GAME_DESIGN.md`,
`<GAME_DIR>/assets/asset-manifest.json`, canonical state and reports under
`<GAME_DIR>/.playtest/`, and the exact outputs returned by a subagent.

### Allowed main-agent tools

| Tool | Main-agent use |
|---|---|
| `mcp__gca__init_workspace` | Open the selected game workspace |
| `mcp__gca__ping` | Connectivity diagnosis |
| `mcp__gca__preflight_check` | MCP/network/filesystem readiness |
| `mcp__gca__asset_pipeline_smoke_test` | Generated-asset readiness |
| `mcp__gca__finalize_asset_manifest` | Post-join manifest reduction |
| `mcp__gca__write_pipeline_state` | Atomic state/resume update |
| `mcp__gca__gate_check` | Composite phase/release decisions |

All generation, browser judgment, gameplay, recording, and video analysis tools
belong to producer or verify agents. Direct shell use is limited to the
orchestration commands named here: preflight, server lifecycle, directory-safe
prompt archival, and `audit-log.sh`.

Use:

- `Agent` for design, level design, scaffold, asset batches, integration, verify,
  critic, and targeted fixes.
- One tracked background `Workflow` for serial scene implementation
  (`Branch A`, defined in §1).
- `TaskOutput` only on older Claude Code versions that do not expose the
  Workflow output path directly.

All dispatch paths are absolute. Resolve `<SKILL_DIR>` to this file's directory,
but pass only the phase `REF_DIR` and explicit shared paths declared by
`dispatch-contract.md`.

Never hot-restart, close, or replace the `gca` MCP server during a pipeline. On
disconnect, persist the recovery breadcrumb, ask the user to reconnect, then
follow "10. Resume After Interruption".

---

## 1. Pipeline State Machine

```text
Startup
  → Design
  → [LEVEL_MODEL=authored ? Level Design : skip]
  → Scaffold → scaffold_done gate
  → verified local server
  → fork
      ├─ Branch A: implement scenes serially in one tracked Workflow
      └─ Branch B: resolve independent asset batches with parallel Agent calls
  → join the tracked implementation Workflow and all declared asset batches
  → [file-backed entries ? finalize manifest metadata
                         : record no-file source completion]
  → Pipeline Step 3 gate
  → Asset Integration
  → bounded iteration loop:
      Verify(MODE=iterate) → iteration_ready gate → Critic
      → [blocked ? fix all blockers → seal new build_digest → repeat
                 : continue]
  → Verify(MODE=full) → final Critic → ready_to_ship gate
  → cleanup and report

Any blocked, paused, aborted, or exhausted exit
  → cleanup and report the exact unresolved state
```

Do not skip a predecessor because files happen to exist. A phase completes only
when its outputs are current, valid, and attributable to this pipeline/build.

### 1.1 Build concurrency and join

Scaffold blocks until it creates the manifest and test contracts. Then Branch A
implements scenes serially in one tracked Workflow while Branch B fans out
disjoint asset batches. Save the Workflow identity before Branch B; join that
exact task after the asset calls return, waiting rather than duplicating it.
Workers never write the GDD or shared manifest. Integration starts only after
both branches are accepted and the manifest finalizer completes.

### 1.2 Return handling

Follow `dispatch-contract.md`: validate and persist `done`; retain valid outputs
and every issue from `partial`, continuing only when safe; stop and report
`blocked`. A timeout does not prove termination, so confirm task state before any
contract-authorized continuation.

---

## 2. Persistent State and Evidence

Track five state groups without copying canonical artifact contents into state:

1. **Run identity and immutable routing**: `pipeline_run_id`, `GAME_DIR`,
   `PROMPT_PATH`, `GENRE`, `DESIGN_AXES`, `QUALITY_PROFILE`, and `LEVEL_MODEL`.
   The archived `PROMPT.md` is canonical for `GAME_IDEA`.
2. **Derived routing invariants**:
   `INTERACTION_PROFILE = DESIGN_AXES.physics` and
   `LEVEL_DESIGN_REQUIRED = (LEVEL_MODEL == "authored")`. Persist them where the
   state API requires, but always derive or validate them rather than routing
   independently.
3. **Pipeline control**: `phase`, `current_step`, `status`, `next_action`,
   completed phases, `state_transaction_id`, and the current `build_digest`.
4. **Build/in-flight state**: `GDD_PATH`, ordered `SCENE_LIST`, `has_levels`,
   exact authored `level_files` when applicable (bound to `LEVEL_FILES` for
   dispatch), asset/source completion, current `URL`, tracked Workflow
   identity/output path, declared batch identities, per-scene/per-batch
   completion and issues, and current scaffold, manifest, and integration
   receipt/report paths.
5. **Verification/release state**: `verify_run_id`, verify mode and lifecycle
   status, evidence/report paths bound to the current `pipeline_run_id` and
   `build_digest`, `verify_runs`, `iteration_count`, `visual_verified`,
   `first_pass_complete`, `playthrough_recorded`, gate and critic results,
   release waivers, design debt, and known issues.

Persist routing, control, and summary fields through
`mcp__gca__write_pipeline_state`; place in-flight identities and output paths in
its resume `details`. Keep verify mode/lifecycle in `current-run.json` and
canonical evidence in MCP-sealed reports and receipts. `URL` and task status are
ephemeral and must be revalidated after resume.

State and resume files are breadcrumbs, not proof. Completion requires canonical
artifacts plus matching audit, freshness, provenance, and run identity.

Single-writer rules:

- design-agent alone writes `GAME_DESIGN.md`;
- when `LEVEL_MODEL=authored`, level-design-agent alone writes
  `<GAME_DIR>/levels/`, including `stage_*.json` and required extension files;
- scaffold creates the manifest; asset workers write owned files/sidecars; the
  manifest finalizer performs the post-join reduction;
- `mcp__gca__write_pipeline_state` owns state/resume writes.

For guarded values, append the canonical audit event before the state update:

- `assets_generated` before setting asset/source completion;
- `verify_run` before incrementing `verify_runs`;
- `iteration_complete` before incrementing `iteration_count`.

Each counter update is monotonic and advances by at most the configured increment.
After all writers for any code, asset, GDD, manifest, or level change set have
completed and joined, call `mcp__gca__write_pipeline_state` and use its returned
`build_digest` before beginning another verify run. A successful release requires
`verify_runs > iteration_count`; unresolved exits need not satisfy this
release-only invariant.

When permitted by the selected `QUALITY_PROFILE` policy, a valid no-file asset
contract is explicit: the manifest sets `fileAssetsRequired:false`, `assets:[]`,
and non-empty `runtimeSources`. Absence of files alone never proves completion.

Never fabricate artifacts, counters, task status, evidence, screenshots, reports,
or gate verdicts.

---

### 2.1 Startup

Execute in order:

1. Preserve the user's original text verbatim as `GAME_IDEA`.
2. Select one absolute `<REPO_ROOT>/playground/<game-slug>/` as `GAME_DIR`.
   Use a filesystem-safe slug and avoid overwriting an existing game. Ask one
   focused question only when ambiguity would materially change the architecture.
3. Resolve the absolute `SKILL_DIR`.
4. Run:

   ```bash
   python3 <REPO_ROOT>/scripts/preflight_env.py --quick --auto-fix
   ```

   If safe repair leaves blockers, stop and report them. Run the broader `--fix`
   only with user approval.
5. Call `mcp__gca__init_workspace` with `path=<GAME_DIR>, mode="create"`.
   Require a successful response whose `workspace.mode` is exactly `create`;
   this acquires the workspace lease before any MCP probe or workspace write.
   Recovery of an existing run uses `mode="resume"`; never infer intent. Stop on
   lease contention or any response/binding mismatch.
6. Call `mcp__gca__preflight_check` with `game_dir=<GAME_DIR>` and stop on
   unresolved blockers.
7. Read `references/routing/genres.md` and `references/quality-profiles.md` once
   and derive:
   - `GENRE`
   - the six-axis `DESIGN_AXES`
   - `INTERACTION_PROFILE = DESIGN_AXES.physics`
   - `QUALITY_PROFILE`
   - `LEVEL_MODEL`
   - `LEVEL_DESIGN_REQUIRED`, exactly `LEVEL_MODEL == "authored"`
8. Before writing `PROMPT.md` or any canonical build input, persist a recoverable
   bootstrap transaction:

   ```text
   mcp__gca__write_pipeline_state({
     game_dir: <GAME_DIR>,
     phase: "startup",
     current_step: "bootstrap",
     status: "in_progress",
     genre: <GENRE>,
     design_axes: <DESIGN_AXES>,
     level_design_required: <LEVEL_DESIGN_REQUIRED>,
     interaction_profile: <INTERACTION_PROFILE>,
     quality_profile: <QUALITY_PROFILE>,
     level_model: <LEVEL_MODEL>,
     next_action: "Archive PROMPT.md"
   })
   ```

   If this first transaction is interrupted after its pending journal is durable,
   a fresh-session `mode="resume"` must roll it forward through
   "10. Resume After Interruption".
9. Archive the original prompt as `<GAME_DIR>/PROMPT.md`; set its absolute path
   as `PROMPT_PATH`. Do not pre-seed producer-owned source or asset trees.
   Do **not** create `levels/` here; only an authored level-design phase owns it.
10. Persist Startup completion only after the prompt exists:

   ```text
   mcp__gca__write_pipeline_state({
     game_dir: <GAME_DIR>,
     phase: "startup",
     current_step: "design",
     status: "completed",
     next_action: "Dispatch design-agent"
   })
   ```

11. Log Startup through:

   ```bash
   bash <REPO_ROOT>/.claude/hooks/audit-log.sh <GAME_DIR> pipeline_start "<summary>"
   ```

Do not start a server or browser test before scaffold creates `index.html`.

---

## 3. Startup Routing Metadata

This is not a subagent phase. Store each derived value once and pass it verbatim;
never re-route from concept imagery, directory existence, or an agent's guess.

---

## 4. Phase 1 — Design

Dispatch `design-agent` with every field in `dispatch-contract.md §2.2`, including
the original `GAME_IDEA` and archived `PROMPT_PATH`.

After return:

1. Validate `<GAME_DIR>/GAME_DESIGN.md` and its profile-required design
   artifacts. The GDD is written in parts against a `<!-- GDD:MORE -->`
   sentinel, so a file still holding one was truncated mid-write however the
   agent reported it; treat that as a non-`done` return. Require at least one
   concept mockup unless every interactive scene is marked exactly
   `waived:concept-generation-unavailable`. A pure transition may use only
   `waived:pure-transition`. Either waiver must retain complete mechanics,
   asset, normalized-layout, and UI contracts.
2. Set `GDD_PATH` to that absolute path.
3. Extract the ordered scene list into `SCENE_LIST`.
4. Treat GDD asset rows as design/source intent; the scaffolded manifest will own
   final runtime keys, paths, shipped dimensions, and metadata sidecars.
5. Persist design completion and route to §5 when authored levels are required,
   otherwise §6.

Do not repair an incomplete GDD yourself. Handle a non-`done` return according to
the dispatch contract. Preserve concept-generation warnings as warnings; do not
promote them to blocking `known_issues`. Reject a zero-mockup `done` return when
any interactive scene lacks the exact bounded-failure waiver.

---

## 5. Phase 1.5 — Level Design

Run this phase only when `LEVEL_MODEL=authored`. Dispatch `level-design-agent`
with `MODE=create` and the `dispatch-contract.md §2.3` fields, then validate the
current `levels/stage_*.json` against the canonical schema plus any
schema/GDD-required extension files.

Require the return's `level_files` to be a non-empty, duplicate-free list of
absolute paths directly under `<GAME_DIR>/levels/`, with every basename matching
`stage_<number>.json` and `level_files.length == stage_count`. Each entry must
exist, validate, and the list must describe every current stage in progression
order. Bind the validated list to `LEVEL_FILES`, then atomically persist
`has_levels:true` and the same dynamic list:

```text
mcp__gca__write_pipeline_state({
  game_dir: <GAME_DIR>,
  phase: "level_design",
  current_step: "scaffold",
  status: "completed",
  has_levels: true,
  level_files: <LEVEL_FILES>,
  next_action: "Dispatch scaffold-agent"
})
```

The state writer stores it as top-level `state.level_files` and
`resume.level_files`, preserves it when later writes omit the field, and rejects
an incomplete inventory. Pass it unchanged to every authored Scaffold and
Implement dispatch. Never synthesize filenames from `stage_count` or rediscover
them with Glob.

For `procedural`, `board`, or `none`, skip this phase, omit `LEVEL_FILES`, and
do not create level evidence. A stray `levels/` directory never changes routing.

---

## 6. Phase 2+3 — Scaffold → Implement ∥ Assets

### 6.1 Plan and scaffold

Read GDD §2 for source intent. Dispatch `scaffold-agent` with `MODE=scaffold`
and every field in `dispatch-contract.md §2.4`, including the validated
`LEVEL_FILES` for authored content, then wait.

Validate:

- `index.html`, `src/config.js`, `src/main.js`, scene stubs, and
  `assets/asset-manifest.json`;
- interaction, objective, layout, rubric, and test contracts under
  `.playtest/inputs/` and `.playtest/tests/`;
- either valid file-backed manifest entries or the explicit no-file contract.

Execute this order without collapsing the two state writes:

1. Refresh the canonical digest after every scaffold-owned build input exists:

   ```text
   mcp__gca__write_pipeline_state({
     game_dir: <GAME_DIR>,
     phase: "scaffold",
     current_step: "scaffold_gate",
     status: "in_progress",
     next_action: "Run scaffold_done gate"
   })
   ```

2. Run `mcp__gca__gate_check({gate:"scaffold_done"})`.
3. Only when that gate returns `blocked:false`, mark the phase complete:

   ```text
   mcp__gca__write_pipeline_state({
     game_dir: <GAME_DIR>,
     phase: "scaffold",
     current_step: "implement",
     status: "completed",
     next_action: "Start verified server and build branches"
   })
   ```

Never mark Scaffold completed before its gate. Use the manifest as the final
runtime key/path contract only after the pass. A later approved design change
uses `MODE=reproject` with non-empty `CHANGED` and optional `ISSUES`; remove only
the paths sealed by Scaffold via
`mcp__gca__delete_scaffold_owned_paths({receipt_id:<returned stale_receipt_id>})`,
then repeat digest refresh → gate → completion. Never delete an inferred,
unsealed, or broader path.

**Step 1b — Start the verified local server**

```bash
python3 <REPO_ROOT>/scripts/serve_game.py <GAME_DIR> --base 3000
```

Capture the printed `URL=http://127.0.0.1:<port>` and pass that exact URL to every
implement and verify dispatch.

Scaffold generates and registers smoke modules but does not execute browser tests
at this boundary. Browser testing starts only after integration has produced its
asset receipt and the verify agent invokes `run_verify_dag`. Its canonical
`run_tests` node runs the complete, unfiltered `.playtest/tests/` suite; smoke
modules are included in that suite.

If the manifest has generated-image entries, run
`mcp__gca__asset_pipeline_smoke_test` before dispatching any asset worker. Stop
when it reports blockers.

Partition only manifest entries with `sourceStrategy:"generated-image"`.
Dispatch supplied/licensed static entries separately with
`MODE=register-existing`; they are sealed with hash/dimension/bbox metadata but
never regenerated. Block supplied/licensed animated sprites unless a validated
sprite-ingestion route is explicitly available. Procedural/live/composition
sources remain runtime contracts and enter no file batch. Bitmap text is
unsupported until a manifest-backed font-atlas route exists.

### 6.2 Dispatch both branches

In one main-agent turn:

1. Start one background Workflow for Branch A and save its task identity.
2. Emit generated and existing-static Branch B calls together so independent
   batches run in parallel.

The Branch A Workflow uses `for...of` plus sequential `await agent()` calls and
returns `{scenes:[{scene,result}]}`. Each call carries every field from
`dispatch-contract.md §2.5`. Substitute real absolute values before dispatch;
do not leave helper names or placeholders unresolved. Every authored call
receives the same validated `LEVEL_FILES` list; no Scene may rediscover or
replace that inventory.

Branch B batches are:

- one batch per sprite entity; its worker produces the entity's single shared
  reference first, then fans out all declared actions together with disjoint
  action output directories;
- one backgrounds batch when applicable;
- one props/UI batch when applicable.

Pass each batch through `BATCH` using `dispatch-contract.md §2.6`. Effects follow
the selected profile and GDD strategy; projectile bodies remain entity assets.

### 6.3 Join and reduce

Collect all Branch B returns, then join the saved Branch A task. Retain partial
outputs and unresolved issues, but do not pass missing required outputs as
successful.

After all declared generated and non-generated sources are resolved:

1. Validate every required file and metadata sidecar.
2. For file-backed entries, call exactly once:

   ```text
   mcp__gca__finalize_asset_manifest({ game_dir: <GAME_DIR> })
   ```

3. Append `assets_generated`, then persist `assets_generated:true` together with
   the complete Branch A Scene summary
   `scenes:{implemented:[...],pending:[...]}`.
4. Run `mcp__gca__gate_check({gate:"step_done", step:3})`. This is the GCA
   Pipeline Step 3 completion gate, not a Phaser version selector.
5. Continue only when both branches and manifest reduction are complete and the
   Step 3 gate returns `blocked:false`.

For a valid no-file contract, skip the reducer but still record completion of the
asset/source phase. On interruption, resume only incomplete scenes or batches;
never regenerate validated owned outputs.

---

## 7. Phase 4 — Asset Integration

After §6's join and reduction, dispatch `asset-integration-agent` with
`MODE=integrate` and every field in `dispatch-contract.md §2.9`.

Normal integration is one full dispatch. Only after a confirmed timeout may the
contract's scoped continuation run sequentially:
`config` → `preloads` → `animations` → `visual` → `verify`.

Require the integration agent to seal
`.playtest/verify/assets/asset-integration-report.json` through
`mcp__gca__write_asset_integration_report`; never accept a hand-authored report.
Route blocking items by cause through §9.1. Then persist integration completion
with `write_pipeline_state` so its build digest equals the receipt before proceeding
to §8. The integration agent owns preload, animation, visual wiring, and its
MCP-owned receipt. Browser test execution belongs to the post-integration
verify-agent.

---

## 8. Phase 5-7 — First Verification

Before every `MODE=iterate` or `MODE=full` dispatch, including re-verification in
§9, use `mcp__gca__write_pipeline_state` to persist `visual_verified:false` for
the current build. Never carry a prior run's screenshot-review receipt into a
new epoch.

Dispatch `verify-agent` in `MODE=iterate` with all fields from
`dispatch-contract.md §2.7`, including `URL`, `QUALITY_PROFILE`, and
`LEVEL_MODEL`. The verifier's canonical first action is one
`mcp__gca__run_verify_dag` call; do not ask it to hand-sequence producers.

`MODE=iterate` produces current tests, screenshots, gameplay, visual/element,
physics when applicable, and layout evidence. It does not record or analyze
video.

After the verify-agent returns or its transport fails, check the durable epoch:

- **Finalized report exists:** proceed immediately to screenshot receipt (when
  actually reviewed), composite gate, and critic.
- **Active epoch after an interrupted transport:** confirm the prior invocation
  is terminal, then resume the same physical verify-agent/task with the exact
  active `verify_run_id`, mode, and URL. Its next `run_verify_dag` call reuses
  completed producer checkpoints and reruns only failed/interrupted nodes. Do
  not launch a new verifier or open a second epoch.
- **DAG finalizer itself failed:** call
  `mcp__gca__write_verify_report({ game_dir, mode, url, verify_run_id, status: "partial", issues: [{severity:"high", description:"verify DAG failed before finalization", root_cause:"<error details>"}] })`
  as the last-resort orchestrator seal.

Never wait for user input between verify failure and gate → critic. Failed and
running producer checkpoints are folded into the partial report; the composite
gate may block, but §9.3 still requires critic and assessment.

When the verify-agent returned a finalized current report and explicitly confirms
that it reviewed the current screenshots, persist that review receipt before the
composite gate:

```text
mcp__gca__write_pipeline_state({
  game_dir: <GAME_DIR>,
  phase: "verify",
  current_step: "iteration_gate",
  status: "in_progress",
  visual_verified: true,
  next_action: "Run iteration_ready"
})
```

`visual_verified` records completion of the current screenshot review; it is not
a visual PASS and never overrides visual/element/layout evidence. Do not set it
when the verifier failed before producing and reviewing current evidence.

Then call:

```text
mcp__gca__gate_check({
  gate: "iteration_ready",
  game_dir: <GAME_DIR>,
  verify_run_id: <VERIFY_RUN_ID returned by verify-agent>
})
```

For every completed verify pass (including partial/blocked reports — a finalized
report of any status counts as a completed pass):

1. append one `verify_run` audit event;
2. increment `verify_runs` by exactly one;
3. persist the latest run identity, report summary, and next action.

Set the pipeline-state field `first_pass_complete:true` only when
`iteration_ready` passes. This state field is distinct from the compatibility
`first_pass_complete` gate in `dispatch-contract.md §5`, which is a with-video
baseline and is not part of the normal §8 path. A configured warning-only VLM
outage is reported as an evidence limitation, not copied into blocking known
issues and not charged as a fix cycle. Real mismatches, stale or missing evidence,
verifier failures, and all other gate blockers go to §9.1.

---

## 9. Iteration Loop

The loop budget comes from `pipeline.max_iterations`; do not copy its current
value into this document. The authoritative blocker set is the full
`iteration_ready` gate output plus blocking critic findings. The critic's
`top_issue` changes ordering only.

### 9.1 Fix all blockers

Fix every blocker in the cycle:

| Cause | Owner |
|---|---|
| Wrong or missing design, asset key, target size, binding, or layout contract | `design-agent`, then `scaffold-agent MODE=reproject` before dependent work |
| Gameplay/code bug or missing Scene behavior | `implement-agent` for the owning Scene |
| Final preload, animation, display metadata, scaling, UI binding, or asset wiring | `asset-integration-agent` with structured `ISSUES` |
| Wrong, missing, placeholder, or low-quality generated art | `assets-agent` with only affected manifest entries |
| Authored level pacing, geometry, spawn, or boss pattern | `level-design-agent MODE=revise` with the complete structured `ISSUES` set |
| Missing or incorrect authored runtime Loader/adapter | `implement-agent` |
| Procedural/board content behavior | design contract owner or `implement-agent`, according to cause |
| Integration mismatch | asset owner when the file/metadata is wrong; `asset-integration-agent` when wiring is wrong |

When a visual/element/layout report drives an implement fix, pass the current
`VISUAL_EVIDENCE` path declared in the dispatch contract. Always pass the verified
`URL` to implement fixes. Numeric truth still comes from contracts, never visual
eyeballing.

Design changes run first because design-agent is the sole GDD writer. Reproject
affected scaffold contracts, then dispatch independent code and asset fix groups
in parallel only when their write sets do not overlap. Give each owner its
complete issue list. Never run `asset-integration-agent` beside an Implement or
Assets writer; it starts only after those producers join.

After every authored `level-design-agent MODE=revise` return, validate its
complete dynamic `level_files` inventory exactly as in §5, replace
`state.level_files` and `resume.level_files` through `write_pipeline_state`, and
rebind the result as `LEVEL_FILES` before dispatching any dependent Implement
fix. Never retain the pre-revise list and never persist only the changed stages.

After fix workers join:

1. rerun the manifest reducer when file-backed metadata changed;
2. write pipeline state without changing verify/iteration counters to seal the
   new `build_digest`;
3. when authored levels or implementation changed, rerun
   `gate_check({gate:"step_done", step:3})`;
4. only after that gate passes, rerun integration when assets, manifest, or
   runtime wiring changed.

Use `references/iteration-procedures.md` for evidence attachment, regression
tracking, visual-loop, stress, and refactor procedures.

### 9.2 Re-verify

After each fix set, dispatch `verify-agent MODE=iterate`, run
`iteration_ready`, and record the verify event/counter in the §8 order. Never
reuse a report from a prior build digest.

If transport fails, follow §8 checkpoint recovery: resume the same verifier and
active epoch, never a fresh physical launch. `run_verify_dag` normally finalizes
server-side despite the lost response; use direct `write_verify_report(partial)`
only when its finalizer demonstrably did not run.

### 9.3 Critic

Critic is mandatory after every completed verify pass — **regardless of the
gate result**. Gate BLOCKED does not skip or defer critic. The full iteration
sequence is always: verify → gate → critic → assessment (§9.4). Skipping
critic on a BLOCKED gate prevents `iteration_count` from advancing, which
deadlocks the pipeline.

Dispatch `critic-agent` with `dispatch-contract.md §2.8` fields.

For an iterate pass, set `VIDEO_ANALYSIS` to `none — MODE=iterate`; supply current
screenshots, play report, verify report, GDD, rubric, and profile. For a full
pass, supply current video analysis paths, or the explicit skip-video waiver
marker. When the gate is BLOCKED, include the gate's blocking issues summary in
the critic dispatch context so it can consider them in its assessment.

A critic PASS is necessary but not sufficient to ship. It must agree with a
passing composite gate for the same run identity.

### 9.4 Assess and confirm release

After the current iterate gate and critic, **always** complete the assessment
cycle:

1. Append `iteration_complete` audit event and increment `iteration_count` by
   one. This happens after every gate+critic pair, regardless of their verdicts.
2. If gate BLOCKED or critic REJECT, first compare the persisted
   `iteration_budget` with `pipeline.max_iterations`. At exhaustion, go directly
   to §9.5; do not dispatch another fixer or verifier. Otherwise return to §9.1,
   then §9.2 and §9.3.
3. When the iterate gate PASS, critic PASS, and configured minimum iteration
   requirement are all green, dispatch `verify-agent MODE=full`.
4. Record that full pass as another `verify_run`, rerun critic with its current
   release evidence, then call `ready_to_ship`.
5. Ship readiness requires both final critic PASS and `ready_to_ship` PASS for
   the current `pipeline_run_id`, `build_digest`, and `verify_run_id`.

Video is release-only, not part of normal iterate rounds. Usually one full pass
is enough. If full-only evidence finds a blocker, or any repair changes the
build, the old full evidence is stale: fix the issue and produce a new full pass.
Non-video changes must return through `MODE=iterate` first; a video/performance-
only repair may proceed directly to another full pass.

`verify.skip_video:true` does not silently weaken release. It requires the
explicit approved `video-performance` waiver and a fresh full-mode report.

### 9.5 Exhaustion and blockers

Stop automatic iteration at the configured cap. Report every unresolved gate and
the latest critic evidence, then ask whether to continue or abandon. A user may
accept an unfinished build, but it must be reported as unresolved/aborted—not
shipped—unless the current release gate passes.

The cap is runtime-enforced: `audit-log.sh` refuses a new `iteration_complete`
event after exhaustion, `begin_verify_run` refuses a new iterate epoch,
`write_pipeline_state` rejects an increment beyond the cap, and the Stop hook
seals `.playtest/stop-hook-unresolved.json` instead of forcing more cycles. Never
bypass those guards or reinterpret the Stop-hook exit as a gate pass.

A blocked fixer, failed critic, definitive auth/quota/permission error, or missing
user decision pauses the pipeline. Do not force-continue or fabricate success.

### 9.6 Cleanup

On every exit path, stop the verified server:

```bash
python3 <REPO_ROOT>/scripts/serve_game.py <GAME_DIR> --stop
```

Then report either ready-to-ship success or the exact unresolved state.

---

## 10. Resume After Interruption

Do not restart from Startup unless no recoverable game exists.

### 10.1 Recovery procedure

1. Select exactly one active `GAME_DIR`; ask if multiple games are plausible.
   Do not inspect that game's state files while selecting by path/name.
2. In a fresh MCP session, the first workspace action must be
   `mcp__gca__init_workspace({path:<GAME_DIR>, mode:"resume"})`. Require success
   and verify the response reports `workspace.mode:"resume"`. This call acquires
   the exclusive lease and rolls forward a valid pending state transaction.
   Stop on lease contention, an invalid journal, or any response/binding
   mismatch. Do not read or migrate state before it succeeds.
3. Read `.playtest/state.json`, `resume.json`, `audit.jsonl`, and any
   `.playtest/verify/current-run.json` together.
4. Reconcile breadcrumbs with current artifacts and any saved/in-flight task
   identity. Never duplicate a task that may still own the action.
5. Validate candidate completion:

   | Phase | Required current evidence |
   |---|---|
   | Design | complete GDD plus internally consistent mockup/crop/layout contracts |
   | Level | valid authored level set only when `LEVEL_MODEL=authored` |
   | Scaffold | non-stub runtime, manifest, inputs, rubric, and test manifest |
   | Build | every required scene/source plus current metadata and provenance |
   | Integration | no fallback wiring and a report fresh for code + manifest |
   | Verify | reports and gates matching the latest run IDs and build digest |
   | Critic/release | critic and release gate matching the same full verify run |

6. Reconstruct immutable routing from identity-matched state and confirm it
   against the GDD. For authored state with `has_levels:true`, recover the exact
   `LEVEL_FILES` from matching top-level `state.level_files` and
   `resume.level_files`. If a legacy state has no inventory, rerun Level Design
   with `MODE=revise` and one migration-only structured issue requiring no
   authored content change; require `files_changed:[]`, validate its complete
   returned `level_files`, and persist the list before any downstream dispatch.
   If validation reveals a real level defect, handle it as a normal revise
   instead of claiming a migration-only result. Do not let Scaffold or Implement
   discover the inventory. Do not guess routing again from genre or directories.
7. Continue from the first phase whose evidence is missing, stale, malformed,
   partial, cross-run, or contract-incompatible.
8. Restore or revalidate the exact server `URL` before any browser-backed agent.
9. If verification was interrupted with `current-run.json` still `active`,
   confirm no prior verifier remains, resume the same physical verify task, then
   call `run_verify_dag` with that run's exact `verify_run_id`, mode, and URL
   (or matching `resume_active:true`). Reuse valid completed checkpoints, rerun
   failed/interrupted producers, and finalize the same epoch. Stop instead of
   adopting any identity mismatch.

### 10.2 Smallest safe redo unit

- Branch A: only the first incomplete scene onward, preserving validated scenes.
- Branch B: only missing/invalid owned batches or keys.
- Integration: only the contract-authorized scope after a confirmed timeout;
  otherwise rerun full integration when its inputs changed.
- Verification: resume a matching interrupted active run; always start a fresh
  run after a build change.
- Iteration: resume the first missing step among fix → iterate verify → critic →
  assessment, using matching run identities.

Missing or corrupt breadcrumbs require conservative reconstruction from validated
artifacts, not optimistic completion claims.

---

## 11. Non-Negotiable Rules

- Main agent orchestrates; producers create. Dispatches use the contract's
  absolute scoped paths and continuation rules.
- Browser evidence is verify-agent-owned and must be bound to an active verify
  epoch; the main agent makes no browser test calls.
- Material actions require producer audit events or
  `<REPO_ROOT>/.claude/hooks/audit-log.sh`; guarded events precede counters.
- **Critic is mandatory**, gates remain authoritative, MCP is never hot-restarted,
  and scaffold/implementation remain Phaser 4.1+ only.

---

## 12. File Map

```text
game-pipeline/                         # skill package
├── SKILL.md                           # orchestration authority
├── dispatch-contract.md               # dispatch/interface authority
└── references/
    ├── quality-profiles.md             # quality/capability authority
    ├── iteration-procedures.md         # detailed fix-loop procedures
    ├── routing/
    │   └── genres.md                   # Startup routing authority
    ├── design/                         # GDD, mechanics, concept, asset plan
    ├── level-design/                   # schemas, prefabs, examples
    ├── scaffold/
    │   └── templates.md
    ├── implement/                      # genre guides and runtime helpers
    ├── assets/                         # generation and post-processing
    ├── verify/                         # tests, rubric, play, failure guidance
    └── phaser/                         # Phaser 4 reference corpus

External Claude Code agent registry:
<REPO_ROOT>/.claude/agents/
└── {design,level-design,scaffold,implement,asset-integration,assets,verify,critic}-agent.md
```

The map is navigational, not an exhaustive inventory. Topic directories may add
references without requiring this tree to enumerate every file.

---

## 13. High-Risk Interfaces

### Asset/implement interface errors

- Runtime keys, paths, and metadata come from the manifest.
- Sprite actions require a same-basename sheet JSON sidecar. Legacy combined
  entries are valid only when the manifest explicitly declares
  `<entity>-spritesheet.json` or `spritesheet-meta.json`.
- Missing files or sidecars block integration; do not substitute GIFs or guessed
  frame dimensions.

### Phaser engine errors

- Use Phaser 4 only. Do not copy Phaser 3 signatures or removed APIs into a
  dispatch prompt; the scaffold/implement agents and Phaser reference corpus own
  the exact API guidance.
