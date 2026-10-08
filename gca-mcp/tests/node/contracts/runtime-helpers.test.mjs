import assert from 'node:assert/strict';
import {
  copyFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const implementReferences = join(
  repoRoot,
  '.claude',
  'skills',
  'game-pipeline',
  'references',
  'implement',
);

function runtimeFixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'gca-runtime-helpers-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));

  writeFileSync(join(dir, 'package.json'), '{"type":"module"}\n');
  writeFileSync(join(dir, 'config.js'), 'export const CONFIG = {};\n');
  copyFileSync(
    join(implementReferences, 'runtime', 'display-utils.js'),
    join(dir, 'display-utils.js'),
  );
  copyFileSync(
    join(implementReferences, 'particle_recipes.js'),
    join(dir, 'particle-recipes.js'),
  );

  return dir;
}

function installGlobalPhaser(t) {
  const previous = globalThis.Phaser;
  class Line {
    constructor(x1, y1, x2, y2) {
      Object.assign(this, { x1, y1, x2, y2 });
    }
  }
  class Circle {
    constructor(x, y, radius) {
      Object.assign(this, { x, y, radius });
    }
  }
  globalThis.Phaser = { Geom: { Line, Circle } };
  t.after(() => {
    if (previous === undefined) delete globalThis.Phaser;
    else globalThis.Phaser = previous;
  });
}

async function importFixtureModule(dir, name) {
  return import(pathToFileURL(join(dir, name)).href);
}

function particleScene() {
  const emitters = [];
  const timers = [];
  const scene = {
    add: {
      particles(x, y, texture, config) {
        const listeners = new Map();
        const emitter = {
          x,
          y,
          texture,
          config,
          destroyCalls: 0,
          explodeCount: null,
          once(event, callback) {
            listeners.set(event, callback);
            return this;
          },
          off(event, callback) {
            if (listeners.get(event) === callback) listeners.delete(event);
            return this;
          },
          emit(event) {
            const callback = listeners.get(event);
            if (!callback) return false;
            listeners.delete(event);
            callback(this);
            return true;
          },
          listenerCount(event) {
            return listeners.has(event) ? 1 : 0;
          },
          explode(count) {
            this.explodeCount = count;
            return this;
          },
          destroy() {
            this.destroyCalls += 1;
            this.emit('destroy');
          },
        };
        emitters.push(emitter);
        return emitter;
      },
    },
    time: {
      delayedCall(delay, callback) {
        const timer = {
          delay,
          callback,
          removed: false,
          remove() {
            this.removed = true;
          },
        };
        timers.push(timer);
        return timer;
      },
    },
  };
  return { scene, emitters, timers };
}

test('pixel-art UI selects an authored integer scale and rejects shrinking', async (t) => {
  const dir = runtimeFixture(t);
  const { applyUiDisplay } = await importFixtureModule(dir, 'display-utils.js');
  const scales = [];
  const image = {
    texture: {
      getSourceImage: () => ({ width: 100, height: 50 }),
    },
    setScale(scale) {
      scales.push(scale);
      return this;
    },
  };

  applyUiDisplay(image, {
    profile: 'pixel_art',
    targetWidth: 250,
    targetHeight: 125,
    integerScales: [1, 2, 3],
  });

  assert.deepEqual(scales, [2]);

  assert.throws(
    () => applyUiDisplay(image, {
      profile: 'pixel_art',
      targetWidth: 50,
      targetHeight: 25,
      integerScales: [1, 2, 3],
    }),
    /cannot shrink below 1x/,
  );
});

test('placeUiText preserves host and anchor identity for runtime evidence', async (t) => {
  const dir = runtimeFixture(t);
  const { addUiCluster, placeUiText } = await importFixtureModule(
    dir,
    'display-utils.js',
  );
  const data = new Map();
  const text = {
    style: { fontSize: '16px' },
    width: 20,
    height: 12,
    setOrigin() { return this; },
    setDepth() { return this; },
    setScrollFactor() { return this; },
    setFontSize(value) {
      this.style.fontSize = `${value}px`;
      return this;
    },
    setText() { return this; },
    setData(key, value) {
      data.set(key, value);
      return this;
    },
  };
  const scene = {
    add: {
      text: () => text,
      container: () => ({
        setDepth() { return this; },
        setData() { return this; },
      }),
    },
  };
  const imageData = new Map([['layoutKey', 'ui-timer-panel']]);
  const image = {
    displayWidth: 200,
    displayHeight: 60,
    x: 100,
    y: 30,
    originX: 0.5,
    originY: 0.5,
    depth: 1000,
    texture: { key: 'ui-timer-panel' },
    getData: (key) => imageData.get(key),
  };

  const placed = placeUiText(
    scene,
    image,
    { id: 'timer-value', binds: 'elapsedSeconds' },
    '12',
  );
  assert.equal(placed.name, 'timer-value');
  assert.equal(data.get('layoutKey'), 'ui-timer-panel');
  assert.equal(data.get('bind'), 'elapsedSeconds');

  assert.throws(
    () => addUiCluster(scene, 'hud', {
      x: 0,
      y: 0,
      gap: 0,
      children: [{
        kind: 'bitmap-text',
        bind: 'score',
      }],
    }),
    /unsupported child kind 'bitmap-text'/,
  );
});

test('groundProbe requires authored standable semantics', async (t) => {
  const dir = runtimeFixture(t);
  const { groundProbe } = await importFixtureModule(dir, 'display-utils.js');
  const body = {
    x: 999,
    left: 12,
    bottom: 34,
    width: 20,
  };
  const sprite = { body };
  let candidates = [body, { gameObject: sprite }];
  let overlapArgs = null;
  const scene = {
    physics: {
      overlapRect(...args) {
        overlapArgs = args;
        return candidates;
      },
    },
  };

  const groundBody = { gameObject: { name: 'ground' } };
  const standable = (candidate) => candidate === groundBody;
  assert.equal(groundProbe(scene, sprite, standable, 6), false);
  assert.deepEqual(overlapArgs, [12, 34, 20, 6, true, true]);

  candidates = [body, { gameObject: sprite }, { gameObject: { name: 'coin' } }];
  assert.equal(groundProbe(scene, sprite, standable, 6), false);
  candidates.push(groundBody);
  assert.equal(groundProbe(scene, sprite, standable, 6), true);
  assert.throws(() => groundProbe(scene, sprite, null, 6), /standable-body predicate/);

  body.blocked = { down: true };
  assert.throws(
    () => groundProbe(scene, sprite, 6),
    /standable-body predicate/,
  );
  assert.equal(groundProbe(scene, sprite, standable, 6), true);

  body.blocked.down = false;
  body.touching = { down: true };
  candidates = [body, { gameObject: sprite }, { gameObject: { name: 'coin' } }];
  assert.equal(groundProbe(scene, sprite, standable, 6), false);

  assert.throws(
    () => groundProbe(scene, { body: null }, 4),
    /standable-body predicate/,
  );
});

test('oneWayProcess scopes drop-through to its collider window', async (t) => {
  const dir = runtimeFixture(t);
  const { oneWayProcess } = await importFixtureModule(dir, 'display-utils.js');
  const actor = {
    scene: { time: { now: 100 } },
    body: {
      velocity: { y: 120 },
      prev: { y: 10 },
      height: 10,
    },
  };
  const platform = { body: { top: 20 } };

  assert.equal(oneWayProcess(actor, platform), true);
  actor.body.gcaOneWayDropUntil = 280;
  assert.equal(oneWayProcess(actor, platform), false);
  actor.scene.time.now = 280;
  assert.equal(oneWayProcess(actor, platform), true);

  actor.body.velocity.y = -1;
  assert.equal(oneWayProcess(actor, platform), false);
});

test('genre examples keep delta-aware score and terminal game-over control flow', () => {
  const platform = readFileSync(
    join(implementReferences, 'platform.md'),
    'utf8',
  );
  assert.match(platform, /this\.score\s*=\s*0/);
  assert.match(
    platform,
    /this\.score\s*\+=\s*CONFIG\.autoScroll\.pointsPerSecond\s*\*\s*deltaSeconds/,
  );
  assert.doesNotMatch(platform, /this\.score\+\+/);

  const towerDefence = readFileSync(
    join(implementReferences, 'tower_defence.md'),
    'utf8',
  );
  assert.match(
    towerDefence,
    /const lives = Math\.max\(0, this\.tdState\.lives - enemy\.leakDamage\);/,
  );
  assert.match(
    towerDefence,
    /if\s*\(lives\s*===\s*0\)\s*this\.finishGame\(false\);/,
  );
});

test('finite particle recipes destroy on completion or timeout exactly once', async (t) => {
  const dir = runtimeFixture(t);
  installGlobalPhaser(t);
  const {
    dust,
    pickupGlow,
    projectileTrail,
    slashArc,
  } = await importFixtureModule(dir, 'particle-recipes.js');

  const burstRuntime = particleScene();
  const burst = dust(burstRuntime.scene, 10, 20);
  const burstTimer = burstRuntime.timers[0];
  assert.equal(burst.explodeCount, 3);
  assert.ok(burstTimer.delay > 400);
  assert.equal(burst.emit('complete'), true);
  assert.equal(burst.destroyCalls, 1);
  burstTimer.callback();
  assert.equal(burst.destroyCalls, 1);

  const durationRuntime = particleScene();
  const glow = pickupGlow(durationRuntime.scene, {});
  const durationTimer = durationRuntime.timers[0];
  assert.ok(durationTimer.delay > 600 + 400);
  assert.equal(glow.emit('complete'), true);
  assert.equal(glow.destroyCalls, 1);
  durationTimer.callback();
  assert.equal(glow.destroyCalls, 1);

  const timeoutRuntime = particleScene();
  const timedOutGlow = pickupGlow(timeoutRuntime.scene, {});
  timeoutRuntime.timers[0].callback();
  assert.equal(timedOutGlow.destroyCalls, 1);
  assert.equal(timedOutGlow.emit('complete'), false);
  assert.equal(timedOutGlow.destroyCalls, 1);

  const trailRuntime = particleScene();
  const trail = projectileTrail(trailRuntime.scene, {});
  assert.equal(trailRuntime.timers.length, 0);
  assert.equal(trail.listenerCount('complete'), 0);

  const geometryRuntime = particleScene();
  const slash = slashArc(geometryRuntime.scene, 0, 0, 45);
  assert.equal(slash.config.emitZone.source.constructor.name, 'Line');
});
