# gca-mcp

**Agent-native MCP server for the Game Creative Agent (GCA) pipeline.**
Bundles every per-skill executable inside `src/gca_mcp/runners/` and exposes
them as MCP tools, alongside the API-calling adapters (gpt-image-2,
Seedance 2.0, Doubao 2.0 video understanding). Skills no longer ship
their own scripts — agents only see `mcp__gca__*` tool calls.

> Heavily inspired by `video-creation-mcp` — same registry / dispatcher /
> error-code architecture, adapted for game asset + verification workflows.

Every declared tool input schema is compiled at registration and enforced by the
dispatcher before workspace acquisition or handler execution.

---

## Quick Start

```bash
cd gca-mcp
uv sync
```

### Run

**Lazy mode** (recommended — agent picks the game folder per session):

```bash
uv run gca-mcp \
  --repo-root /path/to/gca
```

The agent then calls `init_workspace(path="playground/my-game", mode="create")` before
any workspace-gated tool. The response includes the bound `workspace.mode`.
Reopening is idempotent only for the same path and mode; changing mode or opening
the same game from a second process fails closed.

**Eager mode** — bind a workspace at launch:

```bash
uv run gca-mcp \
  --repo-root /path/to/gca \
  --workspace-root ./playground/my-game \
  --workspace-mode resume
```

Every create/resume binding holds an exclusive `fcntl` lease until
`close_workspace` or process exit. Resume acquires that lease before reading
state and rolls forward a valid `.playtest/state-transaction.pending.json` under
the state lock; malformed journals are rejected rather than ignored.

### Claude Code / Desktop

The repo ships an `.mcp.json` at the project root. With Claude Code:

```bash
cd /path/to/gca
claude
# the gca server is registered automatically
```

For Claude Desktop, copy the `mcpServers.gca` block from `.mcp.json`
into `~/Library/Application Support/Claude/claude_desktop_config.json`.

---

## Tools

The registry currently exports **55 tools**. Call `list_tools_summary` for the
live server list; the tables below document the same registry by concern.

### Lifecycle (9)

| Tool | Purpose |
|---|---|
| `ping` | Health check; returns workspace + credential status. |
| `init_workspace` | Pin one game directory by inode and take the repository-wide cross-process lease with explicit `create`/`resume` intent; returns the bound mode and recovers a valid pending state transaction on resume. |
| `close_workspace` | Detach the current workspace and release its process lease. |
| `list_tools_summary` | Return one-line summaries for every registered tool. |
| `preflight_check({game_dir?, check_rmbg?})` | Startup readiness check: MCP, credentials, RMBG, Playwright, ffmpeg, filesystem access. |
| `asset_pipeline_smoke_test({game_dir?, submit_seedance?})` | Pre-asset smoke: inline image encoding, RMBG probe, Seedance readiness/optional submit. |
| `seal_scaffold_reproject({})` | Read the fixed Scaffold-owned declaration through pinned no-follow I/O, then bind its fresh reproject ID and exact stale list to file/run/build identity; replay is rejected. |
| `delete_scaffold_owned_paths({receipt_id})` | Consume one sealed Scaffold receipt and delete only its unchanged obsolete scene/helper/test files. |
| `write_pipeline_state({game_dir?, phase, status, quality_profile?, level_model?, has_levels?, level_files?, next_action?})` | Atomically journal state/resume with run identity, canonical build digest, and guarded-counter evidence. Authored `level_files` is the complete dynamic absolute stage inventory and is persisted in both state and resume; it has no fixed cardinality. New runs and legacy routing migrations require explicit routing metadata. Write Startup `in_progress` before `PROMPT.md`/build inputs, and refresh state after scaffold writes before `scaffold_done`. |

### Asset Generation and Validation (20)

| Tool | Purpose |
|---|---|
| `generate_image` | Text-to-image via gpt-image-2. |
| `edit_image` | Reference-based image editing for crop-seeded assets and locked first frames. |
| `generate_seedance_video` | Seedance i2v/t2v through ARK native submit/poll; local frames are inlined, so no object storage is required. |
| `extend_background` | Canonical generated-static outpainting: preflight source collision rejection across every runtime/sidecar/review output, source-right overlap + full-width blank region, rollback-safe same-basename composite/full-review artifacts, restored overlap pixels, exact source-sized runtime PNG, and source/lineage/review-hash-bound `.asset.json`. |
| `build_i2v_prompt` | Add the validated framing, background, camera, and effects constraints to an animation prompt. |
| `rmbg2_video_matting` | Matte every video frame and emit transparent frame/video outputs. |
| `validate_asset_video` | VLM framing and scale-lock validation for generated animation videos. |
| `produce_validated_video` | Retry Seedance generation and matting serially until a take passes; each candidate's independent framing/content VLM checks run with at most two workers. |
| `validate_image` | VLM validation for a sprite first frame before i2v. |
| `validate_asset_slot` | Validate one exact manifest runtime artifact or a full `items` batch against GDD slots. Batch mode fans out one worker per item and reuses schema-v3 passed receipts bound to the artifact plus the selected slot section/description/contract/rubric/policy; unrelated GDD prose or sibling manifest entries do not trigger another Doubao call, while an old description copied into a changelog cannot keep a receipt fresh. Sheets use an internal frame-zero crop while the receipt binds the complete artifact. |
| `produce_validated_first_frame` | Retry first-frame generation with validator feedback. |
| `produce_validated_sprite_action` | End-to-end validated first-frame, animation, matting, and transparent-frame workflow; finish runtime output with `pack_spritesheet`. |
| `analyze_sprite_metadata` | Measure alpha content bounds and infer sprite facing. |
| `pack_spritesheet` | Assemble transparent frames into a Phaser-ready PNG and metadata sidecar. |
| `register_existing_static_asset` | Preserve supplied/licensed PNG pixels and atomically seal provenance, source strategy, hash, decoded PNG format, and dimensions. |
| `finalize_asset_manifest` | Active-workspace single-writer reducer that reopens every static PNG, verifies producer/hash/measured-dimension/provenance/reference/UI-sizing/source-contract and bbox sidecars, checks actual profile sizing for generated and supplied/licensed UI, binds generated `assetType` to manifest category (UI aliases map to `ui`), and accepts sealed outpainted background rows before atomically merging measured fields without rewriting authored sizing. |
| `content_bbox` | Measure opaque bounds for static collision/display metadata. |
| `trim_transparent` | Remove excess transparent margins from a static asset. |
| `scale_uniform` | Resize an RGBA asset to its authored in-game size without changing aspect ratio. |
| `produce_static_asset` | Recommended atomic PNG pipeline with canonical asset types: generate/edit → optional matte/trim/profile sizing/bbox → one pinned multi-file publication with metadata and auxiliaries. Crop edits require a local workspace `assets/**` or `design/**` reference and seal its path/hash. Every generated UI seals one `qualityProfile` + canonical `sizingContract`: one target axis (`art_directed`), exact nearest-neighbor `base_size` (`pixel_art`), or required `max_box` for all responsive/minimal UI including decorative art. |

### Concept Grounding (3)

| Tool | Purpose |
|---|---|
| `fit_image_to_resolution` | Fit a concept mockup to the exact game resolution; rejected for runtime assets. |
| `detect_layout` | Detect normalized boxes, roles, anchors, and layout kinds in a concept mockup; publishes only canonical `design/**/*.detect.json` evidence. |
| `ground_crop` | Crop detected concept elements into key-stable references owned exactly at `design/crops/<scene-or-screen>`. |

### Post-Processing and Matting (3)

| Tool | Purpose |
|---|---|
| `postprocess({command, …})` | CPU-only asset operations: `compose-preview` or `make-seamless`. Does not remove backgrounds or split grids. |
| `rmbg2_matting` | Explicit single-image/background-removal path (RMBG-2.0, remote or local); returns a soft-alpha RGBA PNG. |
| `color_key_matting` | Pure-computation `#FF00FF` / `#00F000` color-key path, and the default `assets.matting.backend: color_key` — no model, no API, nothing downloaded. Opting into `assets.matting.backend: auto` probes RMBG once per process route and remembers the result; network, missing-route HTTP 404, or HTTP 5xx outages then select sticky static/video color-key fallback. |

### Video Understanding and Recording (3)

| Tool | Purpose |
|---|---|
| `analyze_video({video, verify_run_id, output?, split?, ...})` | Analyze only a current recording whose adjacent video receipt matches its SHA, timeline SHA, URL/token, and verify identity. |
| `record_with_state({url, verify_run_id, output, duration?, interval?, script?})` | Runtime-token-verified recording + state/performance sampling. The canonical per-scenario output also includes an atomic `video-receipt.json`. |
| `get_state_extractor` | Return the bundled Phaser state-extractor path and source for generated tests. |

### Verification (16)

| Tool | Purpose |
|---|---|
| `write_asset_integration_report` | Derive and seal manifest/file/runtime wiring evidence after integration. |
| `run_verify_dag({game_dir?, mode, url, verify_run_id?, ...})` | Canonical iterate/full entry point: single-flight begin/resume, per-producer checkpoints, bounded parallel tests/play/visual/layout/runtime nodes, isolated recording, fan-in report finalization, and idempotent finalized replay. |
| `begin_verify_run({game_dir?, mode, url, resume_active?})` | Low-level epoch primitive used by the DAG; direct use is limited to diagnostics/recovery. |
| `gate_check({gate, game_dir?, verify_run_id?, step?})` | Run a pipeline gate. Verify/final gates read the explicit active/finalized epoch identity. Returns `blocked` plus a structured `failures[]` (each with its originating `check` + `axis`/`agent` routing hints); the stderr `BLOCKED:` lines remain the compat channel. |
| `check_ui_contract({game_dir?})` | Advisory diagnostic: UI contract gate standalone (proportion + art parity + binding + static-text-as-asset). Non-epoch; NOT a release authority. |
| `check_td_contract({game_dir?})` | Advisory diagnostic: tower-defence structural contract standalone (auto-skips non-TD genres). NOT a release authority. |
| `check_interaction_contract({game_dir?})` | Advisory diagnostic: interaction contract standalone (layers/matrix/world bounds, hit-target fit, tunnel, prefab coverage). NOT a release authority. |
| `run_tests({url, verify_run_id, tests?, solo?, filter?, screenshot_each?, record?, ...})` | Runtime-token-verified Playwright runner. Only an unfiltered canonical `.playtest/tests/` run updates release-gated `test-results.json`. |
| `write_verify_report({game_dir?, mode, url, verify_run_id, status?, issues?})` | Atomically aggregate current receipts and finalize the epoch; later producers are rejected. |
| `play_game` | Closed-loop gameplay agent that validates the strict executable objectives DSL before browser/provider work, then reports coverage/regressions. |
| `materialize_gameplay_alignment` | Project current play health/objective results into identity-bound Axis 2 evidence. |
| `materialize_physics_alignment` | Project deterministic interaction body/art/world checks into Axis 4 evidence, or explicit N/A for a non-physics profile. |
| `visual_diff` | Whole-screen and per-element comparison against concept mockups and crops. A failed pinned Doubao request returns `status:"unavailable"` and warns without blocking by default. |
| `capture_layout` | Capture rendered object bounds for deterministic layout alignment. |
| `capture_runtime_assets({url, verify_run_id})` | Atomically capture loaded, animation-frame, and reachable-scene display-list texture keys with manifest/URL/token identity. |
| `materialize_layout_alignment` | Non-blocking Axis 5 evaluator: reads captured bounds, writes `alignment/layout/alignment.json`, and returns layout issues for the verify report. |

For `gate:"step_done"`, `step` is required and identifies the current cumulative
top-level pipeline phase, from 2 through 7: Scaffold, implementation plus
asset/source completion, integration with measured entity/UI ledgers and sprite
slot-fit receipts, verification entry, visual review, and full verification
(`full` or approved `full-novideo`). Phase 5 adds no new predicate beyond phase
4. Phase 1 Design is validated by its producer contract and is not a
`step_done` target. Phase 7 also requires the matching finalized
`verify_run_id`. Each call records the normalized integer phase in its response
and writes `.playtest/verify/gate-results/step_done-<N>.json`.

### Path and Layout (1)

| Tool | Purpose |
|---|---|
| `render_layout_mask` | Render a tower-defence path-corridor hint from authoritative `map-layout.json`. |

---

## Architecture

```
src/gca_mcp/
├── cli.py             # entry point (argparse + asyncio.run)
├── server.py          # MCP stdio server, ToolDef registry, dispatcher
├── errors.py          # canonical ErrorCode enum + ToolError
├── config.py          # env-var-backed Config with .env auto-loading
├── providers.py        # OpenAI and ARK API transport
├── workspace.py       # open_workspace + WorkspaceMeta (gca's "canvas")
├── serialize.py       # JSON / TextContent helpers
├── adapters/
│   ├── image_gen.py / seedance.py       # image + video generation
│   ├── r2.py                             # public image upload
│   ├── rmbg2.py / rmbg2_video.py        # image + video matting
│   ├── produce_validated_*.py            # closed-loop asset production
│   ├── *_validator.py                    # image/video/slot VLM validators
│   └── script_runner.py                  # controlled runner-script launcher
├── tools/
│   ├── lifecycle.py / asset_tools.py
│   ├── concept_tools.py / path_tools.py
│   ├── postprocess_tools.py / video_tools.py
│   └── verify_tools.py
└── runners/
    ├── assets/                           # CPU post-process and color-key scripts
    └── verify/                           # Playwright, video, visual, and gate scripts
```

### Why subprocess for the Node scripts?

The bundled verification runners depend on Node, Playwright, and ffmpeg.
MCP handlers invoke them through the controlled runner so agents use one
validated tool surface and never launch those subprocesses directly. CPU-only
preview/seam and color-key utilities are packaged under
`src/gca_mcp/runners/assets/`; skills carry no executable scripts.

### Workspace = single game directory

Unlike `video-creation-mcp`'s "canvas" (a creative project with media,
nodes, jobs), GCA's workspace is just the `playground/<game-slug>/`
folder. Tools that need an implicit `game_dir` (gate-check,
record_with_state) read it from the open workspace; explicit
`game_dir` arguments still override.

---

## Environment Variables

See the repo-root `.env.example` for the full list. Core settings:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI Images auth for `generate_image` / `edit_image`. |
| `ARK_API_KEY` | Volcengine ARK auth for Seedance video and Doubao VLM. |
| `GAME_ASSET_BASE_URL` / `GAME_ASSET_MODEL` | Image API root and model id. |
| `SEEDANCE_URL` | ARK native async task endpoint. |
| `RMBG_BASE_URL` | RMBG-2.0 endpoint used by image and video matting tools. Leave empty to matte in-process. |

Endpoints and model ids are pre-filled with provider defaults, so a setup
normally only needs `OPENAI_API_KEY` and `ARK_API_KEY`.
