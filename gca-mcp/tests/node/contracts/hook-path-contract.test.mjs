import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const settings = JSON.parse(
  readFileSync(join(repoRoot, '.claude/settings.json'), 'utf8'),
);

const EXPECTED_HOOK_PATHS = [
  '.claude/hooks/enforce-pipeline.sh',
  '.claude/hooks/session/bind-pipeline-session.py',
  '.claude/hooks/session/guard-agent-boundary.py',
  '.claude/hooks/session/guard-mcp-restart.sh',
  '.claude/hooks/validation/check-env.py',
];

function projectPaths(value, paths = new Set()) {
  if (typeof value === 'string') {
    for (const match of value.matchAll(
      /\$\{CLAUDE_PROJECT_DIR\}\/([^\s"']+)/g,
    )) {
      paths.add(match[1]);
    }
  } else if (Array.isArray(value)) {
    for (const item of value) projectPaths(item, paths);
  } else if (value && typeof value === 'object') {
    for (const item of Object.values(value)) projectPaths(item, paths);
  }
  return paths;
}

test('Claude settings reference the organized hook entrypoints', () => {
  const paths = [...projectPaths(settings.hooks)].sort();
  assert.deepEqual(paths, EXPECTED_HOOK_PATHS);
  for (const path of paths) {
    assert.equal(existsSync(join(repoRoot, path)), true, `missing hook: ${path}`);
  }
});

test('directly executed hook commands retain executable mode', () => {
  const commands = Object.values(settings.hooks)
    .flatMap((registrations) => registrations)
    .flatMap((registration) => registration.hooks)
    .map((hook) => hook.command);
  const directPaths = commands
    .map((command) => (
      /^\$\{CLAUDE_PROJECT_DIR\}\/(\S+)$/.exec(command)?.[1] || null
    ))
    .filter(Boolean);

  assert.deepEqual(directPaths.sort(), [
    '.claude/hooks/enforce-pipeline.sh',
  ]);
  for (const path of directPaths) {
    assert.notEqual(
      statSync(join(repoRoot, path)).mode & 0o111,
      0,
      `hook must remain executable: ${path}`,
    );
  }
});

test('hook-internal project paths resolve after organization', () => {
  for (const caller of [
    '.claude/hooks/enforce-pipeline.sh',
  ]) {
    const source = readFileSync(join(repoRoot, caller), 'utf8');
    const paths = [...source.matchAll(
      /\$PROJECT_DIR\/(\.claude\/hooks\/[A-Za-z0-9_./-]+)/g,
    )].map((match) => match[1]);
    assert.ok(paths.length > 0, `expected project paths in ${caller}`);
    for (const path of new Set(paths)) {
      if (path === '.claude/hooks/.dev_mode') continue;
      assert.equal(
        existsSync(join(repoRoot, path)),
        true,
        `${caller} references missing hook path: ${path}`,
      );
    }
  }
});
