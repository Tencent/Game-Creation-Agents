/**
 * gca-config.mjs — Shared minimal gca.config reader for the bundled Node
 * verification runners.
 *
 * A dependency-free YAML *subset* parser (nested maps + scalars) plus the
 * canonical config loader. Extracted verbatim from gate-check.mjs so
 * gate-check and element-visual-diff read config through one implementation
 * (they previously carried near-identical copies). The gate-check variant is
 * the superset kept here: it additionally supports a `.json` config and
 * validates that the parsed result is a plain object.
 */

import { readFileSync, existsSync } from 'fs';

export function stripYamlComment(line) {
  let quote = null;
  let escaped = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (escaped) { escaped = false; continue; }
    if (ch === '\\') { escaped = true; continue; }
    if (ch === '"' || ch === "'") {
      quote = quote === ch ? null : (quote === null ? ch : quote);
      continue;
    }
    if (ch === '#' && quote === null) return line.slice(0, i);
  }
  return line;
}

export function parseYamlScalar(value) {
  const trimmed = value.trim();
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) ||
      (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1);
  }
  const lowered = trimmed.toLowerCase();
  if (lowered === 'true') return true;
  if (lowered === 'false') return false;
  if (lowered === 'null' || lowered === 'none') return null;
  if (/^-?\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10);
  if (/^-?\d+\.\d+$/.test(trimmed)) return Number.parseFloat(trimmed);
  return trimmed;
}

export function parseSimpleYaml(text) {
  const root = {};
  const stack = [{ indent: -1, value: root }];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = stripYamlComment(rawLine).replace(/\s+$/, '');
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    const stripped = line.trim();
    const colon = stripped.indexOf(':');
    if (colon < 0) throw new Error(`Invalid YAML line: ${rawLine}`);
    const key = stripped.slice(0, colon).trim();
    const rawValue = stripped.slice(colon + 1).trim();
    while (stack.length && indent <= stack[stack.length - 1].indent) stack.pop();
    if (!stack.length) throw new Error(`Invalid YAML indentation: ${rawLine}`);
    const parent = stack[stack.length - 1].value;
    if (rawValue === '') {
      const child = {};
      parent[key] = child;
      stack.push({ indent, value: child });
    } else {
      parent[key] = parseYamlScalar(rawValue);
    }
  }
  return root;
}

export function readGcaConfigFrom(configPath) {
  if (!existsSync(configPath)) {
    throw new Error(`Required config authority is missing: ${configPath}`);
  }
  const text = readFileSync(configPath, 'utf-8');
  const config = configPath.endsWith('.json')
    ? JSON.parse(text)
    : parseSimpleYaml(text);
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error(`Config authority must parse to an object: ${configPath}`);
  }
  return config;
}
