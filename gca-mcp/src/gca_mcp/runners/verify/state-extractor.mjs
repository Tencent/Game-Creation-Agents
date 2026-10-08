/**
 * state-extractor.mjs — Universal Phaser game state extraction.
 *
 * Injects into page.evaluate() to read structured game state from any Phaser game
 * without game-specific code. Auto-discovers scenes, objects, groups, physics,
 * and custom scene properties.
 *
 * Requirements:
 *   - Game must expose Phaser.Game instance as window.__phaserGame
 *   - Custom game state should be stored as scene instance properties or
 *     GameObject.data values (not closure variables)
 *
 * Usage:
 *   import { EXTRACTOR, extractState, extractStateCompact } from './state-extractor.mjs';
 *
 *   // Full state (for analysis)
 *   const state = await page.evaluate(EXTRACTOR);
 *
 *   // Or via helper (handles error wrapping)
 *   const state = await extractState(page);
 *
 *   // Compact state (for test assertions — smaller payload)
 *   const state = await extractStateCompact(page);
 */

// ---------------------------------------------------------------------------
// Phaser built-in scene properties to exclude from custom property discovery.
// These exist on every Scene instance and are not game-specific state.
// ---------------------------------------------------------------------------
const SCENE_BUILTINS = new Set([
  // Phaser.Scene core
  'sys', 'game', 'anims', 'cache', 'registry', 'sound', 'textures',
  'events', 'cameras', 'add', 'make', 'scene', 'children', 'lights',
  'data', 'input', 'load', 'time', 'tweens', 'physics', 'matter',
  'plugins', 'renderer', 'scale', 'facebook',
  // Common Phaser internals
  '_events', '_eventsCount', '_maxListeners',
  // Lifecycle methods — these are functions, filtered anyway, but list for clarity
  'init', 'preload', 'create', 'update',
]);

// ---------------------------------------------------------------------------
// EXTRACTOR — the function injected into page.evaluate()
// Returns a complete game state snapshot or { error: string }.
// ---------------------------------------------------------------------------
export const EXTRACTOR = function () {
  try {
    const game = window.__phaserGame;
    if (!game) return { error: 'no __phaserGame on window' };
    if (!game.scene) return { error: '__phaserGame has no scene manager' };

    const BUILTINS = new Set([
      'sys', 'game', 'anims', 'cache', 'registry', 'sound', 'textures',
      'events', 'cameras', 'add', 'make', 'scene', 'children', 'lights',
      'data', 'input', 'load', 'time', 'tweens', 'physics', 'matter',
      'plugins', 'renderer', 'scale', 'facebook',
      '_events', '_eventsCount', '_maxListeners',
      'init', 'preload', 'create', 'update',
    ]);

    const state = {
      timestamp: Date.now(),
      fps: Math.round(game.loop.actualFps),
      resolution: {
        width: game.config.width,
        height: game.config.height,
      },
      activeSceneKeys: [],
      scenes: {},
      totalObjectCount: 0,
      consoleErrors: window.__gameVerifyErrors || [],
    };

    // ---- Helper: safe shallow serialize ----
    function safeSerialize(val, depth) {
      if (depth > 3) return '[deep]';
      if (val === null || val === undefined) return val;
      const t = typeof val;
      if (t === 'number' || t === 'string' || t === 'boolean') return val;
      if (t === 'function') return undefined; // skip functions
      if (Array.isArray(val)) {
        if (val.length > 50) return `[Array(${val.length})]`;
        return val.map(v => safeSerialize(v, depth + 1)).filter(v => v !== undefined);
      }
      if (t === 'object') {
        // Skip Phaser objects (scenes, game objects, etc.)
        if (val.scene && val.type) return undefined; // GameObject
        if (val.sys && val.scene) return undefined;  // Scene
        if (val.world && val.config) return undefined; // Physics
        const out = {};
        let count = 0;
        for (const [k, v] of Object.entries(val)) {
          if (count > 30) { out['...'] = `${Object.keys(val).length - 30} more`; break; }
          if (k.startsWith('_')) continue;
          const sv = safeSerialize(v, depth + 1);
          if (sv !== undefined) { out[k] = sv; count++; }
        }
        return Object.keys(out).length > 0 ? out : undefined;
      }
      return undefined;
    }

    // ---- Helper: extract one GameObject ----
    function extractObject(obj) {
      const entry = {
        type: obj.type,                           // Sprite, Text, Image, Rectangle...
        name: obj.name || undefined,
        texture: obj.texture?.key || undefined,
        x: Math.round(obj.x * 100) / 100,
        y: Math.round(obj.y * 100) / 100,
        width: obj.width ? Math.round(obj.width) : undefined,
        height: obj.height ? Math.round(obj.height) : undefined,
        active: obj.active,
        visible: obj.visible,
        depth: obj.depth || 0,
        alpha: obj.alpha !== undefined ? Math.round(obj.alpha * 100) / 100 : undefined,
        scale: (obj.scaleX !== 1 || obj.scaleY !== 1)
          ? { x: Math.round(obj.scaleX * 100) / 100, y: Math.round(obj.scaleY * 100) / 100 }
          : undefined,
      };

      // Physics body
      if (obj.body) {
        entry.body = {
          velocity: {
            x: Math.round((obj.body.velocity?.x ?? 0) * 100) / 100,
            y: Math.round((obj.body.velocity?.y ?? 0) * 100) / 100,
          },
          acceleration: {
            x: Math.round((obj.body.acceleration?.x ?? 0) * 100) / 100,
            y: Math.round((obj.body.acceleration?.y ?? 0) * 100) / 100,
          },
          blocked: obj.body.blocked ? {
            up: obj.body.blocked.up,
            down: obj.body.blocked.down,
            left: obj.body.blocked.left,
            right: obj.body.blocked.right,
          } : undefined,
          isStatic: obj.body.isStatic || obj.body.immovable || false,
          enable: obj.body.enable !== undefined ? obj.body.enable : true,
        };
      }

      // DataManager values (game-specific data attached to objects)
      if (obj.data && obj.data.values && Object.keys(obj.data.values).length > 0) {
        entry.data = {};
        for (const [k, v] of Object.entries(obj.data.values)) {
          const sv = safeSerialize(v, 0);
          if (sv !== undefined) entry.data[k] = sv;
        }
      }

      // Common game-specific properties found directly on sprites
      // (e.g., enemy.health, enemy.enemyType, pickup.pickupType)
      const commonProps = [
        'health', 'maxHealth', 'damage', 'speed',
        'enemyType', 'pickupType', 'itemType',
        'lastHitTime', 'lastTouchTime',
      ];
      for (const prop of commonProps) {
        if (obj[prop] !== undefined && typeof obj[prop] !== 'function') {
          if (!entry.data) entry.data = {};
          entry.data[prop] = obj[prop];
        }
      }

      // Clean undefined fields
      for (const k of Object.keys(entry)) {
        if (entry[k] === undefined) delete entry[k];
      }

      return entry;
    }

    // ---- Process each active scene ----
    const activeScenes = game.scene.getScenes(true);

    for (const scene of activeScenes) {
      const key = scene.scene.key;
      state.activeSceneKeys.push(key);

      const sceneState = {
        objects: [],
        objectsByType: {},
        objectsByTexture: {},
        groups: {},
        custom: {},
      };

      // ---- All GameObjects ----
      if (scene.children && scene.children.list) {
        for (const obj of scene.children.list) {
          const entry = extractObject(obj);
          sceneState.objects.push(entry);
          state.totalObjectCount++;

          // Index by type
          const t = entry.type || 'Unknown';
          if (!sceneState.objectsByType[t]) sceneState.objectsByType[t] = 0;
          sceneState.objectsByType[t]++;

          // Index by texture
          if (entry.texture) {
            if (!sceneState.objectsByTexture[entry.texture]) sceneState.objectsByTexture[entry.texture] = 0;
            sceneState.objectsByTexture[entry.texture]++;
          }
        }
      }

      // ---- Groups (auto-discover) ----
      for (const [prop, val] of Object.entries(scene)) {
        if (!val || typeof val !== 'object') continue;
        if (BUILTINS.has(prop)) continue;
        // Detect Phaser Group: has getChildren and countActive
        if (typeof val.getChildren === 'function' && typeof val.countActive === 'function') {
          sceneState.groups[prop] = {
            total: val.getLength(),
            active: val.countActive(true),
            inactive: val.countActive(false),
          };
        }
      }

      // ---- Custom scene properties (game-specific state) ----
      for (const [prop, val] of Object.entries(scene)) {
        if (BUILTINS.has(prop)) continue;
        if (prop.startsWith('_')) continue;
        if (typeof val === 'function') continue;

        // Skip Phaser objects already captured
        if (val && typeof val === 'object') {
          // Skip Groups (already in groups section)
          if (typeof val.getChildren === 'function') continue;
          // Skip GameObjects
          if (val.scene && val.type) continue;
          // Skip Phaser Input objects
          if (val.manager && val.enabled !== undefined) continue;
          // Skip Phaser Key objects & CursorKeys (e.g. scene.cursors, scene.wasd)
          // Key objects have .keyCode + .plugin; CursorKeys containers have Key-valued props
          if (val.keyCode !== undefined && val.plugin) continue;
          if (val.up && val.up.keyCode !== undefined) continue; // CursorKeys
          // Check if it's a WASD-style key map (object whose values are all Phaser Keys)
          const valEntries = Object.entries(val);
          if (valEntries.length > 0 && valEntries.length <= 10 &&
              valEntries.every(([, v]) => v && typeof v === 'object' && v.keyCode !== undefined)) continue;
        }

        // Serialize value
        if (['number', 'string', 'boolean'].includes(typeof val)) {
          sceneState.custom[prop] = val;
        } else if (val === null) {
          sceneState.custom[prop] = null;
        } else if (Array.isArray(val)) {
          const sv = safeSerialize(val, 0);
          // Skip empty arrays (often arrays of GameObjects that all got filtered)
          if (sv !== undefined && !(Array.isArray(sv) && sv.length === 0)) {
            sceneState.custom[prop] = sv;
          }
        } else if (typeof val === 'object') {
          // Attempt shallow extraction of non-Phaser objects
          // (e.g., scene.stats = { maxHealth: 100, damage: 10 })
          try {
            const shallow = {};
            let count = 0;
            for (const [k, v] of Object.entries(val)) {
              if (count > 30) break;
              if (k.startsWith('_')) continue;
              if (typeof v === 'function') continue;
              // Skip nested Phaser objects
              if (v && typeof v === 'object' && (v.scene || v.type || v.sys)) continue;
              if (v && typeof v === 'object' && typeof v.getChildren === 'function') continue;
              const sv = safeSerialize(v, 0);
              if (sv !== undefined) { shallow[k] = sv; count++; }
            }
            if (Object.keys(shallow).length > 0) {
              sceneState.custom[prop] = shallow;
            }
          } catch { /* circular ref or other issue — skip */ }
        }
      }

      state.scenes[key] = sceneState;
    }

    // ---- All registered scene keys (including inactive) ----
    state.allSceneKeys = game.scene.keys
      ? Object.keys(game.scene.keys)
      : game.scene.getScenes(false).map(s => s.scene.key);

    return state;

  } catch (e) {
    return { error: e.message, stack: e.stack };
  }
};

// ---------------------------------------------------------------------------
// COMPACT_EXTRACTOR — smaller payload for test assertions.
// Only returns: active scenes, custom properties, group counts, fps.
// Skips individual object enumeration (saves bandwidth on object-heavy games).
// ---------------------------------------------------------------------------
export const COMPACT_EXTRACTOR = function () {
  try {
    const game = window.__phaserGame;
    if (!game) return { error: 'no __phaserGame on window' };

    const BUILTINS = new Set([
      'sys', 'game', 'anims', 'cache', 'registry', 'sound', 'textures',
      'events', 'cameras', 'add', 'make', 'scene', 'children', 'lights',
      'data', 'input', 'load', 'time', 'tweens', 'physics', 'matter',
      'plugins', 'renderer', 'scale', 'facebook',
      '_events', '_eventsCount', '_maxListeners',
      'init', 'preload', 'create', 'update',
    ]);

    function safeSerialize(val, depth) {
      if (depth > 2) return '[deep]';
      if (val === null || val === undefined) return val;
      const t = typeof val;
      if (t === 'number' || t === 'string' || t === 'boolean') return val;
      if (t === 'function') return undefined;
      if (Array.isArray(val)) {
        if (val.length > 20) return `[Array(${val.length})]`;
        return val.map(v => safeSerialize(v, depth + 1)).filter(v => v !== undefined);
      }
      if (t === 'object') {
        if (val.scene && val.type) return undefined;
        if (val.sys && val.scene) return undefined;
        const out = {};
        let count = 0;
        for (const [k, v] of Object.entries(val)) {
          if (count > 20) break;
          if (k.startsWith('_')) continue;
          const sv = safeSerialize(v, depth + 1);
          if (sv !== undefined) { out[k] = sv; count++; }
        }
        return Object.keys(out).length > 0 ? out : undefined;
      }
      return undefined;
    }

    const state = {
      ts: Date.now(),
      fps: Math.round(game.loop.actualFps),
      scenes: {},
      objectCount: 0,
    };

    for (const scene of game.scene.getScenes(true)) {
      const key = scene.scene.key;
      const s = { groups: {}, custom: {} };

      // Object count only
      if (scene.children?.list) state.objectCount += scene.children.list.length;

      // Groups
      for (const [prop, val] of Object.entries(scene)) {
        if (BUILTINS.has(prop) || prop.startsWith('_')) continue;
        if (val && typeof val.getChildren === 'function' && typeof val.countActive === 'function') {
          s.groups[prop] = { total: val.getLength(), active: val.countActive(true) };
        }
      }

      // Custom properties
      for (const [prop, val] of Object.entries(scene)) {
        if (BUILTINS.has(prop) || prop.startsWith('_')) continue;
        if (typeof val === 'function') continue;
        if (val && typeof val === 'object') {
          if (typeof val.getChildren === 'function') continue;
          if (val.scene && val.type) continue;
          if (val.manager && val.enabled !== undefined) continue;
          // Skip Phaser Key objects & CursorKeys
          if (val.keyCode !== undefined && val.plugin) continue;
          if (val.up && val.up.keyCode !== undefined) continue;
          const valEntries = Object.entries(val);
          if (valEntries.length > 0 && valEntries.length <= 10 &&
              valEntries.every(([, v]) => v && typeof v === 'object' && v.keyCode !== undefined)) continue;
        }
        if (['number', 'string', 'boolean'].includes(typeof val) || val === null) {
          s.custom[prop] = val;
        } else if (Array.isArray(val)) {
          const sv = safeSerialize(val, 0);
          if (sv !== undefined && !(Array.isArray(sv) && sv.length === 0)) {
            s.custom[prop] = sv;
          }
        } else if (typeof val === 'object' && val) {
          const sv = safeSerialize(val, 0);
          if (sv !== undefined) s.custom[prop] = sv;
        }
      }

      state.scenes[key] = s;
    }

    return state;
  } catch (e) {
    return { error: e.message };
  }
};

// ---------------------------------------------------------------------------
// Helper: extract a single named object's state (e.g., 'player')
// Looks for scene[propName] that is a GameObject.
// ---------------------------------------------------------------------------
export const OBJECT_EXTRACTOR = function (sceneName, propName) {
  try {
    const game = window.__phaserGame;
    if (!game) return { error: 'no __phaserGame' };

    const scene = game.scene.getScene(sceneName);
    if (!scene) return { error: `scene '${sceneName}' not found` };

    const obj = scene[propName];
    if (!obj) return { error: `property '${propName}' not found on scene '${sceneName}'` };

    const result = {
      x: obj.x, y: obj.y,
      active: obj.active, visible: obj.visible,
    };

    if (obj.body) {
      result.velocity = { x: obj.body.velocity?.x, y: obj.body.velocity?.y };
      result.blocked = obj.body.blocked ? { ...obj.body.blocked } : undefined;
    }

    // All own enumerable non-function, non-Phaser properties
    for (const [k, v] of Object.entries(obj)) {
      if (k.startsWith('_')) continue;
      if (['scene', 'body', 'data', 'input', 'texture', 'anims', 'parentContainer'].includes(k)) continue;
      if (typeof v === 'function') continue;
      if (typeof v === 'object' && v !== null) continue;
      result[k] = v;
    }

    // Data manager
    if (obj.data?.values) {
      result.data = { ...obj.data.values };
    }

    return result;
  } catch (e) {
    return { error: e.message };
  }
};

// ---------------------------------------------------------------------------
// Convenience wrappers for Playwright page context
// ---------------------------------------------------------------------------

/**
 * Extract full game state via Playwright page.
 * @param {import('playwright').Page} page
 * @returns {Promise<object>} Game state snapshot
 */
export async function extractState(page) {
  return page.evaluate(EXTRACTOR);
}

/**
 * Extract compact game state (for assertions).
 * @param {import('playwright').Page} page
 * @returns {Promise<object>} Compact state
 */
export async function extractStateCompact(page) {
  return page.evaluate(COMPACT_EXTRACTOR);
}

/**
 * Extract a specific object's state from a named scene property.
 * @param {import('playwright').Page} page
 * @param {string} sceneName  Scene key (e.g., 'ArenaScene')
 * @param {string} propName   Property name on scene (e.g., 'player')
 * @returns {Promise<object>} Object state
 */
export async function extractObject(page, sceneName, propName) {
  return page.evaluate(OBJECT_EXTRACTOR, sceneName, propName);
}

/**
 * Install console error capture on a page.
 * Call BEFORE page.goto(). Errors become available in state.consoleErrors.
 * @param {import('playwright').Page} page
 */
export async function installErrorCapture(page) {
  await page.addInitScript(() => {
    window.__gameVerifyErrors = [];
    const origError = console.error;
    console.error = function (...args) {
      window.__gameVerifyErrors.push(args.map(String).join(' '));
      origError.apply(console, args);
    };
    window.addEventListener('error', (e) => {
      window.__gameVerifyErrors.push(`[uncaught] ${e.message} at ${e.filename}:${e.lineno}`);
    });
    window.addEventListener('unhandledrejection', (e) => {
      window.__gameVerifyErrors.push(`[unhandled rejection] ${e.reason}`);
    });
  });
}

/**
 * Wait for Phaser game to be fully booted and first scene active.
 * @param {import('playwright').Page} page
 * @param {number} timeout  Max wait in ms (default 10000)
 */
export async function waitForGame(page, timeout = 10000) {
  await page.waitForFunction(
    () => {
      const g = window.__phaserGame;
      return g && g.scene && g.scene.getScenes(true).length > 0;
    },
    { timeout }
  );
  // Extra settle time for first render
  await page.waitForTimeout(500);
}
