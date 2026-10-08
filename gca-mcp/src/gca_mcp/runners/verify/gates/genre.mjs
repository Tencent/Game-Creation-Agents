/** Genre and interaction gates — mechanically extracted gate domain. */

import {
  readFileSync,
  existsSync,
  readdirSync,
} from 'fs';

import { join } from 'path';

import {
  resolveRead,
  DISPLAY_PARAMS,
  INTERACTION_CONTRACT,
} from '../playtest-layout.mjs';
import { validateJsonSchema } from '../json-schema.mjs';
import {
  readJSON,
  listFiles,
  isFiniteNumber,
} from '../shared/gate-io.mjs';
import {
  gameDir,
  playtestDir,
  fail,
  fileExists,
} from '../gate-context.mjs';
import {
  readGcaConfig,
  canonicalPipelineReference,
  validateInteractionRouting,
  isNonEmptyRecord,
  hasConsumableStagePayload,
  resolveEffectiveStageMapLayout,
  qualityPolicy,
  readCanvasSize,
  normalizeDisplayParams,
  worldBodyDimensions,
  worldVisibleDimensions,
  normalizeGenre,
  detectGenre,
  isTowerDefence,
  usesAuthoredLevels,
  isPhysicsGame,
  tdConfig,
  readSrcText,
  stripJsComments,
  manifestEntries,
} from './support.mjs';

import { gateEntityScale } from './assets.mjs';

function gateLevelsPresent(state) {
  if (state?.level_model !== 'authored') return;
  const stateGenre = state?.genre;
  if (!normalizeGenre(stateGenre)) {
    fail(`state.json genre '${stateGenre ?? '<missing>'}' is invalid for level_model="authored"`);
  }
  const levelsDir = join(gameDir, 'levels');
  if (!existsSync(levelsDir)) {
    fail('levels/ missing although state requires authored Level DSL content');
    return;
  }

  const canonicalSchemaPath = canonicalPipelineReference('level-design', 'level_schema.json');
  if (!canonicalSchemaPath) {
    fail('canonical level_schema.json missing from the repository');
  }

  const schema = canonicalSchemaPath ? readJSON(canonicalSchemaPath) : null;
  const expectedRequired = ['level_id', 'genre', 'length', 'win_condition', 'lose_condition'];
  if (!schema || schema.$id !== 'https://gca.harnessrl.dev/schemas/level.json') {
    fail('canonical level_schema.json is invalid or has the wrong $id');
  } else {
    for (const key of expectedRequired) {
      if (!schema.required?.includes(key) || !schema.properties?.[key]) {
        fail(`canonical level_schema.json is missing contract '${key}'`);
      }
    }
  }

  const stageNames = readdirSync(levelsDir)
    .filter((name) => /^stage_\d+\.json$/.test(name))
    .sort((a, b) => Number(a.match(/\d+/)?.[0]) - Number(b.match(/\d+/)?.[0]));
  if (stageNames.length === 0) fail('No levels/stage_*.json files found');

  const stages = [];
  const ids = new Set();
  const unlockModes = new Set();
  for (const name of stageNames) {
    const path = join(levelsDir, name);
    const stage = readJSON(path);
    if (!stage) {
      fail(`levels/${name} is invalid JSON`);
      continue;
    }
    if (schema) {
      const errors = validateJsonSchema(stage, schema, name);
      if (errors.length) {
        fail(`levels/${name} violates canonical level_schema.json: ${errors.slice(0, 8).join('; ')}${errors.length > 8 ? `; +${errors.length - 8} more` : ''}`);
      }
    }
    if (stage.genre !== stateGenre) {
      fail(
        `levels/${name}: genre '${stage.genre ?? '<missing>'}' must exactly equal `
        + `state.json genre '${stateGenre ?? '<missing>'}'`,
      );
    }
    const contentRequired = ['puzzle', 'card', 'idle'].includes(stage.genre);
    const hasContent = stage.content !== undefined;
    if (contentRequired && !hasContent) {
      fail(
        `levels/${name}: ${stage.genre} authored stages require a non-empty `
        + 'content envelope with {kind,data}',
      );
    }
    if (hasContent || contentRequired) {
      if (
        typeof stage.content?.kind !== 'string'
        || !stage.content.kind.trim()
        || !isNonEmptyRecord(stage.content?.data)
      ) {
        fail(`levels/${name}: content must be {kind: non-empty string, data: non-empty object}`);
      }
    }
    if (!hasConsumableStagePayload(stage)) {
      fail(
        `levels/${name}: authored stage is an empty shell; add at least one `
        + 'consumable terrain/spawn/boss/map/content/starter/scroll payload',
      );
    }

    const expectedId = name.replace(/\.json$/, '');
    if (stage.level_id !== expectedId) {
      fail(`levels/${name}: level_id must equal '${expectedId}'`);
    }
    if (typeof stage.level_id === 'string') {
      if (ids.has(stage.level_id)) fail(`duplicate level_id '${stage.level_id}'`);
      ids.add(stage.level_id);
    }
    unlockModes.add(stage.unlock_mode || 'sequential');

    const spawnIds = new Set();
    let priorAt = -Infinity;
    for (const [index, spawn] of (Array.isArray(stage.spawns) ? stage.spawns : []).entries()) {
      if (spawn?.id) {
        if (spawnIds.has(spawn.id)) fail(`levels/${name}: duplicate spawn id '${spawn.id}'`);
        spawnIds.add(spawn.id);
      }
      if (typeof spawn?.at === 'number') {
        if (spawn.at < priorAt) fail(`levels/${name}: spawns[${index}].at=${spawn.at} is not monotonic`);
        priorAt = spawn.at;
      }
    }
    stages.push({ name, stage });
  }

  if (unlockModes.size > 1) fail('all authored levels must use the same unlock_mode');
  for (let index = 0; index < stages.length; index += 1) {
    const { name, stage } = stages[index];
    if (typeof stage.next_level === 'string' && !ids.has(stage.next_level)) {
      fail(`levels/${name}: next_level '${stage.next_level}' does not resolve`);
    }
    if ((stage.unlock_mode || 'sequential') === 'sequential') {
      const expectedNext = stages[index + 1]?.stage?.level_id ?? null;
      if ((stage.next_level ?? null) !== expectedNext) {
        fail(`levels/${name}: sequential next_level must be ${expectedNext === null ? 'null/omitted' : `'${expectedNext}'`}`);
      }
    }
  }

  const mapSchemaPath = canonicalPipelineReference('level-design', 'map_layout_schema.json');
  const mapSchema = mapSchemaPath ? readJSON(mapSchemaPath) : null;
  const effectiveLayouts = new Map();
  const validatedSources = new Set();
  for (const { name, stage } of stages) {
    if (
      stage.genre !== 'tower_defence'
      && !Object.hasOwn(stage, 'map_layout')
      && !Object.hasOwn(stage, 'map_layout_ref')
    ) {
      continue;
    }
    const resolvedLayout = resolveEffectiveStageMapLayout(levelsDir, name, stage);
    if (resolvedLayout.unsafe) {
      fail(`levels/${name}: map_layout_ref escapes GAME_DIR: ${resolvedLayout.source}`);
      continue;
    }
    if (resolvedLayout.missing) {
      fail(`levels/${name}: effective map layout '${resolvedLayout.source}' is missing`);
      continue;
    }
    if (resolvedLayout.invalid || !resolvedLayout.layout) {
      fail(`levels/${name}: effective map layout '${resolvedLayout.source}' is invalid JSON/object`);
      continue;
    }
    const { layout, source } = resolvedLayout;
    if (!validatedSources.has(source)) {
      validatedSources.add(source);
      if (!mapSchema) {
        fail('canonical map_layout_schema.json missing from the repository');
      } else {
        const errors = validateJsonSchema(layout, mapSchema, source);
        if (errors.length) {
          fail(`level extension '${source}' violates map_layout_schema.json: ${errors.slice(0, 8).join('; ')}${errors.length > 8 ? `; +${errors.length - 8} more` : ''}`);
        }
      }
    }
    effectiveLayouts.set(name, layout);
  }

  for (const { name, stage } of stages) {
    const layout = effectiveLayouts.get(name);
    if (!layout) continue;
    const paths = new Map((layout.paths || []).map((entry) => [entry.id, entry]));
    for (const [index, spawn] of (stage.spawns || []).entries()) {
      if (!spawn.path) continue;
      const route = paths.get(spawn.path);
      if (!route) {
        fail(`levels/${name}: spawns[${index}].path '${spawn.path}' is not declared by its map layout`);
        continue;
      }
      if (!Number.isInteger(spawn.wave)) {
        fail(`levels/${name}: spawns[${index}] using path '${spawn.path}' needs an integer wave`);
      } else if (spawn.wave < (route.unlocked_at_wave || 1)) {
        fail(`levels/${name}: wave ${spawn.wave} uses path '${spawn.path}' before unlocked_at_wave ${route.unlocked_at_wave || 1}`);
      }
    }
  }
}

function gateLevelRuntime(state) {
  if (state?.level_model !== 'authored') return;
  const levelsDir = join(gameDir, 'levels');
  if (!existsSync(levelsDir)) return;
  const loader = join(gameDir, 'src/level/LevelLoader.js');
  if (!fileExists(loader)) {
    fail('src/level/LevelLoader.js missing for level_model="authored"');
    return;
  }

  const contentStageNames = readdirSync(levelsDir)
    .filter((name) => /^stage_\d+\.json$/.test(name))
    .filter((name) => readJSON(join(levelsDir, name))?.content !== undefined);
  const loaderSource = stripJsComments(readFileSync(loader, 'utf-8'));
  if (loaderSource.includes(gameDir)) {
    fail(
      'src/level/LevelLoader.js embeds an absolute GAME_DIR path; '
      + 'browser loading must use game-relative levels/<basename> URLs',
    );
  }
  if (contentStageNames.length > 0) {
    if (!/\bcontent\b/.test(loaderSource) || !/\bkind\b/.test(loaderSource)) {
      fail(
        'src/level/LevelLoader.js does not consume the authored content.kind '
        + `envelope required by ${contentStageNames.join(', ')}`,
      );
    }
  }
}
// ── Tower-defense asset↔implementation consistency gates ───────────────
// Enforce the single-source-of-truth contract: gameplay-authoritative map
// geometry (path/core/spawns/build slots) lives in levels/map-layout.json and
// is rendered in code (not baked into the art); tower names/costs live in
// CONFIG and are runtime text over a value-less panel. See
// references/implement/tower_defence.md + references/level-design/map_layout_schema.json.
function loadMapLayoutsForGate() {
  const levelsDir = join(gameDir, 'levels');
  if (!existsSync(levelsDir)) return [];
  return readdirSync(levelsDir)
    .filter((name) => /^stage_\d+\.json$/.test(name))
    .sort((a, b) => Number(a.match(/\d+/)?.[0]) - Number(b.match(/\d+/)?.[0]))
    .map((stageName) => {
      const stage = readJSON(join(levelsDir, stageName));
      if (!stage) {
        return {
          stageName,
          layout: null,
          source: `levels/${stageName}`,
          invalid: true,
          missing: false,
        };
      }
      return {
        stageName,
        ...resolveEffectiveStageMapLayout(levelsDir, stageName, stage),
      };
    });
}

function distToSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function mapLayoutInvariantErrors(layout, cfg) {
  const tol = Number(cfg.endpoint_tolerance_px ?? 2);
  const slotMargin = Number(cfg.build_slot_margin_px ?? 8);
  const canvasTol = Number(cfg.canvas_tolerance_px ?? 2);
  const allowOnPath = cfg.allow_on_path_build_slots !== false;
  const errs = [];

  const canvas = layout?.canvas;
  const W = canvas?.width;
  const H = canvas?.height;
  if (!canvas || !isFiniteNumber(W) || !isFiniteNumber(H) || W <= 0 || H <= 0) {
    errs.push('canvas.width/height missing or non-positive');
  }
  const expectedCanvas = readCanvasSize();
  if (
    W > 0
    && H > 0
    && expectedCanvas
    && (
      Math.abs(W - expectedCanvas.w) > canvasTol
      || Math.abs(H - expectedCanvas.h) > canvasTol
    )
  ) {
    errs.push(
      `canvas ${W}x${H} does not match game canvas `
      + `${expectedCanvas.w}x${expectedCanvas.h}`,
    );
  }
  const outsideCanvas = (x, y, marginX = 0, marginY = 0) => (
    W > 0
    && H > 0
    && (
      x - marginX < -canvasTol
      || y - marginY < -canvasTol
      || x + marginX > W + canvasTol
      || y + marginY > H + canvasTol
    )
  );

  const core = layout?.core;
  if (!core || !isFiniteNumber(core.x) || !isFiniteNumber(core.y)) {
    errs.push('core.{x,y} missing');
  } else if (outsideCanvas(core.x, core.y)) {
    errs.push(`core (${core.x},${core.y}) outside canvas ${W}x${H}`);
  }

  const spawns = Array.isArray(layout?.spawns) ? layout.spawns : [];
  if (spawns.length < 1) errs.push('spawns[] must have >= 1 entry');
  const spawnById = {};
  for (const spawn of spawns) {
    if (
      !spawn
      || typeof spawn.id !== 'string'
      || !spawn.id
      || !isFiniteNumber(spawn.x)
      || !isFiniteNumber(spawn.y)
    ) {
      errs.push('each spawn needs {id,x,y}');
      continue;
    }
    if (spawnById[spawn.id]) errs.push(`spawn id '${spawn.id}' is duplicated`);
    spawnById[spawn.id] = spawn;
    if (outsideCanvas(spawn.x, spawn.y)) {
      errs.push(`spawn '${spawn.id}' (${spawn.x},${spawn.y}) outside canvas ${W}x${H}`);
    }
  }

  const paths = Array.isArray(layout?.paths) ? layout.paths : [];
  if (paths.length < 1) errs.push('paths[] must have >= 1 route');
  const pathIds = new Set();
  const near = (a, b) => Math.abs(a - b) <= tol;
  for (const path of paths) {
    const id = (path && path.id) || '?';
    if (pathIds.has(id)) errs.push(`path id '${id}' is duplicated`);
    pathIds.add(id);
    if (!path || !Array.isArray(path.waypoints) || path.waypoints.length < 2) {
      errs.push(`path '${id}': needs >= 2 waypoints`);
      continue;
    }
    const waypoints = path.waypoints;
    if (waypoints.some((point) => !point || !isFiniteNumber(point.x) || !isFiniteNumber(point.y))) {
      errs.push(`path '${id}': a waypoint is missing x/y`);
      continue;
    }
    const spawn = spawnById[path.spawn];
    if (!spawn) {
      errs.push(`path '${id}': spawn '${path.spawn}' not declared in spawns[]`);
    } else if (
      !near(waypoints[0].x, spawn.x)
      || !near(waypoints[0].y, spawn.y)
    ) {
      errs.push(
        `path '${id}': waypoints[0] (${waypoints[0].x},${waypoints[0].y}) `
        + `!= spawn '${path.spawn}' (${spawn.x},${spawn.y})`,
      );
    }
    const endpoint = waypoints[waypoints.length - 1];
    if (
      core
      && isFiniteNumber(core.x)
      && isFiniteNumber(core.y)
      && (!near(endpoint.x, core.x) || !near(endpoint.y, core.y))
    ) {
      errs.push(
        `path '${id}': last waypoint (${endpoint.x},${endpoint.y}) `
        + `!= core (${core.x},${core.y})`,
      );
    }
    const outOfBounds = waypoints.find((point) => outsideCanvas(point.x, point.y));
    if (outOfBounds) {
      errs.push(
        `path '${id}': waypoint (${outOfBounds.x},${outOfBounds.y}) `
        + `outside canvas ${W}x${H}`,
      );
    }
  }

  const roadHalfWidth = Number(layout?.road_width ?? 48) / 2;
  if (!Number.isFinite(roadHalfWidth) || roadHalfWidth <= 0) {
    errs.push('road_width must be a finite positive number');
  }
  for (const slot of (Array.isArray(layout?.build_slots) ? layout.build_slots : [])) {
    if (!slot || !isFiniteNumber(slot.x) || !isFiniteNumber(slot.y)) {
      errs.push('build_slot missing x/y');
      continue;
    }
    const slotWidth = Number(slot.width ?? 64);
    const slotHeight = Number(slot.height ?? 64);
    if (
      !Number.isFinite(slotWidth)
      || !Number.isFinite(slotHeight)
      || slotWidth < 0
      || slotHeight < 0
    ) {
      errs.push(`build_slot (${slot.x},${slot.y}) has invalid width/height`);
      continue;
    }
    let minDistance = Infinity;
    for (const path of paths) {
      if (!path || !Array.isArray(path.waypoints)) continue;
      for (let index = 0; index < path.waypoints.length - 1; index += 1) {
        const start = path.waypoints[index];
        const end = path.waypoints[index + 1];
        if (start && end) {
          minDistance = Math.min(
            minDistance,
            distToSeg(slot.x, slot.y, start.x, start.y, end.x, end.y),
          );
        }
      }
    }
    const slotRadius = Math.max(slotWidth, slotHeight) / 2;
    const requiredClearance = roadHalfWidth + slotRadius + slotMargin;
    // A slot is valid if it is EITHER clear of every path (the tower-beside-a-road
    // default) OR deliberately on one (lane defence, e.g. plants on a lawn lane).
    // The two branches share one threshold, so on_path inverts the requirement
    // rather than removing it: an off-road slot cannot silence the clearance
    // check by claiming to be on the lane.
    const declaresOnPath = slot.on_path === true;
    if (declaresOnPath && !allowOnPath) {
      errs.push(
        `build_slot (${slot.x},${slot.y}) sets on_path but `
        + 'gates.tower_defence.map_layout.allow_on_path_build_slots is disabled',
      );
    } else if (Number.isFinite(minDistance)) {
      if (declaresOnPath) {
        if (minDistance > requiredClearance) {
          errs.push(
            `build_slot (${slot.x},${slot.y}) declares on_path but sits clear of every `
            + `path (center distance ${minDistance.toFixed(0)}px > ${requiredClearance}px); `
            + 'drop on_path or move it onto the route',
          );
        }
      } else if (minDistance < requiredClearance) {
        errs.push(
          `build_slot (${slot.x},${slot.y}) footprint sits on/too close to a road `
          + `(center distance ${minDistance.toFixed(0)}px < ${requiredClearance}px); `
          + 'set on_path:true if this is a lane-defence slot',
        );
      }
    }
    if (outsideCanvas(slot.x, slot.y, slotWidth / 2, slotHeight / 2)) {
      errs.push(
        `build_slot (${slot.x},${slot.y},${slotWidth}x${slotHeight}) `
        + `extends outside canvas ${W}x${H}`,
      );
    }
  }
  return errs;
}

// ── Gate: map-layout present + structurally valid (the geometry SSOT) ──
function gateMapLayoutPresent() {
  if (!isTowerDefence() || !usesAuthoredLevels()) return;
  const cfg = tdConfig().map_layout || {};
  if (cfg.enabled === false) return;
  const required = cfg.require !== false; // default true

  const effectiveLayouts = loadMapLayoutsForGate();
  if (effectiveLayouts.length === 0) {
    if (required) fail('tower_defence: no levels/stage_*.json exists to resolve an authored map layout');
    return;
  }
  for (const resolvedLayout of effectiveLayouts) {
    const {
      stageName,
      layout,
      source,
      invalid,
      missing,
      unsafe,
    } = resolvedLayout;
    if (unsafe) {
      fail(`tower_defence: levels/${stageName} map_layout_ref escapes GAME_DIR: ${source}`);
      continue;
    }
    if (invalid) {
      fail(`tower_defence: levels/${stageName} effective map layout '${source}' is invalid JSON/object`);
      continue;
    }
    if (missing || !layout) {
      if (required) {
        fail(
          `tower_defence: levels/${stageName} has no effective map layout at '${source}' `
          + '(precedence: embedded map_layout > map_layout_ref > levels/map-layout.json)',
        );
      }
      continue;
    }
    const errs = mapLayoutInvariantErrors(layout, cfg);
    if (errs.length) {
      const preview = errs.slice(0, 10).join('; ')
        + (errs.length > 10 ? `; +${errs.length - 10} more` : '');
      fail(
        `tower_defence map-layout for levels/${stageName} (${source}) `
        + `invariant violation(s): ${preview}`,
      );
    }
  }
}

// ── Gate: map base declared terrain-only (no baked path network) ───────
function gateNoBakedPath() {
  if (!isTowerDefence() || !usesAuthoredLevels()) return;
  const cfg = tdConfig().no_baked_path || {};
  if (cfg.enabled === false) return;
  if (cfg.require_terrain_only_flag === false) return;

  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  if (!manifest) return; // gateAssetManifest() handles missing/invalid
  const entries = manifestEntries(manifest);

  const baseRe = /(^|[-_/])(map[-_]?base|game[-_]?map|playfield|board[-_]?base|arena[-_]?base|terrain)($|[-_])/i;
  const NON_GAMEPLAY = /(title|gameover|game-over|menu|victory|defeat|win|lose|intro|splash)/i;
  const isGameBase = (e) => {
    if (!e || typeof e !== 'object') return false;
    const key = String(e.key || '').toLowerCase();
    const cat = String(e.category || '').toLowerCase();
    if (NON_GAMEPLAY.test(key)) return false;
    if (baseRe.test(key)) return true;
    if (/(scene-base|background|map|scene|map_base)/.test(cat) && /(base|map|terrain|field)/.test(key)) return true;
    return false;
  };

  const bases = entries.filter(isGameBase);
  if (bases.length === 0) {
    fail("tower_defence: no gameplay map-base entry found in asset-manifest.json to verify it is terrain-only. Name the playfield base like 'map-base' and set \"terrain_only\": true (path/core/spawns are rendered in code from levels/map-layout.json, never baked into the art).");
    return;
  }
  const unflagged = bases.filter(e => e.terrain_only !== true && e.terrainOnly !== true);
  if (unflagged.length) {
    fail(`tower_defence: ${unflagged.length} map-base asset(s) [${unflagged.map(e => e.key || '?').join(', ')}] not marked "terrain_only": true — the base must NOT bake the path network / core / spawns / build slots (a stochastic generator cannot match the coordinates the code walks). Regenerate terrain-only (optionally seeded by render_layout_mask) and set the flag.`);
  }
}

// ── Gate: enemy path built from map-layout, no interpolation residue ───
function gatePathFromLayout() {
  if (!isTowerDefence() || !usesAuthoredLevels()) return;
  const cfg = tdConfig().path_alignment || {};
  if (cfg.enabled === false) return;

  const files = listFiles(join(gameDir, 'src'), (_p, n) => n.endsWith('.js'));
  if (files.length === 0) return;

  let buildsFromLayout = false;
  const interpHits = [];
  // Classic spawn→core "thirds" interpolation constants (the legacy bug).
  const INTERP = [/\*\s*0\.(?:33|34|66|67)\b/];
  // Structural linear interpolation between spawn and core on one line.
  const LERP_PAIR = [
    /\b(corePos|core)\b[^\n;]*-[^\n;]*\b(spawnPos|spawn)\b[^\n;]*\*/i,
    /\b(spawnPos|spawn)\b[^\n;]*\+[^\n;]*\(\s*\b(corePos|core)\b[^\n;]*-[^\n;]*\)/i,
  ];
  for (const f of files) {
    let t; try { t = readFileSync(f, 'utf-8'); } catch { continue; }
    if (/buildPathFromLayout|map[-_]?layout|mapLayout|\.waypoints\b/.test(t)) buildsFromLayout = true;
    const rel = f.replace(gameDir + '/', '');
    t.split(/\r?\n/).forEach((ln, i) => {
      if (INTERP.some(re => re.test(ln)) || LERP_PAIR.some(re => re.test(ln))) interpHits.push(`${rel}:${i + 1}`);
    });
  }

  if (!buildsFromLayout) {
    fail('tower_defence: no src/ code builds the enemy path from map-layout waypoints (expected buildPathFromLayout(...) reading levels/map-layout.json `waypoints`). The path must come from the layout SSOT, not be synthesized.');
  }
  if (interpHits.length) {
    const preview = interpHits.slice(0, 8).join(', ') + (interpHits.length > 8 ? `, +${interpHits.length - 8} more` : '');
    fail(`tower_defence: ${interpHits.length} path-interpolation residue site(s) — synthesizing waypoints between spawn and core (e.g. * 0.33 / * 0.67, or (core - spawn) * t) is forbidden; build the path from map-layout waypoints and stroke that same path as the visible road. At: ${preview}`);
  }
}

// ── Gate: tower panel is a value-less binding asset (names/costs from CONFIG) ──
function readTowerRosterCount() {
  const cfgPath = join(gameDir, 'src/config.js');
  if (!fileExists(cfgPath)) return null;
  let t; try { t = readFileSync(cfgPath, 'utf-8'); } catch { return null; }
  const at = t.search(/\btowers\s*:\s*\{/);
  if (at < 0) return null;
  let i = t.indexOf('{', at), depth = 0, end = -1;
  const start = i;
  for (; i < t.length; i++) { const c = t[i]; if (c === '{') depth++; else if (c === '}') { depth--; if (depth === 0) { end = i; break; } } }
  if (end < 0) return null;
  const block = t.slice(start, end + 1);
  const names = block.match(/\bname\s*:/g); // one `name:` per tower def — robust proxy
  return names ? names.length : null;
}

function findTowerPanelEntry(manifest) {
  const entries = manifestEntries(manifest);
  return entries.find(e => {
    if (!e || typeof e !== 'object') return false;
    const key = String(e.key || '').toLowerCase();
    if (key === 'ui-tower-panel') return true;
    return /tower/.test(key) && /(panel|select|roster|cards?|bar)/.test(key);
  }) || null;
}

function gateHudBinding() {
  if (!isTowerDefence()) return;
  const cfg = tdConfig().hud_binding || {};
  if (cfg.enabled === false) return;

  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  const panel = manifest ? findTowerPanelEntry(manifest) : null;
  const rosterN = readTowerRosterCount();

  if (!panel && qualityPolicy().requireWidgetArt) {
    fail('tower_defence: no tower selection panel asset in asset-manifest.json (expected key like ui-tower-panel) — cannot verify it is a value-less binding asset.');
  } else if (panel && panel.value_less !== true && panel.valueLess !== true) {
    fail('tower_defence: tower panel asset is not marked "value_less": true — tower names/costs must be runtime add.text from CONFIG.towers, never baked into the panel art. Regenerate value-less card frames and set the flag.');
  }

  if (rosterN == null) {
    fail('tower_defence: could not read CONFIG.towers roster in src/config.js to verify panel/roster parity.');
  } else if (panel) {
    const slots = Number(panel.slots ?? panel.card_count ?? panel.cards ?? panel.slot_count);
    if (Number.isFinite(slots) && slots !== rosterN) {
      fail(`tower_defence: panel slot count (${slots}) != CONFIG tower roster size (${rosterN}) — the panel must show exactly one card per tower type.`);
    }
  }

  const src = readSrcText();
  if (!/CONFIG\.towers/.test(src)) {
    fail('tower_defence: src/ never references CONFIG.towers — the panel labels must be rendered from CONFIG (add.text reading CONFIG.towers[type].name/.cost), not hardcoded or baked into art.');
  } else if (!(/\badd\.text\s*\(/.test(src) && /\.cost\b/.test(src))) {
    fail('tower_defence: tower cost is not rendered as live text from CONFIG.towers[...].cost.');
  }
}

function gateTowerDefenseContract() {
  gateMapLayoutPresent();
  gateNoBakedPath();
  gatePathFromLayout();
  gateHudBinding();
}

// ── Interaction / collision contract gates ─────────────────────────────
// The physics counterpart of the TD contract bundle. Reads the unified
// .playtest/inputs/interaction-contract.json (scaffold-authored from GDD §0.2)
// and joins it to the MEASURED display-params.json on entityKey. Every check is
// genre-gated: pointer/board games with no layers/matrix auto-N/A, so the
// ~80% of non-physics games see zero new enforcement.
// ALWAYS-physics genres only. One-to-many genres (roguelike/puzzle/simulation/
// custom) are intentionally NOT here — a grid roguelike or logic puzzle is not a
// physics game. isPhysicsGame() catches their physics variants via the routing
// interaction_profile (arcade-gravity/arcade-no-gravity/matter) in state.json.
const TERRAIN_LAYERS = new Set(['terrain', 'oneway']); // built by prefab factories, not sprite assets → not in display-params
const INTERACTION_VERBS = new Set(['collide', 'collide_oneway', 'overlap', 'pick', 'ignore']);

// Normalize display-params.json to a { key: params } map (same three shapes
// gateEntityScale tolerates: { entities:{} } / { entities:[] } / flat map).
function loadInteractionDisplayParams() {
  const dpPath = resolveRead(playtestDir, DISPLAY_PARAMS);
  if (!fileExists(dpPath)) return null;
  const dp = readJSON(dpPath);
  return normalizeDisplayParams(dp);
}

// (a) Entity↔art fit + contract completeness + pick target size.
// ALWAYS runs gateEntityScale first (the legacy body↔visible-art check), so a
// sprite game without an interaction contract keeps that protection. Only the
// contract-specific checks are gated on the contract existing.
function gateInteractionFit() {
  gateEntityScale(); // body↔art (deterministic, N/A when no display-params)
  const cfg = readGcaConfig().gates?.interaction || {};
  if (cfg.enabled === false) return;
  const contract = readJSON(resolveRead(playtestDir, INTERACTION_CONTRACT));
  if (!contract || !contract.entities) return; // no authored entities → only entity-scale applies

  const minHit = Number(cfg.min_hit_target_px ?? 24);
  const assets = loadInteractionDisplayParams();
  const pickLayers = new Set((contract.matrix || []).filter((r) => Array.isArray(r) && r[2] === 'pick').map((r) => r[1]));
  const errs = [];
  const measurable = Object.entries(contract.entities).filter(([, e]) =>
    e && typeof e === 'object' && !TERRAIN_LAYERS.has(e.layer)
  );
  if (measurable.length > 0 && assets === null) {
    errs.push('display-params.json missing — authored entities cannot bypass measured geometry');
  }
  for (const [key, e] of Object.entries(contract.entities)) {
    if (!e || typeof e !== 'object') continue;
    if (!['rect', 'circle'].includes(e.shape)) errs.push(`entity '${key}' missing hit shape (shape: rect|circle)`);
    if (TERRAIN_LAYERS.has(e.layer)) continue;
    const measured = assets && assets[key];
    if (!measured) {
      errs.push(`entity '${key}' declared in interaction-contract but never measured into display-params.json (the AUTHORED→MEASURED join is broken)`);
      continue;
    }
    if (!worldVisibleDimensions(measured)) {
      errs.push(`entity '${key}' needs finite positive scale + contentBbox in display-params.json`);
    }
    if (e.fit === 'bbox' && e.body !== 'none') {
      if (!worldBodyDimensions(measured)) {
        errs.push(`entity '${key}' fit:bbox requires bodyWidth/bodyHeight and a valid bodySpace`);
      }
      if (!Number.isFinite(measured.bodyOffsetX) || !Number.isFinite(measured.bodyOffsetY)) {
        errs.push(`entity '${key}' fit:bbox requires explicit finite bodyOffsetX/bodyOffsetY`);
      }
    }
    // pick targets must be clickably large (e.g. G53 MUST-05: hit diameter ≥ 40px).
    if (pickLayers.has(e.layer)) {
      const visible = worldVisibleDimensions(measured);
      const need = Math.max(minHit, Number(e.min_hit_px ?? 0));
      const hit = visible ? Math.min(visible.w, visible.h) : NaN;
      if (!Number.isFinite(hit)) {
        errs.push(`pick target '${key}' has no measurable world-space ${e.shape || 'shape'}`);
      } else if (hit < need) {
        errs.push(`pick target '${key}' hit ${hit}px < required ${need}px — too small to click reliably`);
      }
    }
  }
  if (errs.length) fail(`interaction-fit violation(s): ${errs.slice(0, 10).join('; ')}`);
}

// (b) The interaction-contract bundle (mirror of gateTowerDefenseContract):
// fit → topology/wiring/world-bounds → tunnel-risk → prefab-coverage.
function gateInteractionContract() {
  const cfg = readGcaConfig().gates?.interaction || {};
  const contract = readJSON(resolveRead(playtestDir, INTERACTION_CONTRACT));
  if (!contract) {
    gateEntityScale();
    fail('runtime interaction gate: interaction-contract.json missing or invalid');
    return;
  }
  validateInteractionRouting(contract, 'runtime interaction gate');
  if (cfg.enabled === false) { gateEntityScale(); return; } // disabled → keep only the legacy body↔art check
  const genre = detectGenre();
  const layerList = Array.isArray(contract?.layers) ? contract.layers : [];
  const matrix = Array.isArray(contract?.matrix) ? contract.matrix : [];
  const hasTopology = layerList.length > 0 && matrix.length > 0;
  const layers = new Set(layerList);
  const declared = [];
  const errs = [];
  const bodyKinds = new Set(['dynamic', 'static', 'kinematic', 'sensor', 'none']);
  const entityEntries = Object.entries(contract?.entities || {});

  for (const [key, entity] of entityEntries) {
    if (!entity || typeof entity !== 'object') {
      errs.push(`entity '${key}' must be an object`);
      continue;
    }
    if (typeof entity.layer !== 'string' || !entity.layer) {
      errs.push(`entity '${key}' missing layer`);
    } else if (layers.size > 0 && !layers.has(entity.layer)) {
      errs.push(`entity '${key}' references undeclared layer '${entity.layer}'`);
    }
    if (!['rect', 'circle'].includes(entity.shape)) {
      errs.push(`entity '${key}' missing hit shape (shape: rect|circle)`);
    }
    if (!bodyKinds.has(entity.body)) {
      errs.push(`entity '${key}' body must be dynamic|static|kinematic|sensor|none`);
    }
    if (entity.fit !== 'bbox') {
      errs.push(`entity '${key}' fit must be 'bbox'`);
    }
  }
  // Pointer primitives MUST declare argsFromLLM: ["x","y"] so the play-agent can
  // pass LLM-chosen coordinates through. Without this, all clicks resolve to canvas
  // center and the agent can never interact with specific UI elements.
  const POINTER_PRIMS = new Set(['pointer_click', 'pointer_down', 'pointer_up', 'pointer_move', 'tap']);
  for (const [name, action] of Object.entries(contract?.actions || {})) {
    if (!action || typeof action !== 'object') continue;
    if (POINTER_PRIMS.has(action.primitive)) {
      const from = action.argsFromLLM;
      if (!Array.isArray(from) || !from.includes('x') || !from.includes('y')) {
        errs.push(`action '${name}' uses pointer primitive '${action.primitive}' but is missing argsFromLLM:["x","y"] — play-agent clicks will all resolve to canvas center`);
      }
    }
  }

  const matrixPairs = new Map();
  for (const row of matrix) {
    if (!Array.isArray(row) || row.length < 3) { errs.push(`matrix row ${JSON.stringify(row)} is not [from, to, verb]`); continue; }
    const [a, b, kind] = row;
    if (!layers.has(a) || !layers.has(b)) errs.push(`matrix [${a},${b}] references an undeclared layer`);
    if (!INTERACTION_VERBS.has(kind)) errs.push(`matrix [${a},${b}] unknown verb '${kind}' (use ${[...INTERACTION_VERBS].join('|')})`);
    const pairKey = [String(a), String(b)].sort().join('\u0000');
    if (matrixPairs.has(pairKey)) {
      errs.push(`matrix pair [${a},${b}] declared more than once (${matrixPairs.get(pairKey)} and ${kind})`);
    } else {
      matrixPairs.set(pairKey, kind);
    }
    if (kind && kind !== 'ignore') declared.push(kind);
  }

  // Always run fit (→ gateEntityScale), regardless of whether a topology block exists.
  gateInteractionFit();
  if (errs.length) fail(`interaction-contract violation(s): ${errs.slice(0, 10).join('; ')}`);

  if (!hasTopology) {
    const hasPhysicalEntity = entityEntries.some(([, entity]) =>
      entity && typeof entity === 'object' && entity.body !== 'none'
    );
    const hasPointerAction = Object.values(contract?.actions || {}).some((action) =>
      action && typeof action === 'object' &&
      /^pointer_(?:down|up|move|click)$/.test(String(action.primitive || ''))
    );
    // Physics bodies and pointer controls require topology. A board/text game
    // may still declare non-interacting body:none hit-shape records and remain N/A.
    if (hasPhysicalEntity || hasPointerAction) {
      fail('interaction: physical entities or pointer actions require non-empty layers + matrix (pointer targets need an explicit pick row)');
    } else if (cfg.require_for_physics_genres !== false && isPhysicsGame()) {
      fail(`interaction: ${genre} game's interaction-contract.json has no layers/matrix — "who collides with whom" is gameplay SSOT (scaffold derives it from GDD §0.2), not implicit in code.`);
    }
    gateTunnelRisk();      // self-N/A when no bullets / no contract
    gatePrefabCoverage();  // self-N/A on non-platform/beat_em_up
    return;
  }

  const src = readSrcText();
  const wiringErrs = [];
  // Wiring: the recommended path is one registerInteractions(this, ...) call that
  // wires the whole matrix in a loop; accept that, else require enough hand-rolled
  // collider/overlap calls to cover the declared interactions.
  const want = declared.filter((k) => k !== 'pick').length;
  const callsRegistrar = /\bregisterInteractions\s*\(\s*(this|scene)\b/.test(src);
  if (want > 0 && !callsRegistrar) {
    const wired = (src.match(/physics\.add\.(collider|overlap)\s*\(/g) || []).length;
    if (wired < want) {
      wiringErrs.push(`matrix declares ${want} collide/overlap interaction(s) but src/ neither calls registerInteractions(this, ...) nor wires ${want} physics.add.collider/overlap (found ${wired}) — missing colliders let entities pass through each other`);
    }
  }
  if (declared.includes('pick') && !/\bpickAt\s*\(\s*this\b/.test(src) && !/\.on\(\s*['"]pointer(down|up|move)['"]/.test(src)) {
    wiringErrs.push('matrix declares a pick interaction but src/ has no pointer handler (.on("pointerdown", ...)) or pickAt(this, ...) call');
  }
  if (isPhysicsGame() && genre !== 'tower_defence' && cfg.require_world_bounds !== false
      && !/setBounds\s*\(|setCollideWorldBounds\s*\(\s*true/.test(src)) {
    wiringErrs.push('no world bounds set — entities can leave the world (expected physics.world.setBounds(...) or body.setCollideWorldBounds(true))');
  }
  if (wiringErrs.length) fail(`interaction wiring violation(s): ${wiringErrs.slice(0, 10).join('; ')}`);
  gateTunnelRisk();
  gatePrefabCoverage();
}

// Deterministic anti-tunneling gate: a fast body whose
// per-frame travel (speed/fps) meets/exceeds the smallest target body it can hit
// will skip past it between frames. Conservative — only asserts when both the
// speed (parsed from config.js) and a measured target body dimension are known.
function gateTunnelRisk() {
  const cfg = readGcaConfig().gates?.tunnel || {};
  if (cfg.enabled === false) return;
  const contract = readJSON(resolveRead(playtestDir, INTERACTION_CONTRACT));
  const assets = loadInteractionDisplayParams();
  if (!contract || !contract.entities || !assets) return; // need authored bullets + measured targets

  const fps = Number(cfg.assumed_fps ?? 60);
  const speedFloor = Number(cfg.speed_threshold ?? 200);
  const ratio = Number(cfg.min_body_ratio ?? 1.0);

  // Flat name→number map from config.js scalar assignments (bulletSpeed: 600, …).
  let cfgText = '';
  try { cfgText = readFileSync(join(gameDir, 'src', 'config.js'), 'utf-8'); } catch { cfgText = readSrcText(); }
  const numbers = {};
  for (const m of cfgText.matchAll(/([A-Za-z_$][\w$]*)\s*[:=]\s*(\d+(?:\.\d+)?)/g)) {
    const name = m[1].toLowerCase();
    if (!(name in numbers)) numbers[name] = Number(m[2]); // first wins (usually the canonical CONFIG value)
  }
  const PROJ = /(bullet|bolt|arrow|laser|projectile|missile|shot|spear|dart|orb)/;
  const speedFor = (key) => {
    const k = key.toLowerCase().replace(/[^a-z0-9]/g, '');
    for (const suffix of ['speed', 'velocity', 'vel']) if (numbers[`${k}${suffix}`]) return numbers[`${k}${suffix}`];
    const tail = (k.match(PROJ) || [])[0];
    if (tail) for (const cand of [`${tail}speed`, `${tail}velocity`, `${tail}vel`]) if (numbers[cand]) return numbers[cand];
    for (const generic of ['bulletspeed', 'projectilespeed', 'arrowspeed', 'laserspeed']) if (numbers[generic]) return numbers[generic];
    return null;
  };

  const errs = [];
  for (const [key, e] of Object.entries(contract.entities)) {
    if (!e || (e.bullet !== true && !PROJ.test(key.toLowerCase()))) continue;
    const speed = speedFor(key);
    if (!speed || speed < speedFloor) continue; // unknown or slow → cannot confirm a risk
    const perFrame = speed / fps;
    const targetLayers = (contract.matrix || []).flatMap((r) => {
      if (!Array.isArray(r) || (r[2] !== 'overlap' && r[2] !== 'collide')) return [];
      if (r[0] === e.layer) return [r[1]];
      if (r[1] === e.layer) return [r[0]];
      return [];
    });
    let minDim = Infinity;
    for (const [tk, te] of Object.entries(contract.entities)) {
      if (!te || !targetLayers.includes(te.layer)) continue;
      const worldBody = worldBodyDimensions(assets[tk]);
      if (worldBody) minDim = Math.min(minDim, worldBody.w, worldBody.h);
    }
    if (!Number.isFinite(minDim)) continue; // no measured target → cannot assert
    const ownBody = worldBodyDimensions(assets[key]);
    const ownCover = ownBody ? Math.max(ownBody.w, ownBody.h) : 0;
    if (perFrame >= minDim * ratio && perFrame > ownCover) {
      errs.push(`bullet '${key}' travels ${perFrame.toFixed(0)}px/frame (speed ${speed} ÷ ${fps}fps) ≥ smallest target body ${minDim}px — likely tunneling risk. Fix by enlarging the bullet body to ≥${Math.ceil(perFrame)}px on its travel axis, using overlap+positional check, capping speed, or adding a multi-step sweep.`);
    }
  }
  if (errs.length) fail(`tunnel-risk: ${errs.slice(0, 8).join('; ')}`);
}

// Terrain prefab coverage (platform incl. auto-scroll variant / side beat_em_up):
// every `chunk` id used in any levels/stage_*.json must have a builder in src/
// (buildChunk_<id> / case '<id>' / a REGISTRY.terrain key). Unbuilt chunks =
// invisible / holey ground.
function gatePrefabCoverage() {
  const cfg = readGcaConfig().gates?.prefab || {};
  if (cfg.enabled === false) return;
  const state = readJSON(join(playtestDir, 'state.json'));
  if (state?.level_model !== 'authored') return;
  if (!['platform', 'beat_em_up'].includes(detectGenre())) return; // other genres N/A

  const used = new Set();
  for (const f of listFiles(join(gameDir, 'levels'), (_p, n) => /^stage_\d+\.json$/.test(n))) {
    const d = readJSON(f);
    for (const t of (d?.terrain || [])) if (t && typeof t.chunk === 'string') used.add(t.chunk);
  }
  if (used.size === 0) return; // no terrain chunks authored → N/A

  const src = readSrcText();
  const missing = [];
  for (const id of used) {
    const esc = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`buildChunk_${esc}\\b|case\\s+['"]${esc}['"]|['"\`]${esc}['"\`]\\s*:|\\b${esc}\\s*:`);
    if (!re.test(src)) missing.push(id);
  }
  if (missing.length) {
    fail(`prefab-coverage: level(s) use terrain chunk(s) [${missing.join(', ')}] with no builder in src/ — every chunk in levels/*.json must map to a buildChunk_<id> / case '<id>' / REGISTRY.terrain entry (see references/level-design/terrain-prefabs.md). Unbuilt chunks render as invisible/holey ground.`);
  }
}

export {
  gateLevelsPresent,
  gateLevelRuntime,
  gateMapLayoutPresent,
  gateNoBakedPath,
  gatePathFromLayout,
  gateHudBinding,
  gateTowerDefenseContract,
  gateInteractionFit,
  gateInteractionContract,
  gateTunnelRisk,
  gatePrefabCoverage,
} ;
