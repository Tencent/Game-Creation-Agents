import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const verifyRoot = join(
  repoRoot,
  'gca-mcp/src/gca_mcp/runners/verify',
);
const enginePath = join(verifyRoot, 'gate-engine.mjs');
const gatesRoot = join(verifyRoot, 'gates');

const PUBLIC_EXPORTS = [
  'canonicalBuildDigest',
  'gateAspectDrift',
  'gateAssetIntegrationReport',
  'gateAssetManifest',
  'gateAssetRuntimeReferences',
  'gateAssetsGenerated',
  'gateBasicStructure',
  'gateBboxCoverage',
  'gateCanonicalHelpers',
  'gateCriticReport',
  'gateCropCoverage',
  'gateElementAlignment',
  'gateEntityScale',
  'gateGameplayAlignment',
  'gateHudBinding',
  'gateInteractionContract',
  'gateInteractionFit',
  'gateLayoutAlignment',
  'gateLevelRuntime',
  'gateLevelsPresent',
  'gateMapLayoutPresent',
  'gateMattingComplete',
  'gateNoBakedPath',
  'gateNoFxSpritesheets',
  'gateNoOpenIssues',
  'gatePathFromLayout',
  'gatePerformance',
  'gatePhysicsAlignment',
  'gatePlayQuality',
  'gatePlaythroughRecorded',
  'gatePolishRounds',
  'gatePrefabCoverage',
  'gateProjectedContracts',
  'gateReleaseWaivers',
  'gateRubricContract',
  'gateSceneCoverage',
  'gateScenesImplemented',
  'gateSlotValidation',
  'gateStateJson',
  'gateStaticTextAsAsset',
  'gateSubjectMargin',
  'gateTestCoverage',
  'gateTestsExist',
  'gateTestsPassing',
  'gateTowerDefenseContract',
  'gateTunnelRisk',
  'gateUiArtParity',
  'gateUiBinding',
  'gateUiContract',
  'gateUiLayout',
  'gateUiProportion',
  'gateVerifyReport',
  'gateVisualAlignment',
  'gateVisualDiff',
  'gateVisualVerified',
  'materializeLayoutAlignment',
].sort();

const PUBLIC_ENTRYPOINTS = [
  'capture-layout.mjs',
  'capture-runtime-assets.mjs',
  'element-visual-diff.mjs',
  'gate-check.mjs',
  'gemini-video-analyze.mjs',
  'play-agent.mjs',
  'probe-playwright.mjs',
  'record-with-state.mjs',
  'state-extractor.mjs',
  'test-runner.mjs',
];

const PROVENANCE_LITERALS = [
  'capture-layout.mjs',
  'element-visual-diff.mjs',
  'play-agent.mjs',
  'runtime-identity.mjs',
  'test-runner.mjs',
];

async function importEngine() {
  const originalArgv = process.argv;
  process.argv = [
    process.execPath,
    enginePath,
    '--game',
    repoRoot,
    '--gate',
    'gate_contract',
    '--config',
    join(repoRoot, 'gca.config.yaml'),
  ];
  try {
    return await import(`${pathToFileURL(enginePath).href}?contract-test=1`);
  } finally {
    process.argv = originalArgv;
  }
}

test('gate engine keeps its public export contract', async () => {
  const engine = await importEngine();
  assert.deepEqual(Object.keys(engine).sort(), PUBLIC_EXPORTS);
});

test('verify runner public entrypoint basenames remain stable', () => {
  for (const basename of PUBLIC_ENTRYPOINTS) {
    assert.equal(
      existsSync(join(verifyRoot, basename)),
      true,
      `missing verify runner entrypoint: ${basename}`,
    );
  }
});

test('gate engine retains frozen producer provenance literals', () => {
  const source = [
    enginePath,
    ...readdirSync(gatesRoot)
      .filter((name) => name.endsWith('.mjs'))
      .map((name) => join(gatesRoot, name)),
  ].map((path) => readFileSync(path, 'utf8')).join('\n');
  for (const literal of PROVENANCE_LITERALS) {
    assert.match(source, new RegExp(`['"]${literal.replaceAll('.', '\\.')}['"]`));
  }
});

test('gate domain modules are importable after extraction', async (t) => {
  if (!existsSync(gatesRoot)) {
    t.skip('gate domains have not been extracted yet');
    return;
  }
  const modules = readdirSync(gatesRoot)
    .filter((name) => name.endsWith('.mjs'))
    .sort();
  assert.deepEqual(modules, [
    'alignment.mjs',
    'assets.mjs',
    'genre.mjs',
    'layout.mjs',
    'structure.mjs',
    'support.mjs',
    'ui.mjs',
    'verify.mjs',
  ]);
  for (const module of modules) {
    await import(`${pathToFileURL(join(gatesRoot, module)).href}?contract-test=1`);
  }
});
