/**
 * evidence.mjs — Canonical build-digest computation (Node side).
 *
 * Extracted VERBATIM from gate-check.mjs. The algorithm and the exact file set
 * are the Node half of a Node/Python pair (mirror: gca_mcp.evidence
 * compute_build_digest). Do NOT "fix" any divergence from the Python side here
 * — that would change every existing game's digest. This is a pure move: the
 * caller injects the game paths and a `manifestEntries` extractor so this
 * module stays free of gate context.
 */

import { createHash } from 'crypto';
import { readFileSync, existsSync } from 'fs';
import { join, relative, resolve } from 'path';
import { listFiles, readJSON } from './gate-io.mjs';

export function computeCanonicalBuildDigest({ gameDir, playtestDir, configPath, manifestEntries }) {
  const paths = new Set([
    join(gameDir, 'index.html'),
    join(gameDir, 'GAME_DESIGN.md'),
    join(gameDir, 'assets', 'asset-manifest.json'),
    configPath,
  ]);
  for (const root of [
    join(gameDir, 'src'),
    join(gameDir, 'levels'),
    join(gameDir, 'design'),
    join(playtestDir, 'tests'),
    join(playtestDir, 'inputs'),
  ]) {
    for (const path of listFiles(root, (p, name) => (
      !p.split(/[\\/]/).includes('.archive')
      && !name.endsWith('.tmp')
      && !name.endsWith('.log')
    ))) paths.add(path);
  }
  const manifest = readJSON(join(gameDir, 'assets', 'asset-manifest.json'));
  for (const entry of manifestEntries(manifest)) {
    if (!entry || typeof entry !== 'object') continue;
    const specs = [entry];
    if (entry.actions && typeof entry.actions === 'object') {
      specs.push(...Object.values(entry.actions).filter((value) => value && typeof value === 'object'));
    }
    for (const spec of specs) {
      for (const field of ['path', 'metadata']) {
        const value = spec[field];
        if (typeof value !== 'string' || !value) continue;
        const path = resolve(gameDir, value);
        const rel = relative(gameDir, path);
        if (rel.startsWith('..') || resolve(gameDir, rel) !== path) continue;
        paths.add(path);
        if (field === 'path') {
          paths.add(path.replace(/\.[^.]+$/, '.asset.json'));
          paths.add(path.replace(/\.[^.]+$/, '.bbox.json'));
        }
      }
    }
  }
  const hash = createHash('sha256');
  for (const path of [...paths].filter((p) => existsSync(p)).sort()) {
    const rel = relative(gameDir, path);
    const label = rel.startsWith('..') ? `@config/${path.split(/[\\/]/).pop()}` : rel.split('\\').join('/');
    hash.update(label, 'utf8');
    hash.update(Buffer.from([0]));
    hash.update(readFileSync(path));
    hash.update(Buffer.from([0]));
  }
  return `sha256:${hash.digest('hex')}`;
}
