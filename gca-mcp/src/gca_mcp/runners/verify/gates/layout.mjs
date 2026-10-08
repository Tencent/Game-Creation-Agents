/** Layout alignment gates — mechanically extracted gate domain. */

import {
  statSync,
  writeFileSync,
  mkdirSync,
} from 'fs';

import { join } from 'path';

import {
  resolveRead,
  LAYOUT_ALIGNMENT,
  LAYOUT_CONTRACT,
  LAYOUT_RENDERED_DIR,
} from '../playtest-layout.mjs';

import {
  readJSON,
  pngSize,
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
  RUNTIME_LAYOUT_SOURCES,
  inspectRuntimeAssetPath,
  hasConceptMockups,
  qualityProfileName,
  qualityPolicy,
  sceneWaiverState,
  latestLayoutInputMtime,
  UI_PANEL_RE,
  uiAllowsLenientExemption,
  manifestEntries,
} from './support.mjs';

// ── Gate: layout alignment (Axis 5 — lays-out-like, deterministic) ─────────
// "every element matches the mmgdd layout": every element in the §2.0.2-derived
// layout-contract.json must be RENDERED at its measured position. This gate is
// deterministic (no VLM): it reads the contract + the per-scene rendered bounds
// dump (capture-layout.mjs → verify_middle/layout/<scene>.rendered.json), checks
//   (1) correspondence/coverage — every contract element found rendered (by name),
//   (2) position — |rendered_center − target_center| / canvas_diag ≤ tolerance,
//       kind-aware: `static`/`spawn` are point checks (spawn = first-frame), `zone`
//       is a containment check (centroid inside the zone rect),
// and WRITES its own audit to alignment/layout/alignment.json (like gateEntityScale
// writes scale-report.json). Migration-safe: N/A when there are no mockups or no
// layout contract, so text-only / not-yet-migrated games are unaffected.
function roleLayoutToleranceMult(role) {
  const r = String(role || '').toLowerCase();
  if (/decor|background|backdrop/.test(r)) return 1.6;  // decorative: looser
  if (/logo|banner|title|text|lettering/.test(r)) return 1.25;
  if (/hud|bar|gauge|button|icon|badge|panel|minimap/.test(r)) return 1.0; // HUD: tight
  return 1.0;
}

function writeLayoutAudit(payload, { blockOnWriteError = true } = {}) {
  try {
    mkdirSync(join(playtestDir, LAYOUT_ALIGNMENT.replace(/\/[^/]+$/, '')), { recursive: true });
    writeFileSync(join(playtestDir, LAYOUT_ALIGNMENT), JSON.stringify({
      source: 'layout-alignment-evaluator',
      generated_at: new Date().toISOString(),
      ...payload,
    }, null, 2));
    return true;
  } catch (error) {
    if (blockOnWriteError) fail(`could not write alignment/layout/alignment.json: ${error.message}`);
    return false;
  }
}

function layoutNotApplicablePayload(reason) {
  return {
    axis: 'layout',
    applicable: false,
    status: 'not_applicable',
    aligned: null,
    reason,
    issues: [],
  };
}

function materializeLayoutAlignment({ blockOnWriteError = true } = {}) {
  const cfg = readGcaConfig();
  if (cfg.gates?.layout?.enabled === false) {
    const payload = layoutNotApplicablePayload('gates.layout.enabled is false');
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return { ...payload, wrote, blocking_messages: wrote ? [] : ['could not write alignment/layout/alignment.json'] };
  }
  const contractPath = resolveRead(playtestDir, LAYOUT_CONTRACT);
  if (!fileExists(contractPath)) {
    const payload = layoutNotApplicablePayload('layout-contract.json missing');
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return { ...payload, wrote, blocking_messages: wrote ? [] : ['could not write alignment/layout/alignment.json'] };
  }
  const contract = readJSON(contractPath);
  if (!contract) {
    const issue = 'layout-contract.json is invalid JSON';
    const payload = { axis: 'layout', applicable: true, status: 'error', aligned: false, issues: [issue] };
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return { ...payload, wrote, blocking_messages: wrote ? [issue] : [issue, 'could not write alignment/layout/alignment.json'] };
  }
  const scenes = contract.scenes && typeof contract.scenes === 'object' ? contract.scenes : {};
  const sceneKeys = Object.keys(scenes);
  if (sceneKeys.length === 0) {
    const payload = layoutNotApplicablePayload('layout contract has no scenes');
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return { ...payload, wrote, blocking_messages: wrote ? [] : ['could not write alignment/layout/alignment.json'] };
  }
  const { valid: waived, issues: waiverIssues } = sceneWaiverState(contract);
  if (waiverIssues.length) {
    const payload = { axis: 'layout', applicable: true, status: 'error', aligned: false, issues: waiverIssues };
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return {
      ...payload,
      wrote,
      blocking_messages: wrote
        ? waiverIssues
        : [...waiverIssues, 'could not write alignment/layout/alignment.json'],
    };
  }
  if (sceneKeys.every((sceneKey) => waived.has(sceneKey))) {
    const payload = layoutNotApplicablePayload('all layout scenes have validated concept-failure or pure-transition waivers');
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return { ...payload, wrote, blocking_messages: wrote ? [] : ['could not write alignment/layout/alignment.json'] };
  }
  if (!hasConceptMockups()) {
    const missing = sceneKeys.filter((sceneKey) => !waived.has(sceneKey));
    const issue = `layout alignment requires concept mockups for unwaived scene(s): ${missing.join(', ')}`;
    const payload = { axis: 'layout', applicable: true, status: 'error', aligned: false, issues: [issue] };
    const wrote = writeLayoutAudit(payload, { blockOnWriteError });
    return {
      ...payload,
      wrote,
      blocking_messages: wrote
        ? [issue]
        : [issue, 'could not write alignment/layout/alignment.json'],
    };
  }

  // Position tolerance as a fraction of the canvas diagonal. `anchor_tolerance` is the
  // repurposed (formerly-dead) alias for `tolerance` — either activates this knob.
  const tol = isFiniteNumber(cfg.gates?.layout?.tolerance) ? cfg.gates.layout.tolerance
            : (isFiniteNumber(cfg.gates?.layout?.anchor_tolerance) ? cfg.gates.layout.anchor_tolerance : 0.04);
  const minCoverage = isFiniteNumber(cfg.gates?.layout?.min_coverage) ? cfg.gates.layout.min_coverage : 1.0;
  const spawnMult = 1.25;

  // Rendered-bounds pass thresholds (Tier 0 of the container-layout plan):
  // overlap/offscreen/size-drift are judged
  // on the REAL capture-layout getBounds, not the self-reported ledger.
  const layoutCfg = cfg.gates?.layout || {};
  const overlapCheck = layoutCfg.overlap_check !== false;
  const overlapPx = isFiniteNumber(layoutCfg.overlap_min_px) ? layoutCfg.overlap_min_px : 12;
  const overlapTol = isFiniteNumber(layoutCfg.overlap_tolerance) ? layoutCfg.overlap_tolerance : 0.12;
  const offscreenPx = isFiniteNumber(layoutCfg.offscreen_min_px) ? layoutCfg.offscreen_min_px : 8;
  const sizeCheck = (
    layoutCfg.size_check !== false
  );
  const sizeTol = isFiniteNumber(layoutCfg.size_drift_tolerance) ? layoutCfg.size_drift_tolerance : 0.02;
  const profileName = qualityProfileName();
  const profilePolicy = qualityPolicy();
  const manifestByKey = {};
  for (const e of manifestEntries(readJSON(join(gameDir, 'assets', 'asset-manifest.json')))) {
    if (e && e.key) manifestByKey[String(e.key)] = e;
  }

  const renderedDir = join(playtestDir, LAYOUT_RENDERED_DIR);
  const sceneReports = [];
  const issues = [];
  let renderedFilesFound = 0;
  const captureManifest = readJSON(join(renderedDir, '_run.json'));
  const captureRunId = captureManifest?.run_id || null;
  const layoutInputMtime = latestLayoutInputMtime();
  if (!captureManifest || captureManifest.source !== 'capture-layout.mjs' || !captureRunId) {
    issues.push('layout capture manifest missing/invalid — rerun capture_layout for one atomic run');
  } else {
    const capturedScenes = Array.isArray(captureManifest.scenes) ? [...captureManifest.scenes].sort() : [];
    const expectedScenes = [...sceneKeys].sort();
    if (JSON.stringify(capturedScenes) !== JSON.stringify(expectedScenes)) {
      issues.push(`layout capture manifest scene set does not match contract (${capturedScenes.join(', ')} vs ${expectedScenes.join(', ')})`);
    }
  }

  for (const sceneKey of sceneKeys) {
    const elements = Array.isArray(scenes[sceneKey]?.elements) ? scenes[sceneKey].elements : [];
    const renderedPath = join(renderedDir, `${sceneKey}.rendered.json`);
    if (!fileExists(renderedPath)) {
      issues.push(`scene '${sceneKey}': not captured (no ${sceneKey}.rendered.json) — run capture-layout.mjs`);
      sceneReports.push({ scene: sceneKey, captured: false, total: elements.length, found: 0, coverage: 0, results: [] });
      continue;
    }
    renderedFilesFound += 1;
    const rendered = readJSON(renderedPath) || {};
    if (!captureRunId || rendered.run_id !== captureRunId) {
      issues.push(`scene '${sceneKey}': rendered bounds are from a different/legacy run — rerun capture_layout`);
    }
    if (layoutInputMtime && statSync(renderedPath).mtimeMs < layoutInputMtime) {
      issues.push(`scene '${sceneKey}': rendered bounds are stale relative to layout inputs — rerun capture_layout`);
    }
    if (rendered.error) {
      issues.push(`scene '${sceneKey}': capture failed — ${rendered.error}`);
      sceneReports.push({ scene: sceneKey, captured: false, error: rendered.error, total: elements.length, found: 0, coverage: 0, results: [] });
      continue;
    }
    const cw = rendered.canvas?.w || contract.canvas?.designWidth || 1280;
    const ch = rendered.canvas?.h || contract.canvas?.designHeight || 720;
    const diag = Math.hypot(cw, ch) || 1;

    // index rendered objects by name/layoutKey → list of instances
    const byKey = {};
    for (const o of (Array.isArray(rendered.elements) ? rendered.elements : [])) {
      for (const k of new Set([o.name, o.layoutKey].filter(Boolean))) {
        (byKey[k] ||= []).push(o);
      }
    }

    const results = [];
    let found = 0;
    for (const el of elements) {
      const key = el.key;
      const box = Array.isArray(el.box) ? el.box : null;
      const kind = String(el.kind || 'static').toLowerCase();
      const insts = byKey[key] || [];
      if (!box || box.length !== 4) {
        results.push({ key, kind, ok: false, reason: 'contract row needs exactly box [x,y,w,h]' });
        issues.push(`scene '${sceneKey}': element '${key}' needs an exact four-value box`);
        continue;
      }
      if (el.origin !== undefined && (!Array.isArray(el.origin) || el.origin.length !== 2)) {
        results.push({ key, kind, ok: false, reason: 'contract row needs exactly origin [x,y]' });
        issues.push(`scene '${sceneKey}': element '${key}' needs an exact two-value origin`);
        continue;
      }
      if (insts.length === 0) {
        results.push({ key, kind, ok: false, reason: 'not rendered (missing or unnamed)' });
        issues.push(`scene '${sceneKey}': element '${key}' was never rendered — place it and set obj.name='${key}' (addUiImage/applyDisplay do this)`);
        continue;
      }
      found += 1;
      const elTol = (isFiniteNumber(el.tolerance) ? el.tolerance : tol)
                  * roleLayoutToleranceMult(el.role) * (kind === 'spawn' ? spawnMult : 1);

      // kind:cluster (Tier 1 — declarative same-corner HUD row): member checks
      // (every declared member rendered INSIDE the container + left→right order)
      // plus an ANCHOR-POINT position check on the container. NOT a center check:
      // the cluster's rendered width is EMERGENT (sum of real PNG widths + gaps),
      // so its union center legitimately drifts from the authored box center —
      // only the anchored corner (box anchor at the row's origin) is authored.
      if (kind === 'cluster') {
        const members = Array.isArray(el.members) ? el.members : [];
        // Prefer the instance INSIDE this cluster's container — the same key may
        // also render elsewhere in the scene (old captures lack `parent`: fall
        // back to any instance).
        const pick = (mk) => {
          const list = byKey[mk] || [];
          const inC = list.filter((o) => o && o.parent === key);
          return (inC.length ? inC : list)[0];
        };
        const missing = members.filter((mk) => !pick(mk));
        const mcx = members.map(pick).filter((o) => o && isFiniteNumber(o.cx)).map((o) => o.cx);
        const misordered = mcx.some((v, k2) => k2 > 0 && v < mcx[k2 - 1]);
        if (missing.length || misordered) {
          const why = [
            missing.length ? `member(s) never rendered: ${missing.join(', ')}` : null,
            misordered ? 'members render out of the declared left→right order' : null,
          ].filter(Boolean).join('; ');
          results.push({ key, kind, ok: false, reason: why, members, instances: insts.length });
          issues.push(`scene '${sceneKey}': cluster '${key}' — ${why}. Lay the cluster out with addUiCluster(scene, '${key}', CONFIG.ui['${key}'], [...members]) so each child keeps obj.name = its key and the declared order.`);
          continue;
        }
        const inst = insts.find((o) => isFiniteNumber(o.cx) && isFiniteNumber(o.cy) && o.w > 0 && o.h > 0);
        const org = Array.isArray(el.origin) ? el.origin : [0, 0];
        const cax = isFiniteNumber(Number(org[0])) ? Number(org[0]) : 0;
        const cay = isFiniteNumber(Number(org[1])) ? Number(org[1]) : 0;
        const tAX = (box[0] + cax * box[2]) * cw;
        const tAY = (box[1] + cay * box[3]) * ch;
        let dA = Infinity, rA = null;
        if (inst) {
          const rax = (inst.cx - inst.w / 2) + cax * inst.w;
          const ray = (inst.cy - inst.h / 2) + cay * inst.h;
          dA = Math.hypot(rax - tAX, ray - tAY) / diag;
          rA = [Math.round(rax), Math.round(ray)];
        }
        const okC = dA <= elTol;
        results.push({ key, kind, ok: okC, delta: Number(dA.toFixed(4)), tolerance: Number(elTol.toFixed(4)),
          target: [Math.round(tAX), Math.round(tAY)], rendered: rA, members });
        if (!okC) issues.push(`scene '${sceneKey}': cluster '${key}' anchored corner (${rA ? rA.join(',') : 'n/a'}) is ${(dA * 100).toFixed(1)}% of the canvas diagonal off its §2.0.2 box anchor (${Math.round(tAX)},${Math.round(tAY)}) (tol ${(elTol * 100).toFixed(1)}%)`);
        continue;
      }

      if (kind === 'zone') {
        // containment: the instances' centroid must sit inside the zone rect (+ tol margin).
        const cxs = insts.map((o) => o.cx).filter(isFiniteNumber);
        const cys = insts.map((o) => o.cy).filter(isFiniteNumber);
        const gx = cxs.reduce((s, v) => s + v, 0) / (cxs.length || 1);
        const gy = cys.reduce((s, v) => s + v, 0) / (cys.length || 1);
        const m = elTol * diag;
        const x0 = box[0] * cw - m, y0 = box[1] * ch - m;
        const x1 = (box[0] + box[2]) * cw + m, y1 = (box[1] + box[3]) * ch + m;
        const inside = gx >= x0 && gx <= x1 && gy >= y0 && gy <= y1;
        results.push({ key, kind, ok: inside, instances: insts.length, centroid: [Math.round(gx), Math.round(gy)], zone: [Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)] });
        if (!inside) issues.push(`scene '${sceneKey}': zone '${key}' centroid (${Math.round(gx)},${Math.round(gy)}) is outside its §2.0.2 region`);
        continue;
      }

      // static/spawn: point check against the nearest instance's center.
      const tcx = (box[0] + box[2] / 2) * cw;
      const tcy = (box[1] + box[3] / 2) * ch;
      let best = Infinity, bestInst = null;
      for (const o of insts) {
        if (!isFiniteNumber(o.cx) || !isFiniteNumber(o.cy)) continue;
        const d = Math.hypot(o.cx - tcx, o.cy - tcy) / diag;
        if (d < best) { best = d; bestInst = o; }
      }
      const ok = best <= elTol;
      results.push({ key, kind, ok, delta: Number(best.toFixed(4)), tolerance: Number(elTol.toFixed(4)),
        target: [Math.round(tcx), Math.round(tcy)], rendered: bestInst ? [Math.round(bestInst.cx), Math.round(bestInst.cy)] : null });
      if (!ok) issues.push(`scene '${sceneKey}': '${key}' is ${(best * 100).toFixed(1)}% of the canvas diagonal off its §2.0.2 position (tol ${(elTol * 100).toFixed(1)}%)`);
    }

    // ── Rendered-bounds pass (Tier 0) ──
    // The ledger overlap check in gateUiProportion sees only what implement
    // self-reported; THIS pass sees what actually rendered. Scope: HUD-layer
    // (effective scrollFactor 0,0) static/ui-* elements — the failure class is
    // HUD widgets colliding; world-space entities/props overlap scenery by design.
    const contractByKey = {};
    const clusterOf = {};      // member key → its cluster key (members position via the cluster)
    const indexContractNode = (node, parentCluster = null) => {
      if (!node || typeof node !== 'object' || Array.isArray(node)) return;
      const nodeKey = node.key || node.bind;
      if (nodeKey) {
        contractByKey[nodeKey] = node;
        if (parentCluster) clusterOf[nodeKey] = parentCluster;
      }
      const childCluster = String(node.kind || '').toLowerCase() === 'cluster'
        ? nodeKey
        : parentCluster;
      for (const child of (Array.isArray(node.children) ? node.children : [])) {
        indexContractNode(child, childCluster);
      }
    };
    for (const el of elements) indexContractNode(el);
    for (const el of elements) {
      if (String(el.kind || '').toLowerCase() === 'cluster') {
        for (const mk of (Array.isArray(el.members) ? el.members : [])) clusterOf[mk] = el.key;
      }
    }
    const contractRowFor = (object) => (
      contractByKey[object?.name]
      || (!clusterOf[object?.name] ? contractByKey[object?.layoutKey] : null)
      || null
    );
    const manifestRowFor = (object) => (
      manifestByKey[object?.name]
      || (!clusterOf[object?.name] ? manifestByKey[object?.layoutKey] : null)
      || null
    );
    const sceneOverlaps = [];
    const sceneOffscreen = [];
    const sceneSizeDrift = [];
    if (overlapCheck || sizeCheck) {
      const isHudLayer = (o) => {
        const sx = isFiniteNumber(o.effScrollFactorX) ? o.effScrollFactorX : o.scrollFactorX;
        const sy = isFiniteNumber(o.effScrollFactorY) ? o.effScrollFactorY : o.scrollFactorY;
        return sx === 0 && sy === 0;
      };
      const decorish = (o, row) => {
        const key = String(o.name || o.layoutKey || '');
        const m = manifestRowFor(o);
        return uiAllowsLenientExemption(
          key,
          m?.category,
          row?.role || m?.role || '',
          m?.decorative === true,
          [row?.kind, row?.category].filter(Boolean).join(' '),
        );
      };
      // Visible-content rect: shrink the full-texture bounds to the manifest
      // contentBbox when present (Layer 2 — transparent padding never counts as
      // overlap, and "frames apart but visible art collides" is caught for real).
      const contentRect = (o) => {
        let w = o.w, h = o.h;
        let x = o.cx - w / 2, y = o.cy - h / 2;
        let shrunk = false;
        const m = manifestRowFor(o);
        const bbox = m && (m.contentBbox || m.content_bbox);
        const tw = m ? Number(m.width) : NaN, th = m ? Number(m.height) : NaN;
        if (bbox && isFiniteNumber(bbox.x) && isFiniteNumber(bbox.y) && bbox.w > 0 && bbox.h > 0 && tw > 0 && th > 0) {
          x += (bbox.x / tw) * w; y += (bbox.y / th) * h;
          w *= bbox.w / tw;       h *= bbox.h / th;
          shrunk = true;
        }
        return { x, y, w, h, shrunk };
      };
      const candidates = (Array.isArray(rendered.elements) ? rendered.elements : []).filter((o) => {
        if (!o || o.visible === false) return false;
        if (!isFiniteNumber(o.cx) || !isFiniteNumber(o.cy) || !(o.w > 0) || !(o.h > 0)) return false;
        const t = String(o.type || '');
        if (t === 'Container' || t === 'Zone') return false;   // containers = union of children; zones = invisible hit areas
        const row = contractRowFor(o);
        const kind2 = row ? String(row.kind || 'static').toLowerCase() : null;
        if (row && kind2 !== 'static' && !clusterOf[o.name]) return false;      // entities move — no static claim
        if (!row && !/^ui[-_]/i.test(String(o.name || '')) && !clusterOf[o.name]) return false;
        if (!isHudLayer(o)) return false;
        return !decorish(o, row);
      });
      if (overlapCheck) {
        // (a) offscreen — visible content may hang off the canvas ≤ offscreenPx.
        for (const o of candidates) {
          const r = contentRect(o);
          const outX = Math.max(0, -r.x) + Math.max(0, r.x + r.w - cw);
          const outY = Math.max(0, -r.y) + Math.max(0, r.y + r.h - ch);
          if (outX > offscreenPx || outY > offscreenPx) {
            sceneOffscreen.push({ key: o.name, instance: o.instance, rect: [Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h)], canvas: [cw, ch] });
            issues.push(`scene '${sceneKey}': '${o.name}' renders off-canvas — rect (${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.w)}×${Math.round(r.h)}) vs ${cw}×${ch} (REAL bounds)`);
          }
        }
        // (b) pairwise overlap — trigger is the both-axis px guard (a real ~21px
        // edge-to-edge text collision between two wide panels must fire even
        // though its AREA ratio is tiny). The area-ratio knob is only a
        // padding-ghost guard for pairs where NEITHER rect could be shrunk to a
        // contentBbox (full-texture rects include transparent padding).
        const famBase = (k) => String(k).replace(/-(frame|bg|background|fill|track|border|chrome)$/i, '');
        const GENERIC_BASE = /^(ui|hud)?[-_]?$/i;
        const stackPair = (ka, kb) => { const b = famBase(ka); return b === famBase(kb) && !GENERIC_BASE.test(b); };
        const panelish = (o, row) => UI_PANEL_RE.test(String(o.name || '')) || /panel|toolbar|dashboard|board/i.test(String((row && row.role) || ''));
        for (let i2 = 0; i2 < candidates.length; i2++) {
          for (let j2 = i2 + 1; j2 < candidates.length; j2++) {
            const A = candidates[i2], B = candidates[j2];
            if (A.name === B.name) continue;                            // formation instances of one key
            if (stackPair(A.name, B.name)) continue;                    // a widget's own frame↔fill stack
            if (A.parent === B.name || B.parent === A.name) continue;   // container ancestry
            const ra = contentRect(A), rb = contentRect(B);
            const ix = Math.max(0, Math.min(ra.x + ra.w, rb.x + rb.w) - Math.max(ra.x, rb.x));
            const iy = Math.max(0, Math.min(ra.y + ra.h, rb.y + rb.h) - Math.max(ra.y, rb.y));
            if (ix <= overlapPx || iy <= overlapPx) continue;           // edge/corner touch
            const areaA = ra.w * ra.h, areaB = rb.w * rb.h;
            const ratio = (ix * iy) / (Math.min(areaA, areaB) || 1);
            if (!ra.shrunk && !rb.shrunk && ratio <= overlapTol) continue; // padding-ghost guard (full-texture rects only)
            // Intended stacking exemptions (≥90% of the smaller sits ON the other):
            //  - a widget/text on a panel, a value overlay on its widget;
            //  - a markedly smaller decoration on its host (icon on a bar frame) —
            //    but NEVER two members of the same cluster (they must sit side by
            //    side; a member fully under its neighbor is the pile-up bug).
            const rowA = contractRowFor(A);
            const rowB = contractRowFor(B);
            if (ratio >= 0.9) {
              if (panelish(A, rowA) || panelish(B, rowB) || String(A.type) === 'Text' || String(B.type) === 'Text') continue;
              const sameCluster = clusterOf[A.name] && clusterOf[A.name] === clusterOf[B.name];
              const smallOnLarge = Math.min(areaA, areaB) / (Math.max(areaA, areaB) || 1) <= 0.45;
              if (smallOnLarge && !sameCluster) continue;
            }
            sceneOverlaps.push({ a: A.name, b: B.name, overlap_px: [Math.round(ix), Math.round(iy)], ratio: Number(ratio.toFixed(3)), a_rect: [Math.round(ra.x), Math.round(ra.y), Math.round(ra.w), Math.round(ra.h)], b_rect: [Math.round(rb.x), Math.round(rb.y), Math.round(rb.w), Math.round(rb.h)] });
            issues.push(`scene '${sceneKey}': '${A.name}' ⨯ '${B.name}' overlap ${Math.round(ix)}×${Math.round(iy)}px (${(ratio * 100).toFixed(0)}% of the smaller; REAL rendered bounds)`);
          }
        }
      }
      // (c) profile-aware size policy on REAL bounds, including parent scaling:
      // art_directed is native 1:1, responsive/minimal permit uniform fit, and
      // pixel_art requires one positive integer uniform scale.
      // Independent of overlap_check. Exempt: Text, and value-scaled fill/track/
      // progress layers — scaling a bar FILL to the live value is the sanctioned
      // technique (the static frame/bg still guards the widget's true size).
      if (sizeCheck) {
        for (const o of candidates) {
          if (String(o.type) === 'Text') continue;
          if (/-(fill|track|progress)$/i.test(String(o.name || ''))) continue;
          const m = manifestRowFor(o);
          if (!m) {
            const row = contractRowFor(o);
            if (RUNTIME_LAYOUT_SOURCES.has(String(row?.sourceStrategy || ''))) {
              continue;
            }
            const contractReason = 'manifest texture contract is missing';
            sceneSizeDrift.push({
              key: o.name,
              instance: o.instance,
              rendered: [Math.round(o.w), Math.round(o.h)],
              texture: null,
              declared_texture: null,
              scale: [null, null],
              profile: profileName,
              reason: contractReason,
              parent: o.parent || null,
            });
            issues.push(
              `scene '${sceneKey}': '${o.name}' violates ${profileName} sizing `
              + `(${contractReason})`,
            );
            continue;
          }
          const declaredWidth = m.width;
          const declaredHeight = m.height;
          const inspected = typeof m.path === 'string' && m.path
            ? inspectRuntimeAssetPath(m.path)
            : null;
          const actual = inspected?.ok && inspected.exists
            ? pngSize(inspected.path)
            : null;
          let contractReason = null;
          if (!inspected) {
            contractReason = 'manifest texture path is missing or unreadable';
          } else if (!inspected.ok) {
            contractReason = `manifest texture path is unsafe (${inspected.reason})`;
          } else if (!inspected.exists) {
            contractReason = 'manifest texture path is missing or unreadable';
          } else if (!actual) {
            contractReason = 'manifest texture is not a readable PNG';
          } else if (
            !Number.isInteger(declaredWidth)
            || declaredWidth <= 0
            || !Number.isInteger(declaredHeight)
            || declaredHeight <= 0
          ) {
            contractReason = 'manifest requires positive finalized width and height';
          } else if (
            declaredWidth !== actual.w
            || declaredHeight !== actual.h
          ) {
            contractReason = (
              `manifest dimensions ${declaredWidth}×${declaredHeight} `
              + `do not match PNG ${actual.w}×${actual.h}`
            );
          }
          if (contractReason) {
            sceneSizeDrift.push({
              key: o.name,
              instance: o.instance,
              rendered: [Math.round(o.w), Math.round(o.h)],
              texture: actual ? [actual.w, actual.h] : null,
              declared_texture: [declaredWidth, declaredHeight],
              scale: [null, null],
              profile: profileName,
              reason: contractReason,
              parent: o.parent || null,
            });
            issues.push(
              `scene '${sceneKey}': '${o.name}' violates ${profileName} sizing `
              + `(${contractReason})`,
            );
            continue;
          }
          const tw = actual.w;
          const th = actual.h;
          const sx = o.w / tw, sy = o.h / th;
          const uniform = sx > 0 && sy > 0
            && Math.abs(sx - sy) / Math.max(sx, sy) <= sizeTol;
          let validScale = uniform;
          let reason = 'non-uniform render scale';
          if (profilePolicy.nativeUiSize) {
            validScale = uniform
              && Math.abs(sx - 1) <= sizeTol
              && Math.abs(sy - 1) <= sizeTol;
            reason = 'art_directed requires native 1:1 rendering';
          } else if (profilePolicy.integerUiScale) {
            const nearest = Math.round((sx + sy) / 2);
            const declaredScales = Array.isArray(m?.integerScales)
              ? m.integerScales
              : [];
            const baseSize = Array.isArray(m?.baseSize) ? m.baseSize : [];
            validScale = uniform && nearest >= 1
              && Math.abs(sx - nearest) <= sizeTol
              && Math.abs(sy - nearest) <= sizeTol
              && declaredScales.includes(nearest)
              && Number(baseSize[0]) === tw
              && Number(baseSize[1]) === th;
            reason = 'pixel_art requires one positive integer uniform scale with texture=baseSize and a declared integerScale';
          } else {
            const fitBox = Array.isArray(m?.maxBox)
              ? m.maxBox
              : (Array.isArray(m?.logicalBox) ? m.logicalBox : null);
            if (
              fitBox
              && fitBox.length === 2
              && Number(fitBox[0]) > 0
              && Number(fitBox[1]) > 0
            ) {
              validScale = uniform
                && o.w <= Number(fitBox[0]) + 1
                && o.h <= Number(fitBox[1]) + 1;
              reason = uniform
                ? 'responsive/minimal render must fit manifest maxBox/logicalBox uniformly'
                : 'non-uniform render scale';
            } else {
              validScale = false;
              reason = 'responsive/minimal requires a positive manifest maxBox/logicalBox';
            }
          }
          if (!validScale) {
            sceneSizeDrift.push({
              key: o.name,
              instance: o.instance,
              rendered: [Math.round(o.w), Math.round(o.h)],
              texture: [tw, th],
              scale: [Number(sx.toFixed(3)), Number(sy.toFixed(3))],
              profile: profileName,
              reason,
              parent: o.parent || null,
            });
            issues.push(`scene '${sceneKey}': '${o.name}' violates ${profileName} sizing (${reason}); rendered scale ${sx.toFixed(3)}×${sy.toFixed(3)}`);
          }
        }
      }
    }

    const coverage = elements.length ? found / elements.length : 1;
    sceneReports.push({ scene: sceneKey, captured: true, total: elements.length, found, coverage: Number(coverage.toFixed(3)), results, overlaps: sceneOverlaps, offscreen: sceneOffscreen, size_drift: sceneSizeDrift });
  }

  const totalElements = sceneReports.reduce((s, r) => s + r.total, 0);
  const totalFound = sceneReports.reduce((s, r) => s + r.found, 0);
  const overallCoverage = totalElements ? totalFound / totalElements : 1;
  const misplaced = sceneReports.flatMap((r) => r.results.filter((x) => x.ok === false && x.reason !== 'not rendered (missing or unnamed)'));
  const uncaptured = sceneReports.filter((r) => !r.captured);
  const allOverlaps = sceneReports.flatMap((r) => (r.overlaps || []).map((o) => ({ scene: r.scene, ...o })));
  const allOffscreen = sceneReports.flatMap((r) => (r.offscreen || []).map((o) => ({ scene: r.scene, ...o })));
  const allSizeDrift = sceneReports.flatMap((r) => (r.size_drift || []).map((o) => ({ scene: r.scene, ...o })));
  const aligned = issues.length === 0 && overallCoverage >= minCoverage;

  const payload = {
    axis: 'layout', applicable: true, status: aligned ? 'passed' : 'failed', aligned,
    run_id: captureRunId,
    quality_profile: qualityProfileName(),
    tolerance: tol, min_coverage: minCoverage, coverage: Number(overallCoverage.toFixed(3)),
    overlap_thresholds: { overlap_check: overlapCheck, overlap_min_px: overlapPx, overlap_tolerance: overlapTol, offscreen_min_px: offscreenPx, size_check: sizeCheck, size_drift_tolerance: sizeTol },
    overlaps: allOverlaps, offscreen: allOffscreen, size_drift: allSizeDrift,
    scenes: sceneReports, issues,
  };
  const wrote = writeLayoutAudit(payload, { blockOnWriteError });

  // Verdicts → gate-style blocking messages, returned instead of emitted here so
  // the same evaluator can be used by a non-blocking materialization MCP tool.
  const blockingMessages = wrote ? [] : ['could not write alignment/layout/alignment.json'];
  for (const issue of issues.filter((x) => /capture manifest|different\/legacy run|stale relative|scene set/.test(x))) {
    blockingMessages.push(`layout evidence invalid — ${issue}`);
  }
  if (renderedFilesFound === 0) {
    blockingMessages.push(`layout alignment: no scene was captured — run capture-layout.mjs to dump verify_middle/layout/<scene>.rendered.json for ${sceneKeys.join(', ')} (Axis 5)`);
    return { ...payload, wrote, blocking_messages: blockingMessages };
  }
  for (const r of uncaptured) {
    blockingMessages.push(`layout alignment: scene '${r.scene}' not captured — run capture-layout.mjs for it (Axis 5)`);
  }
  if (overallCoverage < minCoverage) {
    const missing = sceneReports.flatMap((r) => r.results.filter((x) => x.reason === 'not rendered (missing or unnamed)').map((x) => `${r.scene}/${x.key}`));
    blockingMessages.push(`layout coverage ${(overallCoverage * 100).toFixed(0)}% < ${(minCoverage * 100).toFixed(0)}% — every §2.0.2 element must be rendered + named. Missing: ${missing.slice(0, 8).join(', ')}${missing.length > 8 ? `, +${missing.length - 8} more` : ''}`);
  }
  if (misplaced.length) {
    const preview = misplaced.slice(0, 6).map((x) => x.key).join(', ');
    blockingMessages.push(`layout alignment: ${misplaced.length} element(s) not at their §2.0.2 position — ${preview}${misplaced.length > 6 ? ', …' : ''}. Place from CONFIG.ui (← §2.0.2) / the §2.0.2 spawn position; see alignment/layout/alignment.json`);
  }
  if (allOverlaps.length) {
    const preview = allOverlaps.slice(0, 4).map((o) => `${o.a} ⨯ ${o.b} (${o.overlap_px[0]}×${o.overlap_px[1]}px)`).join('; ');
    blockingMessages.push(`layout overlap: ${allOverlaps.length} HUD widget pair(s) collide on the REAL rendered bounds — ${preview}${allOverlaps.length > 4 ? '; …' : ''}. Move them apart in CONFIG.ui, shrink an asset, or group the same-corner row as a §2.0.2 cluster (addUiCluster spaces members by their real width). Coordinates in alignment/layout/alignment.json overlaps[] are executable.`);
  }
  if (allOffscreen.length) {
    const preview = allOffscreen.slice(0, 4).map((o) => `${o.scene}/${o.key}`).join(', ');
    blockingMessages.push(`layout offscreen: ${allOffscreen.length} element(s) render partly off-canvas (REAL bounds) — ${preview}${allOffscreen.length > 4 ? ', …' : ''}. Fix CONFIG.ui x/y (mind the origin) or the asset size; see alignment/layout/alignment.json offscreen[].`);
  }
  if (allSizeDrift.length) {
    const preview = allSizeDrift.slice(0, 4).map((o) => `${o.key} (${o.scale[0]}×${o.scale[1]}, ${o.reason})`).join('; ');
    blockingMessages.push(`layout size policy: ${allSizeDrift.length} UI element(s) violate ${profileName} scaling on REAL bounds: ${preview}${allSizeDrift.length > 4 ? '; …' : ''}. Preserve aspect ratio; use native 1:1 only for art_directed and integer scale for pixel_art.`);
  }
  return { ...payload, wrote, blocking_messages: blockingMessages };
}

function gateLayoutAlignment() {
  const result = materializeLayoutAlignment();
  for (const msg of result?.blocking_messages || []) fail(msg);
}

export {
  materializeLayoutAlignment,
  gateLayoutAlignment,
} ;
