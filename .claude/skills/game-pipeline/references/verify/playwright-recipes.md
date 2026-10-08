# Playwright Page Helpers for Verify

Short, page-only helpers for test modules executed by `mcp__gca__run_tests`.
The runner owns browser launch, navigation, viewport, runtime identity, result
paths, and provenance. Copy a helper into a test module and call it only from
`setup`, `act`, or `assert`; this file does not define the module lifecycle,
manifest, or assertion contract. Those patterns live in `test-patterns.md`.

Composite membership is machine-owned and queryable through
`gate_check({ gate: "gate_contract" })`; the table in `verify-agent.md` is only
an advisory gate-to-evidence routing index.

## Execution contract

```text
PASS = mcp__gca__run_verify_dag({
  game_dir: GAME_DIR,
  mode: "iterate",
  url: URL
})
```

Test modules receive a fresh, already navigated `page`. Do **not** run
`npx playwright`, call `chromium.launch()`, create a page/context, call
`page.goto()`, or select `waitUntil` inside a module. All MCP browser calls use
the exact DAG-bound URL and `verify_run_id`; never hardcode or guess a host,
port, query string, or viewport.

Direct `run_tests` is for a scoped diagnostic against an explicitly active
recovery epoch; it cannot replace the DAG's canonical unfiltered test node.

Navigation remains producer-owned and is not currently uniform. Formal tests,
recording, visual diff, and layout capture use their own internal
`networkidle` waits; play-agent and runtime-asset capture use
`domcontentloaded` plus their producer-specific readiness checks. This is an
implementation fact, not a pattern for modules to copy and not authorization to
weaken runtime identity or replace observable readiness with fixed sleeps.

Page helpers must not create or resize the viewport. Fractional clicks use the
rendered canvas box; game-space coordinates use
`.playtest/inputs/interaction-contract.json` canvas
`designWidth`/`designHeight`. A producer's viewport default is not the game's
design resolution.

## Runner-Owned Screenshots

For a passing formal test, `screenshot_each:true` writes and references
`<safeName>_before.png` and `<safeName>_after.png`. A formal failure after page
creation attempts `FAIL_<safeName>.png` regardless of `screenshot_each` and
references that failure image from the result.

The wrapper's result `scope`—not an image filename—determines whether a run is
canonical, scoped, or solo. Do not hand-write files into
`.playtest/verify/test-run/screenshots/` or treat custom images as canonical
evidence.

## Page Helpers

### Canvas Coordinate Clicks

For canvas-rendered UIs (game engines, WebGL), buttons are not DOM elements:

```javascript
async function canvasBox(page) {
  const canvas = page.locator('canvas').first();
  await canvas.waitFor({ state: 'visible' });
  const box = await canvas.boundingBox();
  if (!box) throw new Error('Expected a visible canvas bounding box');
  return box;
}

async function canvasClick(page, pctX, pctY) {
  if (![pctX, pctY].every(v => Number.isFinite(v) && v >= 0 && v <= 1)) {
    throw new Error(
      `Canvas click fractions must be within [0,1], got ${pctX},${pctY}`
    );
  }
  const box = await canvasBox(page);
  await page.mouse.click(box.x + box.width * pctX, box.y + box.height * pctY);
}

async function gameToPage(page, gameX, gameY, DESIGN_W, DESIGN_H) {
  if (![DESIGN_W, DESIGN_H].every(v => Number.isFinite(v) && v > 0)) {
    throw new Error(
      `Expected positive design dimensions, got ${DESIGN_W}x${DESIGN_H}`
    );
  }
  const box = await canvasBox(page);
  return {
    x: box.x + (gameX / DESIGN_W) * box.width,
    y: box.y + (gameY / DESIGN_H) * box.height,
  };
}

await canvasClick(page, 0.5, 0.7);

// DESIGN_W / DESIGN_H come from the interaction contract.
const pos = await gameToPage(page, targetX, targetY, DESIGN_W, DESIGN_H);
await page.mouse.click(pos.x, pos.y);
```

### Runtime Errors

The formal runner installs its collector before navigation. It currently wraps
`console.error` and listens for `window.error`; it does not collect
`console.warn` or `unhandledrejection`. Do not add a competing
`page.on('pageerror')` or `page.on('console')` listener.

```javascript
async function readVerifyErrors(page) {
  return page.evaluate(() => window.__gameVerifyErrors || []);
}

// In assert:
const errors = await readVerifyErrors(page);
if (errors.length > 0) {
  throw new Error(`Expected no console errors, got ${errors.join('; ')}`);
}
```

If all three phases complete, the runner copies a non-empty array into the PASS
result's `consoleErrors`, but that field does not automatically fail the test.
The current FAIL path records `error` and does not copy the collector array.
Modules must therefore assert explicitly when runtime errors are unacceptable.

### Keyboard Sequence

Minimal keyboard helper for modules. Full input/mechanics examples live in
`test-patterns.md`.

```javascript
async function pressSequence(page, keys, delayMs = 120) {
  for (const key of keys) {
    await page.keyboard.press(key);
    await page.waitForTimeout(delayMs);
  }
}
```

`delayMs` only paces inputs; it must not replace an observable readiness
condition or the final state assertion.

## Producer-Owned Evidence Boundaries

### Runtime Assets

Do not use shell `file` output or raw PNG dimensions as pipeline evidence.
Source dimensions omit content bounds, manifest targets, slot receipts, and
actual runtime use.

```text
mcp__gca__capture_runtime_assets({
  game_dir: GAME_DIR,
  url: RUN.url,
  verify_run_id: RUN.verify_run_id
})
```

Use the resulting runtime registry together with the composite checks for
`asset_runtime_references`, `slot_validation`, `bbox_coverage`,
`aspect_drift`, `ui_contract`, and related asset integration checks. A shell
dimension inspection is maintainer-only diagnosis outside the verify evidence
path and has no release authority.

### Layout (Axis 5)

The compact contract is:

```text
mcp__gca__capture_layout({
  game_dir: GAME_DIR,
  url: RUN.url,
  verify_run_id: RUN.verify_run_id
})
layout_result = mcp__gca__materialize_layout_alignment({
  game_dir: GAME_DIR,
  verify_run_id: RUN.verify_run_id
})
mcp__gca__write_verify_report({
  game_dir: GAME_DIR,
  mode: RUN.mode,
  url: RUN.url,
  verify_run_id: RUN.verify_run_id,
  status: "blocked" if layout_result.blocked else status
})
```

Run the sequence before report finalization, and still write a blocked report
when materialization returns `blocked:true`. Capture writes one atomic
`.playtest/verify_middle/layout/_run.json` plus
`<scene>.rendered.json` files. Capture only discovers objects with a non-empty
`obj.name`; the gate matches a contract key against the captured `name` or
`layoutKey`. Runtime placement helpers establish both identities. For a raw
object, set `obj.name = key` and `obj.setData('layoutKey', key)`.

Re-run after changes to `src/`, `assets/`, `design/`,
`.playtest/inputs/layout-contract.json`, or `gca.config.yaml`. Detailed
sequencing and gate routing live in `.claude/agents/verify-agent.md` under
**Axis 5 — Layout alignment**; signature-to-fix routing lives in
`common-failures.md` under **Layout Alignment Failures**.

Do not hand-write `.playtest/verify_middle/layout/*.rendered.json` or copy the
capture walker. Maintainer internals:
`gca-mcp/src/gca_mcp/runners/verify/capture-layout.mjs`.
