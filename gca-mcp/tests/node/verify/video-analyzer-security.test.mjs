import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const packageRoot = fileURLToPath(new URL('../../../', import.meta.url));
const analyzerPath = join(
  packageRoot,
  'src',
  'gca_mcp',
  'runners',
  'verify',
  'gemini-video-analyze.mjs',
);
const source = readFileSync(analyzerPath, 'utf8');
const llmClientPath = join(
  packageRoot,
  'src',
  'gca_mcp',
  'runners',
  'verify',
  'llm-client.mjs',
);
const llmClientSource = readFileSync(llmClientPath, 'utf8');
const callOnce = source.slice(
  source.indexOf('async function callOnce'),
  source.indexOf('// Run exactly one request against the pinned model'),
);
const llmClient = await import(pathToFileURL(llmClientPath));

test('video analysis API requests never pass caller data through a shell', () => {
  assert.ok(callOnce.length > 0, 'callOnce implementation must be discoverable');
  assert.doesNotMatch(callOnce, /\bexecSync\s*\(/);
  assert.match(callOnce, /await fetch\(target,/);
  assert.match(callOnce, /new URL\(url\)/);
});

test('all Node VLM runners pin one Doubao 2.0 request', () => {
  for (const runtimeSource of [source, llmClientSource]) {
    const modelIds = runtimeSource.match(/doubao-seed-[a-z0-9-]+/g) || [];
    assert.deepEqual([...new Set(modelIds)], ['doubao-seed-2-0-pro-260215']);
  }
  assert.doesNotMatch(source, /ATTEMPTS_PER_MODEL|VLM_ALL_MODELS_FAILED/);
  assert.match(llmClientSource, /retries = 0/);
});

test('Node VLM accepts only the ARK transport', () => {
  const previous = {
    ARK_API_KEY: process.env.ARK_API_KEY,
  };
  try {
    process.env.ARK_API_KEY = 'test-key';
    assert.equal(llmClient.detectProvider({}), 'ark-doubao');
    assert.equal(llmClient.detectProvider({ provider: 'ark-doubao' }), 'ark-doubao');
    assert.equal(llmClient.detectProvider({ provider: 'unsupported' }), null);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
