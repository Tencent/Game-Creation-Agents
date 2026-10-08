# Quality and Capability Profiles

Startup selects art/layout policy, content architecture, and release capability
without weakening mechanics, evidence, correctness, accessibility, or integrity.

## 1. Startup selection

- `art_directed`: bespoke illustrated game and the default.
- `responsive`: localized, resizable, orientation-aware, or text-heavy UI.
- `pixel_art`: intentional pixel grid, nearest-neighbor rendering, integer scale.
- `minimal`: deliberately sparse geometry/typography, never placeholder quality.

Honor an explicit prompt choice; infer a non-default only from clear requirements.
`quality_profile` and `level_model` become immutable after the first state write.
Current tooling rejects an in-place change—it does not automatically start a new
run or reproject contracts. A change requires a separately initialized run.

## 2. Policy matrix

| Contract | `art_directed` | `responsive` | `pixel_art` | `minimal` |
|---|---|---|---|---|
| Static labels | Raster lettering/widget art when designed as art | Live localized text; raster only logos/illustration | Live text or sprite lettering | Live text allowed |
| UI sizing | Authored display size; render 1:1 | Uniform runtime fit/scale | Integer uniform runtime scale | Uniform runtime scale |
| Concept coverage | Distinct mockup per interactive scene | Shared explicit scene references allowed | Shared explicit scene references allowed | Shared explicit scene references allowed |
| Crop coverage | Every grounded crop ships or is waived | Only `required:true` crops | Only `required:true` crops | Only `required:true` crops |
| Functional widgets | Art assets required | Art, nine-slice, or deliberate composition | Sprite art or deliberate composition | Deliberate primitives/text allowed |
| Effects | Runtime particle recipes; no FX sheets | Particles, masks, shaders, or flipbooks | Flipbooks or particles | Any deliberate low-cost method |

All profiles preserve aspect ratio; non-uniform distortion always fails.
For every file-backed static manifest row, `width` and `height` are required
final PNG pixel dimensions and must match the readable file exactly.

File-backed UI sizing is exclusive by profile:

- `art_directed` declares exactly one positive `targetWidth` xor
  `targetHeight`, with no max/logical/base/integer mode.
- `responsive` and `minimal` declare exactly one positive `maxBox` xor
  `logicalBox`, including decorative UI, with no target/base/integer mode.
- `pixel_art` declares only a positive two-axis `baseSize` plus non-empty
  positive `integerScales`; the finalized PNG dimensions equal `baseSize`.

`pixel_art` additionally requires the running Phaser game's
`game.config.pixelArt` to be `true`. Verification captures that runtime value;
source strings or unrelated objects cannot stand in for nearest-neighbor
texture filtering.

Every profile must attempt concept mockup generation. At least one successful
mockup is required unless every interactive reference attempt exhausts bounded
generation and uses the exact failure waiver.

Every `design/crops/<scene>/crops.json` remains producer evidence, not a
path-only inventory: its source/detection hashes and every crop path/hash must
match current files. GDD or manifest crop references must resolve to those exact
current rows. Profile coverage and explicit waivers decide which rows must ship;
they never waive evidence integrity.

If bounded concept generation fails, any profile may use the exact
`waived:concept-generation-unavailable` fallback for the affected scene. The
scene still requires complete mechanics, assets, normalized layout, and UI
contracts. This is a warning-only design fallback, not a release waiver and not
permission to claim missing visual evidence.

## 3. Source and manifest contract

Allowed strategies: `generated-image`, `supplied-image`, `licensed-image`,
`procedural`, `live-text`, and `composition`. Hand-authored files use
`supplied-image`; never invent a source or license. `bitmap-text` is unsupported
until the pipeline has a complete manifest-backed font-atlas contract.

- `generated-image` → `assets-agent MODE=generate`.
- Existing supplied/licensed still → `MODE=register-existing`; preserve pixels
  and record provenance, hash, dimensions, and required bbox metadata.
- Existing animated sprite → block until a validated ingestion route exists.
- Procedural/live-text/composition → no generated-file batch.

Outside `art_directed`, a no-file schema-v2 manifest requires
`fileAssetsRequired:false`, `assets:[]`, the selected `qualityProfile`, and a
non-empty unique `runtimeSources` exactly matching layout elements.
A manifest with files uses `fileAssetsRequired:true` and still lists its exact
non-file strategies.

Every recursive layout node's logical `key`/`bind` identity is unique within its
scene. The same identity may be reused in another scene only with the same source
strategy; conflicting cross-scene strategies fail. Runtime-sourced display
objects keep that identity in `name`, `layoutKey`, or `bind`. Text placed over a
file-backed widget keeps the host layout key plus the manifest anchor's exact
`id` and `binds`. A generated Phaser UUID is accepted only when current runtime
evidence shows the compatible object displayed against that declared node or
anchor.

## 4. Level capability

| `LEVEL_MODEL` | Owner and requirement |
|---|---|
| `authored` | legal for any genre; level-design-agent produces validated `levels/stage_*.json` |
| `procedural` | design/implementation runtime generation |
| `board` | board/deck/grid runtime contract; no level dispatch or `levels/*.json` |
| `none` | no level-content subsystem |

`LEVEL_MODEL` is the sole level-applicability/dispatch authority. Set
`LEVEL_DESIGN_REQUIRED` exactly when `LEVEL_MODEL=authored`; genre and directory
existence never override it.

## 5. Invariants and release waiver

- Never fabricate assets, measurements, evidence, run IDs, or verdicts.
- Judge sources by rights and visual/runtime fitness, not AI provenance.
- Dynamic and roster-derived values remain runtime data.
- Runtime keys, paths, dimensions, sidecars, files, and collision metadata agree.
- Mechanics correctness, runtime scene/layout coverage, asset integrity, and
  other quality gates cannot be waived. Only the missing concept reference has
  the bounded-failure fallback defined in §2.

The only implemented release waiver is `video-performance` for
`verify.skip_video:true`:

```json
{"release_waivers":[{
  "id":"video-performance",
  "reason":"specific reason of at least twelve characters",
  "approved_by":"human or policy authority",
  "approved_at":"ISO-8601 timestamp"
}]}
```

`write_pipeline_state` binds approval to the current `pipeline_run_id` and
`build_digest` and adds an MCP-owned `approval_event_id`. A build change requires
a newer `approved_at` than the prior state update; an old waiver object cannot be
rebound. Future-dated approvals fail. Only video, analysis, performance, and
playthrough requirements are waived; every other release gate remains mandatory.
