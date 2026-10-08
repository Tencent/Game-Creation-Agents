import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const rulesDir = join(repoRoot, '.claude', 'rules');
const iterationProcedures =
  '.claude/skills/game-pipeline/references/iteration-procedures.md';

function readRule(name) {
  return readFileSync(join(rulesDir, name), 'utf8');
}

function frontmatterPaths(name) {
  const text = readRule(name);
  const frontmatter = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  assert.ok(frontmatter, `${name} must have YAML frontmatter`);
  return [...frontmatter[1].matchAll(/^\s*-\s+"([^"]+)"\s*$/gm)]
    .map((match) => match[1]);
}

test('scoped rules retain the iteration evidence runbook', () => {
  assert.ok(
    frontmatterPaths('documentation-contracts.md').includes(iterationProcedures),
  );
  assert.ok(
    frontmatterPaths('pipeline-evidence.md').includes(iterationProcedures),
  );
});

test('scoped rules do not restore broad context-loading patterns', () => {
  const documentationPaths = frontmatterPaths('documentation-contracts.md');
  for (const path of [
    'CLAUDE.md',
    'gca-mcp/CLAUDE.md',
    'playground/CLAUDE.md',
    '.claude/skills/**/*.md',
  ]) {
    assert.ok(!documentationPaths.includes(path), `unexpected broad path: ${path}`);
  }

  const evidencePaths = frontmatterPaths('pipeline-evidence.md');
  for (const path of [
    '.claude/hooks/**/*',
    '.claude/skills/game-pipeline/**/*.md',
    'playground/**/src/**/*',
  ]) {
    assert.ok(!evidencePaths.includes(path), `unexpected broad path: ${path}`);
  }
});

test('agent-boundary rule covers every enforcing configuration surface', () => {
  const paths = frontmatterPaths('agent-boundary-sync.md');
  for (const path of [
    '.claude/agents/**/*.md',
    '.claude/skills/game-pipeline/SKILL.md',
    '.claude/skills/game-pipeline/dispatch-contract.md',
    '.claude/settings.json',
    '.claude/hooks/session/guard-agent-boundary.py',
    '.claude/hooks/session/bind-pipeline-session.py',
    '.claude/hooks/session/pipeline_session.py',
  ]) {
    assert.ok(paths.includes(path), `missing agent-boundary path: ${path}`);
  }
});

test('rules remain non-blocking and preserve authority ownership', () => {
  for (const name of [
    'agent-boundary-sync.md',
    'documentation-contracts.md',
    'pipeline-evidence.md',
  ]) {
    assert.match(readRule(name), /\bnon-blocking\b/);
  }

  const evidence = readRule('pipeline-evidence.md');
  assert.match(
    evidence,
    /`gca\.config\.yaml` owns runtime defaults, limits, thresholds, and retry budgets\./,
  );
  assert.match(evidence, /Canonical ToolDefs own public tool schemas;/);
});
