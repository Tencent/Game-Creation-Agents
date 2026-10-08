/**
 * gate-io.mjs — Dependency-free filesystem / hashing / PNG helpers shared by the
 * bundled verification runners.
 *
 * These are all pure utilities (no gate context, no global state): they take
 * explicit paths / values and return results. Extracted verbatim from
 * gate-check.mjs so the gate modules and the test suite consume one
 * implementation instead of drifting copies.
 */

import { readFileSync, existsSync, readdirSync } from 'fs';
import { createHash } from 'crypto';
import { join } from 'path';
import { inflateSync } from 'node:zlib';

const CANONICAL_URL_PATH_SEGMENT_RE = /^[A-Za-z0-9._~!$&'()*+,;=:@-]+$/;

export function canonicalRelativeBrowserPathIssue(value, requiredRoot = null) {
  if (typeof value !== 'string' || !value) return 'path must be a non-empty string';
  if (/[\u0000-\u001f\u007f]/.test(value)) return 'path contains a control character';
  if (value.includes('\\')) return 'path must use forward slashes';
  if (value.includes('%')) return 'path must not contain percent-encoding';
  if (value.includes('?') || value.includes('#')) {
    return 'path must not contain URL query or fragment delimiters';
  }
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value)) {
    return 'path must be relative';
  }
  const parts = value.split('/');
  if (
    parts.some((part) => (
      !part
      || part === '.'
      || part === '..'
      || !CANONICAL_URL_PATH_SEGMENT_RE.test(part)
    ))
  ) {
    return 'path contains a non-canonical URL segment';
  }
  if (requiredRoot !== null && (parts.length < 2 || parts[0] !== requiredRoot)) {
    return `path must stay under ${requiredRoot}/**`;
  }
  try {
    const parsed = new URL(value, 'https://gca.invalid/');
    if (
      parsed.origin !== 'https://gca.invalid'
      || parsed.pathname !== `/${value}`
      || parsed.search
      || parsed.hash
    ) {
      return 'browser URL canonicalization would change the pathname';
    }
  } catch {
    return 'path is not a canonical browser pathname';
  }
  return null;
}

export function runtimeAssetPathIssue(value) {
  return canonicalRelativeBrowserPathIssue(value, 'assets');
}

export function mergeRuntimeRegistrySnapshots(snapshots) {
  const loaded = new Set();
  const animated = new Set();
  for (const snapshot of snapshots || []) {
    for (const key of snapshot?.loaded_texture_keys || []) {
      if (typeof key === 'string' && key) loaded.add(key);
    }
    for (const key of snapshot?.animation_frame_texture_keys || []) {
      if (typeof key === 'string' && key) animated.add(key);
    }
  }
  return {
    loaded_texture_keys: [...loaded].sort(),
    animation_frame_texture_keys: [...animated].sort(),
  };
}

export function readJSON(p) {
  try { return JSON.parse(readFileSync(p, 'utf-8')); }
  catch { return null; }
}

export function sha256File(p) {
  try { return createHash('sha256').update(readFileSync(p)).digest('hex'); }
  catch { return null; }
}

export function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${stableJson(value[key])}`
    ).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function slotContractSha256(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const contract = {};
  for (const key of [
    'key', 'category', 'sourceStrategy', 'description', 'path', 'metadata',
    'layout', 'defaultAction',
  ]) {
    if (Object.hasOwn(entry, key)) contract[key] = entry[key];
  }
  if (entry.actions && typeof entry.actions === 'object') {
    contract.actions = {};
    for (const name of Object.keys(entry.actions).sort()) {
      const spec = entry.actions[name];
      if (!spec || typeof spec !== 'object') continue;
      contract.actions[name] = {};
      for (const key of ['textureKey', 'path', 'metadata']) {
        if (Object.hasOwn(spec, key)) contract.actions[name][key] = spec[key];
      }
    }
  }
  return createHash('sha256').update(stableJson(contract)).digest('hex');
}

export function gddSlotContextSha256(gddText, key, description, action = null) {
  if (
    typeof gddText !== 'string'
    || typeof key !== 'string'
    || typeof description !== 'string'
    || (action !== null && typeof action !== 'string')
  ) return null;
  const normalize = (value) => value
    .replace(/[ \t\f\v]+/g, ' ')
    .replace(/^[ \t\f\v]+|[ \t\f\v]+$/g, '');
  const cleanCell = (value) => {
    let cleaned = normalize(value);
    if (cleaned.length >= 2 && cleaned.startsWith('`') && cleaned.endsWith('`')) {
      cleaned = normalize(cleaned.slice(1, -1));
    }
    return cleaned;
  };
  const splitRow = (line) => {
    const cells = [];
    let cell = '';
    for (let index = 0; index < line.length; index += 1) {
      if (line[index] === '\\' && line[index + 1] === '|') {
        cell += '|';
        index += 1;
      } else if (line[index] === '|') {
        cells.push(cleanCell(cell));
        cell = '';
      } else {
        cell += line[index];
      }
    }
    cells.push(cleanCell(cell));
    if (cells[0] === '') cells.shift();
    if (cells.at(-1) === '') cells.pop();
    return cells;
  };
  const lines = gddText
    .replace(/\r\n?|\u0085|\u2028|\u2029/g, '\n')
    .split('\n');
  const normalizedDescription = normalize(description);
  const normalizedKey = normalize(key).toLowerCase();
  const normalizedAction = action === null ? null : normalize(action).toLowerCase();
  if (!normalizedDescription || !normalizedKey) return null;

  const validSections = new Set(['2a', '2b', '2c', '2d', '2e']);
  let section = null;
  let columns = null;
  const seenTables = new Set();
  let duplicateTable = false;
  let duplicateMatch = false;
  let inFence = false;
  const authoritative = [];
  for (const line of lines) {
    const stripped = line.replace(/^[ \t\f\v]+|[ \t\f\v]+$/g, '');
    if (stripped.startsWith('```') || stripped.startsWith('~~~')) {
      inFence = !inFence;
      columns = null;
      continue;
    }
    if (inFence || stripped.startsWith('>')) continue;
    if (stripped.startsWith('#')) {
      const title = normalize(stripped.replace(/^#+/, ''));
      const first = (title.split(' ', 1)[0] || '')
        .replace(/[.:]+$/, '')
        .replace(/^§/, '')
        .toLowerCase();
      section = validSections.has(first) ? first : null;
      columns = null;
      continue;
    }
    if (section === null) continue;
    if (!stripped.includes('|')) {
      if (stripped) columns = null;
      continue;
    }
    const cells = splitRow(stripped);
    const lowered = cells.map((cell) => cell.toLowerCase());
    if (lowered.includes('key') && lowered.includes('description')) {
      duplicateTable = seenTables.has(section);
      seenTables.add(section);
      columns = {
        key: lowered.indexOf('key'),
        description: lowered.indexOf('description'),
      };
      if (lowered.includes('action')) columns.action = lowered.indexOf('action');
      continue;
    }
    if (
      columns === null
      || cells.every((cell) => !cell || /^[-:]+$/.test(cell))
      || Math.max(...Object.values(columns)) >= cells.length
    ) continue;
    if (cells[columns.key].toLowerCase() !== normalizedKey) continue;
    const rowAction = Object.hasOwn(columns, 'action')
      ? cells[columns.action].toLowerCase()
      : null;
    if (normalizedAction !== null && rowAction !== normalizedAction) continue;
    if (duplicateTable) {
      duplicateMatch = true;
      continue;
    }
    authoritative.push({
      action: rowAction,
      description: cells[columns.description],
      section,
    });
  }
  if (duplicateMatch || authoritative.length !== 1) return null;
  const [selected] = authoritative;
  if (selected.description !== normalizedDescription) return null;
  return createHash('sha256').update(stableJson({
    action: normalizedAction,
    description: normalizedDescription,
    key: normalizedKey,
    schema_version: 1,
    section: selected.section,
  })).digest('hex');
}

const PNG_SIGNATURE = Buffer.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
]);
const MAX_INFLATED_PNG_BYTES = 256 * 1024 * 1024;
const MAX_COMPRESSED_PNG_BYTES = 256 * 1024 * 1024;
const PNG_CHANNELS = new Map([
  [0, 1], // grayscale
  [2, 3], // RGB
  [3, 1], // indexed
  [4, 2], // grayscale + alpha
  [6, 4], // RGBA
]);
const PNG_BIT_DEPTHS = new Map([
  [0, new Set([1, 2, 4, 8, 16])],
  [2, new Set([8, 16])],
  [3, new Set([1, 2, 4, 8])],
  [4, new Set([8, 16])],
  [6, new Set([8, 16])],
]);
const PNG_CRC_TABLE = Uint32Array.from({ length: 256 }, (_, value) => {
  let crc = value;
  for (let bit = 0; bit < 8; bit += 1) {
    crc = (crc & 1) ? (0xedb88320 ^ (crc >>> 1)) : (crc >>> 1);
  }
  return crc >>> 0;
});

function pngCrc32(parts) {
  let crc = 0xffffffff;
  for (const part of parts) {
    for (const byte of part) {
      crc = PNG_CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngScanlineLayout(w, h, bitDepth, colorType, interlace) {
  const channels = PNG_CHANNELS.get(colorType);
  if (!channels) return null;
  const bitsPerPixel = BigInt(channels * bitDepth);
  const segments = [];
  let expected = 0n;
  const addPass = (startX, startY, stepX, stepY) => {
    if (w <= startX || h <= startY) return;
    const passWidth = Math.ceil((w - startX) / stepX);
    const rows = Math.ceil((h - startY) / stepY);
    const rowBytes = (BigInt(passWidth) * bitsPerPixel + 7n) / 8n;
    expected += BigInt(rows) * (rowBytes + 1n);
    if (expected > BigInt(MAX_INFLATED_PNG_BYTES)) return;
    segments.push({ rowBytes: Number(rowBytes), rows });
  };
  if (interlace === 0) {
    addPass(0, 0, 1, 1);
  } else {
    for (const pass of [
      [0, 0, 8, 8],
      [4, 0, 8, 8],
      [0, 4, 4, 8],
      [2, 0, 4, 4],
      [0, 2, 2, 4],
      [1, 0, 2, 2],
      [0, 1, 1, 2],
    ]) {
      addPass(...pass);
      if (expected > BigInt(MAX_INFLATED_PNG_BYTES)) return null;
    }
  }
  if (expected <= 0n || expected > BigInt(MAX_INFLATED_PNG_BYTES)) return null;
  return { expectedLength: Number(expected), segments };
}

function parsePng(buf) {
  if (
    !Buffer.isBuffer(buf)
    || buf.length < PNG_SIGNATURE.length + 12
    || !buf.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)
  ) return null;

  let offset = PNG_SIGNATURE.length;
  let ihdr = null;
  let sawPLTE = false;
  let paletteEntries = 0;
  let sawTRNS = false;
  let sawIDAT = false;
  let idatEnded = false;
  let sawIEND = false;
  let compressedBytes = 0;
  const idat = [];

  while (offset < buf.length) {
    if (buf.length - offset < 12) return null;
    const length = buf.readUInt32BE(offset);
    if (length > buf.length - offset - 12) return null;
    const typeBytes = buf.subarray(offset + 4, offset + 8);
    if (
      !typeBytes.every((byte) => (
        (byte >= 0x41 && byte <= 0x5a)
        || (byte >= 0x61 && byte <= 0x7a)
      ))
      || (typeBytes[2] & 0x20) !== 0
    ) return null;
    const type = typeBytes.toString('ascii');
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    const expectedCrc = buf.readUInt32BE(dataEnd);
    if (pngCrc32([typeBytes, buf.subarray(dataStart, dataEnd)]) !== expectedCrc) {
      return null;
    }
    if (!ihdr && type !== 'IHDR') return null;
    if (sawIEND) return null;

    if (type === 'IHDR') {
      if (ihdr || offset !== PNG_SIGNATURE.length || length !== 13) return null;
      const w = buf.readUInt32BE(dataStart);
      const h = buf.readUInt32BE(dataStart + 4);
      const bitDepth = buf.readUInt8(dataStart + 8);
      const colorType = buf.readUInt8(dataStart + 9);
      const compression = buf.readUInt8(dataStart + 10);
      const filter = buf.readUInt8(dataStart + 11);
      const interlace = buf.readUInt8(dataStart + 12);
      if (
        w === 0
        || h === 0
        || w > 0x7fffffff
        || h > 0x7fffffff
        || !PNG_BIT_DEPTHS.get(colorType)?.has(bitDepth)
        || compression !== 0
        || filter !== 0
        || (interlace !== 0 && interlace !== 1)
      ) return null;
      ihdr = { w, h, bitDepth, colorType, interlace };
    } else if (type === 'PLTE') {
      if (
        sawPLTE
        || sawIDAT
        || length === 0
        || length % 3 !== 0
        || length > 768
        || ihdr.colorType === 0
        || ihdr.colorType === 4
      ) return null;
      sawPLTE = true;
      paletteEntries = length / 3;
    } else if (type === 'tRNS') {
      if (
        sawTRNS
        || sawIDAT
        || (ihdr.colorType === 3 && !sawPLTE)
        || (ihdr.colorType === 0 && length !== 2)
        || (ihdr.colorType === 2 && length !== 6)
        || (ihdr.colorType === 3 && (length === 0 || length > paletteEntries))
        || ihdr.colorType === 4
        || ihdr.colorType === 6
      ) return null;
      sawTRNS = true;
    } else if (type === 'IDAT') {
      if (idatEnded) return null;
      sawIDAT = true;
      compressedBytes += length;
      if (compressedBytes > MAX_COMPRESSED_PNG_BYTES) return null;
      idat.push(buf.subarray(dataStart, dataEnd));
    } else {
      if (sawIDAT) idatEnded = true;
      if (type === 'IEND') {
        if (length !== 0 || !sawIDAT) return null;
        sawIEND = true;
      } else if ((typeBytes[0] & 0x20) === 0) {
        // Unknown critical chunks cannot be decoded safely.
        return null;
      }
    }

    offset = dataEnd + 4;
    if (type === 'IEND') {
      if (offset !== buf.length) return null;
      break;
    }
  }

  if (
    !ihdr
    || !sawIDAT
    || !sawIEND
    || (ihdr.colorType === 3 && !sawPLTE)
    || (ihdr.colorType === 3 && paletteEntries > (1 << ihdr.bitDepth))
  ) return null;
  const layout = pngScanlineLayout(
    ihdr.w,
    ihdr.h,
    ihdr.bitDepth,
    ihdr.colorType,
    ihdr.interlace,
  );
  if (!layout || compressedBytes === 0) return null;

  let raw;
  try {
    const inflated = inflateSync(Buffer.concat(idat, compressedBytes), {
      info: true,
      maxOutputLength: layout.expectedLength,
    });
    if (inflated.engine.bytesWritten !== compressedBytes) return null;
    raw = inflated.buffer;
  } catch {
    return null;
  }
  if (raw.length !== layout.expectedLength) return null;
  let rawOffset = 0;
  for (const segment of layout.segments) {
    for (let row = 0; row < segment.rows; row += 1) {
      if (raw[rawOffset] > 4) return null;
      rawOffset += segment.rowBytes + 1;
    }
  }
  if (rawOffset !== raw.length) return null;
  return { ...ihdr, hasTRNS: sawTRNS, raw };
}

// PNG dimensions are returned only after the complete file has passed
// signature/chunk/CRC/zlib/scanline validation.
export function pngSize(p) {
  try {
    const parsed = parsePng(readFileSync(p));
    return parsed ? { w: parsed.w, h: parsed.h } : null;
  } catch {
    return null;
  }
}

// Return structural information plus a transparent-pixel fraction for
// non-interlaced 8-bit RGBA images. Invalid or undecodable PNGs fail closed.
export function pngInfo(p) {
  try {
    const parsed = parsePng(readFileSync(p));
    if (!parsed) return null;
    const {
      w, h, bitDepth, colorType, interlace, hasTRNS, raw,
    } = parsed;
    const hasAlpha = colorType === 4 || colorType === 6 || hasTRNS;
    let transparentFraction = null;
    const PIXEL_BUDGET = 6_000_000;
    if (
      colorType === 6
      && bitDepth === 8
      && interlace === 0
      && w * h <= PIXEL_BUDGET
    ) {
      const bpp = 4;
      const stride = w * bpp;
      const paeth = (a, b, c) => {
        const estimate = a + b - c;
        const pa = Math.abs(estimate - a);
        const pb = Math.abs(estimate - b);
        const pc = Math.abs(estimate - c);
        return pa <= pb && pa <= pc ? a : (pb <= pc ? b : c);
      };
      let prev = Buffer.alloc(stride);
      let transparent = 0;
      let pos = 0;
      for (let y = 0; y < h; y += 1) {
        const filterType = raw[pos];
        pos += 1;
        const cur = Buffer.alloc(stride);
        for (let i = 0; i < stride; i += 1) {
          const value = raw[pos + i];
          const left = i >= bpp ? cur[i - bpp] : 0;
          const above = prev[i];
          const upperLeft = i >= bpp ? prev[i - bpp] : 0;
          let decoded;
          switch (filterType) {
            case 1: decoded = value + left; break;
            case 2: decoded = value + above; break;
            case 3: decoded = value + ((left + above) >> 1); break;
            case 4: decoded = value + paeth(left, above, upperLeft); break;
            default: decoded = value;
          }
          cur[i] = decoded & 0xff;
        }
        pos += stride;
        for (let x = 0; x < w; x += 1) {
          // Count only genuinely transparent pixels. Near-opaque matte haze
          // must not be mistaken for a removed background.
          if (cur[x * bpp + 3] < 16) transparent += 1;
        }
        prev = cur;
      }
      transparentFraction = transparent / (w * h);
    }
    return {
      w,
      h,
      bitDepth,
      colorType,
      interlace,
      hasAlpha,
      hasTRNS,
      transparentFraction,
    };
  } catch {
    return null;
  }
}

export function listFiles(dir, predicate = () => true) {
  const files = [];
  if (!existsSync(dir)) return files;
  function walk(d) {
    for (const e of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (predicate(p, e.name)) files.push(p);
    }
  }
  walk(dir);
  return files;
}

export function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}
