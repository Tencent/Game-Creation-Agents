# Dispatch Contract

**Reader:** the main agent executing `SKILL.md`.

This file is the hard interface for subagent invocation: required/optional fields,
minimal returns, and continuation policy. Phase order, concurrency, and gates live
in `SKILL.md`; phase-internal craft lives in each agent spec.

## 1. Invocation

Dispatch registered agents with:

```text
Agent(subagent_type="<agent-name>", description="...", prompt="KEY: value\n...")
```

Prompt field order is irrelevant. Every path is absolute. `REF_DIR` narrows
reference context; agent tool allowlists and the boundary hook enforce actual
capabilities.

| Use | `subagent_type` | Fields |
|---|---|---|
| Design | `design-agent` | §2.2 |
| Authored levels | `level-design-agent` | §2.3 |
| Scaffold/reproject | `scaffold-agent` | §2.4 |
| Scene implementation | `implement-agent` | §2.5 |
| Asset generation/registration | `assets-agent` | §2.6 |
| Verification | `verify-agent` | §2.7 |
| Independent review | `critic-agent` | §2.8 |
| Asset integration | `asset-integration-agent` | §2.9 |

### 1.1 Shared field dictionary

| Field | Meaning |
|---|---|
| `GAME_DIR` | Absolute game-project root |
| `GDD_PATH` | Absolute `<GAME_DIR>/GAME_DESIGN.md` |
| `GENRE` | Startup-selected genre; downstream agents never re-route it |
| `QUALITY_PROFILE` | `art_directed`, `responsive`, `pixel_art`, or `minimal` |
| `LEVEL_MODEL` | `authored`, `procedural`, `board`, or `none` |
| `INTERACTION_PROFILE` | Startup-selected physics/interaction route |
| `URL` | Exact local URL verified by `scripts/serve_game.py` |
| `REF_DIR` | Absolute phase-specific reference directory |
| `QUALITY_PROFILE_PATH` | Absolute `references/quality-profiles.md` |
| `PHASER_REF_DIR` | Absolute `references/phaser` directory |
| `CONFIG_PATH` | Absolute repository `gca.config.yaml` |
| `DISPLAY_UTILS_PATH` | Absolute canonical `references/implement/runtime/display-utils.js` |
| `LEVEL_FILES` | Complete ordered JSON array of absolute current `levels/stage_*.json` paths; required only for `LEVEL_MODEL=authored`; build-time read paths, never browser URLs |

---

## 2. Phase Interfaces

### 2.1 Startup routing (no subagent)

Read `references/routing/genres.md` and `references/quality-profiles.md`. Produce
`GENRE`, `DESIGN_AXES`, `INTERACTION_PROFILE`, `QUALITY_PROFILE`, and
`LEVEL_MODEL`; set `LEVEL_DESIGN_REQUIRED` exactly when
`LEVEL_MODEL=authored`.

### 2.2 `design-agent`

| Contract | Fields |
|---|---|
| Required | `GAME_IDEA`, `GENRE`, `DESIGN_AXES`, `INTERACTION_PROFILE`, `QUALITY_PROFILE`, `LEVEL_MODEL`, `GAME_DIR`, `PROMPT_PATH` |
| References | `REF_DIR=<SKILL_DIR>/references/design`, `FX_GUIDE_PATH=<SKILL_DIR>/references/implement/fx-guide.md`, `QUALITY_PROFILE_PATH`, `CONFIG_PATH` |
| Writes | `<GAME_DIR>/GAME_DESIGN.md` and multimodal evidence under `<GAME_DIR>/design/` |
| Returns | `status`, `gdd_path`, `sections`, `genre`, `scene_count`, `asset_count`, `quality_profile`, `level_model`, `concept_mockups`, `scene_coverage`, `ref_crops`, `warnings`, `issues`, `summary` |

### 2.3 `level-design-agent`

Dispatch only for `LEVEL_MODEL=authored`.

| Contract | Fields |
|---|---|
| Mode | `MODE=create` (default) or `MODE=revise` |
| Required | `GDD_PATH`, `GENRE`, `LEVEL_MODEL`, `GAME_DIR` |
| Conditional | Structured `ISSUES` for `MODE=revise` |
| References | `REF_DIR=<SKILL_DIR>/references/level-design` |
| Writes | `levels/stage_*.json` and schema/GDD-required extension files |
| Returns | `status`, `mode`, `levels_path`, `stage_count`, `level_files`, `has_boss`, `files_created`, `files_changed`, `extensions`, `warnings`, `issues`, `design_change_requests`, `summary` |

### 2.4 `scaffold-agent`

| Contract | Fields |
|---|---|
| Mode | `MODE=scaffold` (default) or `MODE=reproject` |
| Required | `GDD_PATH`, `GAME_DIR`, `CONFIG_PATH`, `QUALITY_PROFILE`, `LEVEL_MODEL` |
| Conditional | Non-empty `LEVEL_FILES` when `LEVEL_MODEL=authored`; omit for every other model |
| References | `SCAFFOLD_REF_DIR=<SKILL_DIR>/references/scaffold`, `PHASER_REF_DIR`, `DISPLAY_UTILS_PATH` |
| Reproject | non-empty `CHANGED`; optional structured `ISSUES` |
| Returns | `status`, `mode`, `summary`, `files_created`, `files_changed`, `stale_owned_paths`, `stale_receipt_id`, `contracts`, `warnings`, `issues`, `design_change_requests` |

Scaffold projects the asset manifest, interaction/objective/layout/rubric
contracts, deterministic tests, scene stubs, and applicable canonical helpers.
It consumes authored routing through the GDD and never executes browser tests.
For a non-empty `stale_owned_paths`, Scaffold writes the exact list to its fixed
authority `.playtest/inputs/scaffold-reproject.json` with a fresh
`reproject-<32 lowercase hex>` ID, seals it with
`mcp__gca__seal_scaffold_reproject({})`, and returns `stale_receipt_id`. The
orchestrator cannot supply or broaden seal paths; it consumes only that receipt with
`mcp__gca__delete_scaffold_owned_paths({receipt_id:<returned id>})`, then reruns
the Scaffold gate.

### 2.5 `implement-agent` — one scene

| Contract | Fields |
|---|---|
| Required | `SCENE_NAME`, `GDD_PATH`, `GAME_DIR`, `GENRE`, `INTERACTION_PROFILE`, `QUALITY_PROFILE`, `LEVEL_MODEL`, `URL` |
| Conditional | Non-empty `LEVEL_FILES` when `LEVEL_MODEL=authored`; omit for every other model |
| References | `REF_DIR=<SKILL_DIR>/references/implement`, `PHASER_REF_DIR`, `QUALITY_PROFILE_PATH` |
| Optional | `VISUAL_EVIDENCE=<GAME_DIR>/.playtest/verify_middle/visual/visual-diff-report.json` on visual/element/binding/layout fix rounds only |
| Returns | `status`, `scene`, `files_changed`, `tests`, `issues`, `needs_attention`, `design_change_requests`, optional `visual_review` |

`URL` carries the verified server identity for downstream continuity; it does
not authorize pre-epoch browser execution. Implement authors test modules but
does not run them.
`VISUAL_EVIDENCE` authorizes bounded inspection of the indexed rendered/reference
pair, but numeric truth still comes from contracts.

### 2.6 `assets-agent`

| Contract | Fields |
|---|---|
| Required | `GDD_PATH`, `GENRE`, `QUALITY_PROFILE`, `LEVEL_MODEL`, `GAME_DIR` |
| References | `REF_DIR=<SKILL_DIR>/references/assets`, `FX_GUIDE_PATH=<SKILL_DIR>/references/implement/fx-guide.md`, `QUALITY_PROFILE_PATH` |
| Mode | `MODE=generate` (default) or `MODE=register-existing` |
| Scope | `BATCH`: generated-image rows for `generate`; supplied/licensed static rows for `register-existing` |
| Returns | `status`, `batch`, `assets_generated`, `assets_registered`, `sprites`, `maps`, `props`, `ui`, `metadata_sidecars`, `design_change_requests`, `issues` |

Parallel workers modify only owned assets and sidecars. They never write the GDD
or manifest. After all batches join, the main agent runs
`finalize_asset_manifest` once. Supplied/licensed animated sprites block unless
a validated ingestion contract exists.

### 2.7 `verify-agent`

| Contract | Fields |
|---|---|
| Required | `GAME_DIR`, `URL`, `MODE`, `QUALITY_PROFILE`, `LEVEL_MODEL` |
| References | `REF_DIR=<SKILL_DIR>/references/verify` |
| Modes | `iterate`, `full`, or `gate` |
| Conditional | `GATE` for `MODE=gate`; `STEP` when `GATE=step_done`; `VERIFY_RUN_ID` for every verify/final gate and for `step_done` Phase 7 |
| Optional | `CHANGED` paths for a diff-aware diagnostic run only; its scoped result never substitutes for the canonical unfiltered iterate/full test run |
| Returns | `status`, `mode`, `verify_run_id`, producer `checkpoints`, `tests`, evidence paths, `verify_report`, `pre_critic_gate`, optional `gate_result`, `issues` |

Every canonical `iterate` or `full` pass uses one `run_verify_dag` call. It
begins/resumes the epoch, writes per-producer checkpoints, executes disjoint
branches concurrently, fan-ins the aggregate report in `finally`, and validates
that report before returning. All evidence carries matching `pipeline_run_id`,
`build_digest`, and `verify_run_id`. `iterate` omits video. `full` is
release-only; rerun full only when a full-only blocker or changed build makes
prior evidence stale.
When `CHANGED` is supplied, verify-agent may use it for a separate fast diagnostic,
but it must still run the unfiltered canonical `.playtest/tests/` suite before
finalizing the pass; scoped results cannot satisfy the composite gates.
After an interruption, resume the same physical verify-agent/task after
confirming its prior call stopped. Call `run_verify_dag` with the exact active
`verify_run_id`, mode, and URL (or matching `resume_active:true`); it must reuse
only identity/input/artifact-valid completed checkpoints and preserve the same
epoch. Starting a fresh verifier/epoch is forbidden.

**Mandatory finalization:** `run_verify_dag` calls `write_verify_report` in a
server-side `finally` block even when individual producers or the agent
transport fail. Producer failures are checkpointed issues and do not cancel
sibling nodes. The orchestrator treats any finalized report (`done`, `partial`,
or `blocked`) as a completed pass eligible for composite gate → critic →
assessment without waiting for the user.

### 2.8 `critic-agent`

| Contract | Fields |
|---|---|
| Required | `GAME_DIR`, `SCREENSHOTS`, `VIDEO_ANALYSIS`, `PLAY_REPORT`, `VERIFY_REPORT`, `GDD_PATH`, `RUBRIC_PATH`, `QUALITY_PROFILE` |
| No-video marker | `none — MODE=iterate`, or `none — verify.skip_video` for an approved waived full pass |
| Returns | `status`, `verdict`, `top_issue`, `issue_category`, `scores` |

Critic receives no `REF_DIR`; it reads only the listed evidence. Its report must
copy the verify run identity.

### 2.9 `asset-integration-agent`

| Contract | Fields |
|---|---|
| Required | `MODE=integrate`, `GDD_PATH`, `GAME_DIR`, `GENRE`, `INTERACTION_PROFILE`, `QUALITY_PROFILE`, `LEVEL_MODEL`, `URL` |
| References | `REF_DIR=<SKILL_DIR>/references/implement`, `PHASER_REF_DIR`, `QUALITY_PROFILE_PATH`, `DISPLAY_UTILS_PATH` |
| Optional | `SCOPE=config`, `preloads`, `animations`, `visual`, or `verify`; structured `ISSUES` for a new semantic-remediation dispatch |
| Returns | `status`, `mode`, `scope`, `summary`, `files_changed`, `assets_wired`, `display_params`, `asset_integration_report`, `tests`, `warnings`, `issues`, `design_change_requests` |

Integration seals its receipt before any verify epoch opens. Its `tests` return
field records `execution:"deferred_to_verify"`; only verify-agent runs
`run_tests`.

Integration consumes manifest-declared runtime paths and metadata, including
canonical `*-sheet.json` and explicitly declared legacy
`<entity>-spritesheet.json` or `spritesheet-meta.json`.

Dispatch only after all Implement and Assets work has joined and the manifest is
finalized. Run one full integration first. Only after it is confirmed terminal
after timeout may scoped continuation run sequentially:
`config` → `preloads` → `animations` → `visual` → `verify`.

---

## 3. Retry and Continuation

Internal retries belong to the producer/MCP and use configured bounds. An
unknown task state means wait, not retry. Scoped continuation resumes only
missing work from a confirmed terminal partial/timeout; semantic remediation is
a new pipeline step after its cause changes.

| Phase | Rule |
|---|---|
| Design / Level Design / Scaffold | No automatic retry; design converts bounded concept-generation failure to its documented warning fallback (complete layout/mechanics without the image), while other incomplete evidence is reported |
| Implement | No automatic scene retry; partial issues enter the fix loop, while resume targets only incomplete scenes |
| Assets | Internal retries are bounded; terminal partials may continue with only missing keys |
| Asset Integration | Full dispatch first; §2.9 scopes are timeout continuation only |
| Verify | Resume the same active epoch/task from checkpoints after a confirmed interruption; never restart completed nodes or open a fresh epoch for transport failure. Start a new run only after build/cause changes or normal finalization |
| Critic | No automatic retry; failed judgment blocks assessment |

---

## 4. Return and Error Envelope

Every return includes `status`; warnings are non-blocking and issues describe
incomplete or blocked work. An agent supports the applicable subset of:

- `done`: validate outputs, persist completion, continue;
- `partial`: retain valid outputs, record all issues, continue only when safe;
- `blocked`: report the concrete `issues` and wait.

The main agent never force-continues, rewrites a return, or treats an in-flight
timeout as terminal.

---

## 5. Gate Interface

The main agent may call `mcp__gca__gate_check` directly; `verify-agent MODE=gate`
uses the same interface.

| Gate | Use |
|---|---|
| `scaffold_done` | Scaffold artifact and contract completion before build branches |
| `iteration_ready` | Main no-video baseline and every fix round |
| `ready_to_ship` | Current MODE=full release evidence; approved video waiver may remove video-only requirements |
| `play_quality` | Standalone play-agent quality diagnosis |
| `first_pass_complete` | Compatibility with-video baseline gate; distinct from the pipeline-state field of the same name and not the normal §8 path |
| `step_done --step N` | Cumulative diagnostic check for current top-level phases 2–7 |

Every `gate_check` response returns `blocked` plus a structured `failures[]`
(each record: `check` + `axis`/`agent` routing hints + `message`); the stderr
`BLOCKED:` lines remain the compat mirror. Group blockers by `agent` to route
parallel fixers.

`step_done` uses the current top-level pipeline numbering: phase 2 checks the
complete Scaffold boundary; phase 3 adds Scene implementation and asset/source
completion; phase 4 adds asset integration, measured entity/UI display-ledger
coverage, and sprite slot-fit receipts; phase 5 is the verification entry
boundary and adds no new predicate; phase 6 adds visual review; phase 7 adds
full verification evidence (`full` or approved `full-novideo`), playthrough,
and performance. The checks are cumulative. Phase 1 Design is validated by its
producer contract and has no `step_done` call. Phase 7 requires the matching
finalized `VERIFY_RUN_ID`.

Each successful or blocked `step_done` call returns its validated `step` and
writes `.playtest/verify/gate-results/step_done-<N>.json`, so one phase cannot
overwrite another phase's diagnostic receipt.

For fast, targeted mid-build feedback, the deterministic contract gates are also
exposed as advisory, non-epoch diagnostic tools — `mcp__gca__check_ui_contract`,
`mcp__gca__check_td_contract`, `mcp__gca__check_interaction_contract`. These are
NOT release authorities: the composite release decision stays
`gate_check({gate:"ready_to_ship", verify_run_id:<VERIFY_RUN_ID>})`, and the stop
hook trusts only
`gate-results/ready_to_ship.json`.
