import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const read = (...parts) => readFileSync(join(repoRoot, ...parts), 'utf8');

const designAgent = read('.claude', 'agents', 'design-agent.md');
const designRef = (...parts) => read(
  '.claude',
  'skills',
  'game-pipeline',
  'references',
  'design',
  ...parts,
);
const assetPlan = designRef('asset-plan-guide.md');
const conceptArt = designRef('concept-art-guide.md');
const gddTemplate = designRef('gdd-template.md');

test('design workflow uses the canonical four-layer mechanics precedence', () => {
  assert.match(
    designAgent,
    /1\. Prompt `\[MUST\]` \/ `\[MUST_NOT\]` hard constraints;[\s\S]*2\. other explicit Prompt prose;[\s\S]*3\. coherent concept-derived additions[\s\S]*4\. tagged genre-standard defaults/,
  );
});

test('design workflow reserves bounded agent budget for the GDD write', () => {
  assert.match(designAgent, /Reserve the final write\s+call for `GAME_DESIGN\.md`/);
  assert.match(designAgent, /one representative image read/);
  assert.match(conceptArt, /use each screen's `detect_layout` result/);
  assert.doesNotMatch(conceptArt, /For each mockup, `Read` it/);
});

test('layout kind guidance matches gate semantics', () => {
  assert.doesNotMatch(gddTemplate, /checked exactly/);
  assert.doesNotMatch(gddTemplate, /checks only the spawn frame/);
  assert.doesNotMatch(conceptArt, /checks only the spawn frame/);
  assert.match(gddTemplate, /same center-point check with `1\.25×` tolerance/);
  assert.match(conceptArt, /same center-point check with `1\.25×` tolerance/);
});

test('cluster containment is attributed to gateUiLayout', () => {
  assert.doesNotMatch(gddTemplate, /member-inside/);
  assert.doesNotMatch(gddTemplate, /origin corner/);
  assert.match(
    gddTemplate,
    /`gateLayoutAlignment` checks the\s+rendered Container's anchor-point at its declared `origin`/,
  );
  assert.match(
    gddTemplate,
    /`gateUiLayout` separately\s+requires every child inside the container with no sibling overlap or overflow/,
  );
});

test('binding and UI sizing guidance names the correct authorities', () => {
  assert.match(assetPlan, /Primary menu \/ CTA buttons/);
  assert.match(assetPlan, /gates\.ui_scale\.max_canvas_fraction\.panel/);
  assert.doesNotMatch(
    assetPlan,
    /Panels \(selection\/build\/context\)[\s\S]{0,180}max_canvas_fraction\.hud/,
  );
  assert.match(assetPlan, /tower cost is live text/);
  assert.match(assetPlan, /other live labels remain `gateUiBinding` responsibilities/);
  assert.match(gddTemplate, /live cost text/);
  assert.match(gddTemplate, /other live labels are enforced by `gateUiBinding`/);
});

test('concept guidance uses dispatched/self-contained references', () => {
  assert.doesNotMatch(conceptArt, /<REPO_ROOT>/);
  assert.match(
    conceptArt,
    /VISUAL numbers use authored sizes and measured positions/,
  );
  assert.doesNotMatch(conceptArt, /author sizes\/tuning/);
});
