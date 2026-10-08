import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const read = (...parts) => readFileSync(join(repoRoot, ...parts), 'utf8');

const assetsAgent = read('.claude', 'agents', 'assets-agent.md');
const spriteGuide = read(
  '.claude',
  'skills',
  'game-pipeline',
  'references',
  'assets',
  'sprite-guide.md',
);

test('asset workflow batches every owned slot into one full-fan-out call', () => {
  for (const contract of [assetsAgent, spriteGuide]) {
    assert.match(contract, /validate_asset_slot\(\{\s*items:/);
    assert.match(contract, /one worker per item/);
  }
  assert.match(assetsAgent, /Do not serialize one MCP call per asset/);
});

test('asset workflow requires every generated manifest artifact receipt', () => {
  assert.match(assetsAgent, /every generated asset/);
  assert.match(
    spriteGuide,
    /Every `sourceStrategy:"generated-image"` manifest entry/,
  );
});
