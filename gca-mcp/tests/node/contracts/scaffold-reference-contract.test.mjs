import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { validateJsonSchema } from '../../../src/gca_mcp/runners/verify/json-schema.mjs';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const scaffoldRoot = join(
  repoRoot,
  '.claude/skills/game-pipeline/references/scaffold',
);
const examplesRoot = join(scaffoldRoot, 'examples');
const schemasRoot = join(scaffoldRoot, 'schemas');

const exampleSchemas = new Map([
  ['contracts/layout-cluster.json', 'layout-contract.schema.json'],
  ['contracts/layout-waived-scene.json', 'layout-contract.schema.json'],
  ['contracts/objectives.json', 'objectives.schema.json'],
  ['contracts/platform-interaction.json', 'interaction-contract.schema.json'],
  ['contracts/pointer-interaction.json', 'interaction-contract.schema.json'],
  ['manifest/binding-panel.json', 'asset-manifest.schema.json'],
  ['manifest/layout-cluster.json', 'asset-manifest.schema.json'],
  ['manifest/no-file-runtime.json', 'asset-manifest.schema.json'],
  ['manifest/sprite-per-action.json', 'asset-manifest.schema.json'],
]);

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

function jsonFilesUnder(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return jsonFilesUnder(path);
    return entry.isFile() && entry.name.endsWith('.json') ? [path] : [];
  });
}

function relativeExamplePath(path) {
  return relative(examplesRoot, path).replaceAll('\\', '/');
}

function fencedBlockAfter(text, heading) {
  const headingAt = text.indexOf(heading);
  assert.notEqual(headingAt, -1, `missing heading: ${heading}`);
  const match = text.slice(headingAt).match(/```(?:js|javascript)\r?\n([\s\S]*?)\r?\n```/);
  assert.ok(match, `missing JavaScript block after: ${heading}`);
  return match[1];
}

test('every scaffold example validates against its canonical schema', () => {
  const actualPaths = jsonFilesUnder(examplesRoot)
    .map(relativeExamplePath)
    .sort();
  const expectedPaths = [...exampleSchemas.keys()].sort();
  assert.deepEqual(
    actualPaths,
    expectedPaths,
    'every scaffold example must have an explicit schema mapping',
  );

  const schemas = new Map();
  for (const path of actualPaths) {
    const schemaName = exampleSchemas.get(path);
    if (!schemas.has(schemaName)) {
      schemas.set(schemaName, readJson(join(schemasRoot, schemaName)));
    }
    const errors = validateJsonSchema(
      readJson(join(examplesRoot, path)),
      schemas.get(schemaName),
      path,
    );
    assert.deepEqual(errors, [], `${path}:\n${errors.join('\n')}`);
  }
});

test('objectives schema rejects unsupported setup and success DSL keys', () => {
  const schema = readJson(join(schemasRoot, 'objectives.schema.json'));
  const document = readJson(join(examplesRoot, 'contracts/objectives.json'));
  document.objectives[0].setup = {
    seedBoard: 'place two matching tiles',
  };
  document.objectives[0].success = {
    read: ['score'],
    predicate: 'score increased',
  };

  const errors = validateJsonSchema(document, schema);
  assert.ok(
    errors.some((error) => error.includes('.setup.seedBoard: unknown field')),
    errors.join('\n'),
  );
  assert.ok(
    errors.some((error) => error.includes('.success.read: unknown field')),
    errors.join('\n'),
  );
});

test('objectives schema rejects scene-start arguments without scene', () => {
  const schema = readJson(join(schemasRoot, 'objectives.schema.json'));
  const document = readJson(join(examplesRoot, 'contracts/objectives.json'));
  document.objectives[0].setup = {
    stop: ['TitleScene'],
    data: { level: 2 },
  };

  const errors = validateJsonSchema(document, schema);
  assert.ok(
    errors.some((error) => error.includes('.setup.scene: required')),
    errors.join('\n'),
  );
});

test('objectives schema rejects whitespace-only executable strings', () => {
  const schema = readJson(join(schemasRoot, 'objectives.schema.json'));
  const document = readJson(join(examplesRoot, 'contracts/objectives.json'));
  document.objectives[0].operation = '   ';
  document.objectives[0].setup = {
    state: { '   ': { score: 0 } },
    eval: '\t',
  };
  document.objectives[0].success = { activeScene: '\n' };

  const errors = validateJsonSchema(document, schema);
  for (const field of ['operation', 'property name', 'eval', 'activeScene']) {
    assert.ok(
      errors.some((error) => error.includes(field)),
      `${field} missing from:\n${errors.join('\n')}`,
    );
  }
});

test('objectives schema rejects duplicate objective ids', () => {
  const schema = readJson(join(schemasRoot, 'objectives.schema.json'));
  const document = readJson(join(examplesRoot, 'contracts/objectives.json'));
  document.objectives.push(structuredClone(document.objectives[0]));

  const errors = validateJsonSchema(document, schema);
  assert.ok(
    errors.some((error) => error.includes('.id: duplicate value')),
    errors.join('\n'),
  );
});

test('waived layout example keeps an exact scene and token pairing', () => {
  const layout = readJson(join(
    examplesRoot,
    'contracts/layout-waived-scene.json',
  ));
  const allowedReferences = new Set([
    'waived:pure-transition',
    'waived:concept-generation-unavailable',
  ]);
  const waivedScenes = layout.waived_scenes;

  assert.equal(new Set(waivedScenes).size, waivedScenes.length);
  for (const sceneKey of waivedScenes) {
    assert.ok(layout.scenes[sceneKey], `missing waived scene: ${sceneKey}`);
    assert.ok(
      allowedReferences.has(layout.scenes[sceneKey].reference),
      `invalid waiver reference for: ${sceneKey}`,
    );
  }
  for (const [sceneKey, scene] of Object.entries(layout.scenes)) {
    if (allowedReferences.has(scene.reference)) {
      assert.ok(
        waivedScenes.includes(sceneKey),
        `waiver reference is not listed: ${sceneKey}`,
      );
    }
  }
});

test('scaffold schemas reject ambiguous array lengths', () => {
  const manifestSchema = readJson(join(
    schemasRoot,
    'asset-manifest.schema.json',
  ));
  const asset = {
    key: 'fixture',
    category: 'ui',
    sourceStrategy: 'generated-image',
    path: 'assets/ui/fixture.png',
    description: 'Schema length fixture.',
  };
  const manifest = {
    schema_version: 2,
    qualityProfile: 'responsive',
    fileAssetsRequired: true,
    runtimeSources: [],
    assets: [asset],
  };

  const runtimeSources = [
    'procedural',
    'live-text',
    'composition',
    'procedural',
  ];
  assert.deepEqual(
    validateJsonSchema(
      { ...manifest, runtimeSources },
      manifestSchema,
    ),
    ['$.runtimeSources: needs at most 3 item(s)'],
  );

  for (const field of ['maxBox', 'logicalBox', 'baseSize']) {
    assert.deepEqual(
      validateJsonSchema(
        { ...manifest, assets: [{ ...asset, [field]: [1, 2, 3] }] },
        manifestSchema,
      ),
      [`$.assets[0].${field}: needs at most 2 item(s)`],
    );
  }

  const interactionSchema = readJson(join(
    schemasRoot,
    'interaction-contract.schema.json',
  ));
  const interaction = {
    schema_version: 1,
    genre: 'custom',
    interaction_profile: 'none',
    scheme: 'none',
    canvas: { designWidth: 800, designHeight: 600 },
    gameContext: 'Schema length fixture.',
    actions: {},
    world: {},
    layers: [],
    entities: {},
    matrix: [['player', 'target', 'activate', 'unexpected']],
  };
  assert.deepEqual(
    validateJsonSchema(interaction, interactionSchema),
    ['$.matrix[0]: needs at most 3 item(s)'],
  );
});

test('scaffold and GDD CONFIG examples use nested screen dimensions', () => {
  const scaffoldTemplate = readFileSync(
    join(scaffoldRoot, 'templates.md'),
    'utf8',
  );
  const gddTemplate = readFileSync(
    join(
      repoRoot,
      '.claude/skills/game-pipeline/references/design/gdd-template.md',
    ),
    'utf8',
  );
  const scaffoldConfig = fencedBlockAfter(
    scaffoldTemplate,
    '### `src/config.js`',
  );
  const gddConfig = fencedBlockAfter(
    gddTemplate,
    '## Section 3: Game Configuration',
  );
  const nestedScreen = /^\s{2}screen:\s*\{\s*width:.*,\s*height:.*\},/m;

  assert.match(scaffoldConfig, nestedScreen);
  assert.match(gddConfig, nestedScreen);
  assert.doesNotMatch(scaffoldConfig, /^\s{2}(?:width|height):/m);
});
