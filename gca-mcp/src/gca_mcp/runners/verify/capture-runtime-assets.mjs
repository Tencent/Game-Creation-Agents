#!/usr/bin/env node

import { parseArgs } from 'node:util';

import { chromium } from './load-playwright.mjs';
import {
  RUNTIME_IDENTITY_OPTIONS,
  verifyRuntimeIdentity,
} from './runtime-identity.mjs';
import { mergeRuntimeRegistrySnapshots } from './shared/gate-io.mjs';

const { values: args } = parseArgs({
  options: {
    url: { type: 'string' },
    width: { type: 'string', default: '1280' },
    height: { type: 'string', default: '720' },
    settle: { type: 'string', default: '400' },
    headed: { type: 'boolean', default: false },
    ...RUNTIME_IDENTITY_OPTIONS,
  },
});

if (!args.url) throw new Error('capture-runtime-assets requires --url');

const WIDTH = Number(args.width);
const HEIGHT = Number(args.height);
const SETTLE = Number(args.settle);

function runtimeBindingKey(binding) {
  return JSON.stringify([
    binding?.scene,
    binding?.texture_key,
    binding?.object_type,
    binding?.name,
    binding?.layout_key,
    binding?.bind,
    binding?.instance,
    binding?.visible,
  ]);
}

function canonicalRuntimeBindings(bindings) {
  return [...new Map(
    (bindings || []).map((binding) => [runtimeBindingKey(binding), binding]),
  ).values()].sort((a, b) => runtimeBindingKey(a).localeCompare(runtimeBindingKey(b)));
}

function collectSceneTextures(sceneKey) {
  const game = window.__phaserGame;
  const scene = game?.scene?.getScene(sceneKey);
  if (!scene) {
    return {
      scene: sceneKey,
      reachable: false,
      error: 'scene not found',
      texture_keys: [],
      texture_bindings: [],
    };
  }
  const textureKeys = new Set();
  const textureBindings = [];
  let textureInstance = 0;
  const sceneVisible = Boolean(scene.scene?.isVisible?.());
  const stringValue = (value) => (
    typeof value === 'string' && value ? value : null
  );
  const dataValue = (object, key) => {
    try {
      return typeof object?.getData === 'function' ? object.getData(key) : null;
    } catch {
      return null;
    }
  };
  const visit = (objects, parentVisible = sceneVisible) => {
    for (const object of objects || []) {
      if (!object) continue;
      const key = object?.texture?.key || object?.frame?.texture?.key;
      const visible = (
        parentVisible
        && object.visible !== false
        && (typeof object.alpha !== 'number' || object.alpha > 0)
      );
      if (typeof key === 'string' && key) {
        const instance = textureInstance;
        textureInstance += 1;
        if (visible) {
          textureKeys.add(key);
          textureBindings.push({
            scene: sceneKey,
            texture_key: key,
            object_type: String(object.type || object.constructor?.name || 'GameObject'),
            name: stringValue(object.name),
            layout_key: stringValue(
              object.layoutKey || dataValue(object, 'layoutKey'),
            ),
            bind: stringValue(object.bind || dataValue(object, 'bind')),
            instance,
            visible: true,
          });
        }
      }
      if (Array.isArray(object?.list)) visit(object.list, visible);
    }
  };
  visit(scene.children?.list);
  textureBindings.sort((a, b) => JSON.stringify([
    a.scene, a.texture_key, a.object_type, a.name, a.layout_key, a.bind,
    a.instance, a.visible,
  ]).localeCompare(JSON.stringify([
    b.scene, b.texture_key, b.object_type, b.name, b.layout_key, b.bind,
    b.instance, b.visible,
  ])));
  return {
    scene: sceneKey,
    reachable: true,
    active: Boolean(scene.scene?.isActive?.()),
    visible: Boolean(scene.scene?.isVisible?.()),
    display_object_count: scene.children?.list?.length || 0,
    texture_keys: [...textureKeys].sort(),
    texture_bindings: textureBindings,
  };
}

function collectRuntimeRegistry() {
  const game = window.__phaserGame;
  const loaded = typeof game.textures?.getTextureKeys === 'function'
    ? game.textures.getTextureKeys()
    : Object.keys(game.textures?.list || {});
  const animation = new Set();
  const animations = typeof game.anims?.getAll === 'function' ? game.anims.getAll() : [];
  for (const anim of animations || []) {
    for (const item of anim?.frames || []) {
      const key = item?.textureKey
        || item?.texture?.key
        || item?.frame?.texture?.key
        || item?.frame?.textureKey;
      if (typeof key === 'string' && key) animation.add(key);
    }
  }
  return {
    loaded_texture_keys: [...new Set(loaded.filter((key) => typeof key === 'string'))].sort(),
    animation_frame_texture_keys: [...animation].sort(),
    game_config: {
      pixelArt: game?.config?.pixelArt === true,
    },
  };
}

async function main() {
  const browser = await chromium.launch({ headless: !args.headed });
  try {
    const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await context.newPage();
    await page.goto(args.url, { waitUntil: 'domcontentloaded' });
    await verifyRuntimeIdentity(page, args);
    await page.waitForFunction(
      () => window.__phaserGame?.scene?.getScenes(false)?.length > 0,
      { timeout: 15000 },
    );
    await page.waitForTimeout(SETTLE);

    const initial = await page.evaluate(() => {
      const game = window.__phaserGame;
      return {
        active: game.scene.getScenes(true).map((scene) => scene.scene.key),
        registered: game.scene.getScenes(false).map((scene) => scene.scene.key),
      };
    });
    const initialRegistry = await page.evaluate(collectRuntimeRegistry);
    const registrySnapshots = [initialRegistry];
    const captureRegistrySnapshot = async () => {
      const snapshot = await page.evaluate(collectRuntimeRegistry);
      registrySnapshots.push(snapshot);
      return snapshot;
    };

    // Snapshot every currently active scene before probing other registered
    // scenes; starting a boot/menu scene can otherwise replace the live scene
    // and erase the display-list evidence we intended to capture.
    const initialCaptures = new Map();
    for (const sceneKey of initial.active) {
      initialCaptures.set(
        sceneKey,
        await page.evaluate(collectSceneTextures, sceneKey),
      );
      await captureRegistrySnapshot();
    }

    const scenes = [];
    for (const sceneKey of initial.registered) {
      let started = false;
      const isActive = initial.active.includes(sceneKey);
      let captured = initialCaptures.get(sceneKey);
      if (!isActive) {
        const result = await page.evaluate((key) => {
          const game = window.__phaserGame;
          try {
            game.scene.start(key);
            return { ok: true };
          } catch (error) {
            return { ok: false, error: String(error?.message || error) };
          }
        }, sceneKey);
        if (!result.ok) {
          scenes.push({
            scene: sceneKey,
            reachable: false,
            error: result.error,
            texture_keys: [],
            texture_bindings: [],
          });
          continue;
        }
        started = true;
        const immediate = await page.evaluate(collectSceneTextures, sceneKey);
        await captureRegistrySnapshot();
        await page.waitForTimeout(SETTLE);
        const settled = await page.evaluate(collectSceneTextures, sceneKey);
        await captureRegistrySnapshot();
        const bindings = canonicalRuntimeBindings([
          ...(immediate.texture_bindings || []),
          ...(settled.texture_bindings || []),
        ]);
        captured = {
          ...settled,
          reachable: Boolean(
            immediate.active
            || immediate.visible
            || settled.active
            || settled.visible
          ),
          texture_keys: [...new Set(
            bindings.map((binding) => binding.texture_key),
          )].sort(),
          texture_bindings: bindings,
        };
        if (!captured.reachable) {
          captured.error = 'scene did not become active or visible after start';
        }
      }
      if (!captured) {
        captured = await page.evaluate(collectSceneTextures, sceneKey);
        await captureRegistrySnapshot();
      }
      captured.initially_active = isActive;
      captured.started_for_capture = started;
      scenes.push(captured);
      if (started) {
        await page.evaluate((key) => {
          try { window.__phaserGame.scene.stop(key); } catch { /* ignore */ }
        }, sceneKey);
      }
    }

    const runtimeIdentity = await verifyRuntimeIdentity(page, args);
    const finalRegistry = await page.evaluate(collectRuntimeRegistry);
    registrySnapshots.push(finalRegistry);
    const mergedRegistry = mergeRuntimeRegistrySnapshots(registrySnapshots);
    const registry = {
      loaded_texture_keys: mergedRegistry.loaded_texture_keys,
      animation_frame_texture_keys: mergedRegistry.animation_frame_texture_keys,
      game_config: finalRegistry.game_config,
    };
    const displayBindings = canonicalRuntimeBindings(
      scenes.flatMap((scene) => scene.texture_bindings || []),
    );
    const displayKeys = new Set(
      displayBindings.map((binding) => binding.texture_key),
    );
    const reachable = scenes
      .filter((scene) => scene.reachable)
      .map((scene) => scene.scene);

    console.log(JSON.stringify({
      schema_version: 1,
      source: 'capture-runtime-assets.mjs',
      generated_at: new Date().toISOString(),
      url: args.url,
      runtime_identity: runtimeIdentity,
      loaded_texture_keys: registry.loaded_texture_keys,
      animation_frame_texture_keys: registry.animation_frame_texture_keys,
      display_list_texture_keys: [...displayKeys].sort(),
      display_object_texture_bindings: displayBindings,
      game_config: registry.game_config,
      active_scene_keys: [...new Set(initial.active)].sort(),
      registered_scene_keys: [...new Set(initial.registered)].sort(),
      reachable_scene_keys: [...new Set(reachable)].sort(),
      scenes,
    }, null, 2));

    await context.close();
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(`capture-runtime-assets: ${error.stack || error.message}`);
  process.exit(1);
});
