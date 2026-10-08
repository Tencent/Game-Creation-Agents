#!/usr/bin/env node
/**
 * llm-client.mjs — shared multimodal LLM client for game-verify scripts.
 *
 * One entry point, `callModel(...)`, that sends the pinned Doubao 2.0 model
 * to Volcengine ARK.
 *
 * Transport:
 *   - 'ark-doubao' : ARK OpenAI-compatible endpoint.
 *
 * callModel({ prompt, images, video, ... }) ->
 *   { text, usage, finishReason, attempts } |
 *   { error, errorKind: api|network|response|config|filesystem|executor, retryable, attempts }
 *   images: [{ path }] or [{ mimeType, data(base64) }]
 *   video:  { path } | string path (optional)
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs';
import { resolve, extname, dirname, join } from 'node:path';
import { spawn } from 'node:child_process';

export const DOUBAO_VLM_MODEL = 'doubao-seed-2-0-pro-260215';

export function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ── .env autoload (matches generate_image.py's _load_dotenv convention) ───
// Search upward from cwd and this module for a .env; set vars that aren't
// already in the real environment (real env always wins).
function loadDotenvOnce() {
  if (globalThis.__gcaDotenvLoaded) return;
  globalThis.__gcaDotenvLoaded = true;
  const candidates = [];
  let dir = process.cwd();
  for (let i = 0; i < 7; i++) { candidates.push(join(dir, '.env')); dir = resolve(dir, '..'); }
  try { let md = dirname(new URL(import.meta.url).pathname); for (let i = 0; i < 7; i++) { candidates.push(join(md, '.env')); md = resolve(md, '..'); } } catch {}
  for (const p of candidates) {
    if (!existsSync(p)) continue;
    try {
      for (const raw of readFileSync(p, 'utf-8').split('\n')) {
        const line = raw.trim();
        if (!line || line.startsWith('#')) continue;
        const eq = line.indexOf('=');
        if (eq === -1) continue;
        const key = line.slice(0, eq).trim();
        let val = line.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
        if (key && process.env[key] === undefined) process.env[key] = val;
      }
    } catch {}
  }
}
loadDotenvOnce();

/**
 * Decide which provider to use.
 * Priority: explicit arg > env GAME_VERIFY_PROVIDER > auto.
 * Only the ARK transport is supported.
 */
export function detectProvider({ provider, model } = {}) {
  const explicit = provider || process.env.GAME_VERIFY_PROVIDER;
  if (explicit && explicit !== 'ark-doubao') return null;
  if (model && model !== DOUBAO_VLM_MODEL) return null;
  if (process.env.ARK_API_KEY) return 'ark-doubao';
  return null; // caller should fall back to a non-LLM policy
}

// ── Media helpers ────────────────────────────────────────────────────────
function imageMime(p) {
  return { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' }[extname(p).toLowerCase()] || 'image/png';
}
function videoMime(p) {
  return { '.webm': 'video/webm', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska' }[extname(p).toLowerCase()] || 'video/webm';
}
function asInlineMedia(item, kindMime) {
  if (!item) return null;
  if (typeof item === 'string') item = { path: item };
  if (item.data && item.mimeType) return { mimeType: item.mimeType, data: item.data };
  if (item.path) {
    const buf = readFileSync(resolve(item.path));
    return { mimeType: item.mimeType || kindMime(item.path), data: buf.toString('base64') };
  }
  return null;
}

// ── curl POST — ASYNC (non-blocking) so the event loop / watchdog stay live.
// Uses spawn (no shell) + a hard child-kill timeout. Respects system proxy /
// NO_PROXY because curl reads them from the environment.
function curlPost(url, headers, bodyObj, timeoutSec) {
  return new Promise((resolveP) => {
    const tmp = `/tmp/llm-payload-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`;
    try { writeFileSync(tmp, JSON.stringify(bodyObj)); }
    catch (e) { return resolveP({ ok: false, error: `payload write failed: ${e.message}`, errorKind: 'filesystem' }); }

    const cargs = ['-sS', '--max-time', String(timeoutSec), '-X', 'POST', url, '-H', 'Content-Type: application/json'];
    for (const [k, v] of Object.entries(headers)) { cargs.push('-H', `${k}: ${v}`); }
    cargs.push('--data-binary', `@${tmp}`);

    const child = spawn('curl', cargs);
    let out = '', err = '', done = false;
    const cleanup = () => { try { unlinkSync(tmp); } catch {} };
    // Hard kill a few seconds past curl's own --max-time, in case curl wedges.
    const killTimer = setTimeout(() => { if (!done) { try { child.kill('SIGKILL'); } catch {} } }, (timeoutSec + 5) * 1000);
    killTimer.unref?.();

    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; });
    child.on('error', (e) => { done = true; clearTimeout(killTimer); cleanup(); resolveP({ ok: false, error: `spawn curl failed: ${e.message}`, errorKind: 'executor' }); });
    child.on('close', (code) => {
      if (done) return;
      done = true; clearTimeout(killTimer); cleanup();
      if (code === 0) resolveP({ ok: true, body: out });
      else resolveP({ ok: false, error: (out || err || `curl exit ${code}`).slice(0, 400), errorKind: 'network' });
    });
  });
}

/**
 * Multimodal model call.
 * @returns {Promise<{text:string,usage?:object,finishReason?:string}|{error:string}>}
 */
export async function callModel(opts = {}) {
  const {
    prompt,
    images = [],
    video = null,
    provider,
    maxTokens = 2048,
    temperature = 0.2,
    timeout = 120,
    retries = 0,
    label = '',
  } = opts;

  const prov = detectProvider({ provider, model: opts.model });
  if (!prov) return { error: 'no supported Doubao VLM transport available', errorKind: 'config', retryable: false, attempts: 0 };
  if (opts.model && opts.model !== DOUBAO_VLM_MODEL) {
    return { error: `unsupported VLM model '${opts.model}'`, errorKind: 'config', retryable: false, attempts: 0 };
  }
  if (retries !== 0) {
    return { error: 'VLM retries are disabled; retries must be 0', errorKind: 'config', retryable: false, attempts: 0 };
  }

  const imgMedia = images.map((i) => asInlineMedia(i, imageMime)).filter(Boolean);
  const vidMedia = video ? asInlineMedia(video, videoMime) : null;

  const key = process.env.ARK_API_KEY;
  if (!key) return { error: 'ARK_API_KEY is not set', errorKind: 'config', retryable: false, attempts: 0 };
  const url = process.env.DOUBAO_API_ENDPOINT || 'https://ark.cn-beijing.volces.com/api/v3/chat/completions';
  const headers = { Authorization: `Bearer ${key}`, 'X-Request-ID': `game-verify-${label}-${Date.now()}` };
  const content = [{ type: 'text', text: prompt }];
  for (const m of imgMedia) content.push({ type: 'image_url', image_url: { url: `data:${m.mimeType};base64,${m.data}` } });
  if (vidMedia) content.push({ type: 'video_url', video_url: { url: `data:${vidMedia.mimeType};base64,${vidMedia.data}` } });
  const body = { model: DOUBAO_VLM_MODEL, messages: [{ role: 'user', content }], max_tokens: maxTokens, temperature, stream: false };
  const parse = (j) => {
    if (j.error) return { error: j.error.message || JSON.stringify(j.error), _retryable: /rate|429/i.test(JSON.stringify(j.error)) };
    return { text: j.choices?.[0]?.message?.content || '', usage: j.usage, finishReason: j.choices?.[0]?.finish_reason };
  };

  for (let attempt = 0; attempt <= retries; attempt++) {
    const res = await curlPost(url, headers, body, timeout);
    if (!res.ok) {
      if (attempt < retries) { await sleep(2000 * (attempt + 1)); continue; }
      return { error: `request failed: ${res.error}`, errorKind: res.errorKind || 'network', retryable: true, attempts: attempt + 1, provider: prov };
    }
    let json;
    try { json = JSON.parse(res.body); }
    catch { if (attempt < retries) { await sleep(1500); continue; } return { error: `non-JSON response: ${res.body.slice(0, 200)}`, errorKind: 'response', retryable: true, attempts: attempt + 1, provider: prov }; }
    const parsed = parse(json);
    if (parsed.error && parsed._retryable && attempt < retries) { await sleep(5000); continue; }
    if (parsed.error) {
      const retryable = !!parsed._retryable;
      delete parsed._retryable;
      return { ...parsed, errorKind: 'api', retryable, attempts: attempt + 1, provider: prov };
    }
    delete parsed._retryable;
    parsed.attempts = attempt + 1;
    parsed.provider = prov;
    return parsed;
  }
  return { error: 'exhausted retries', errorKind: 'api', retryable: true, attempts: retries + 1, provider: prov };
}

/** Best-effort JSON extraction from a model reply (handles ```json fences and prose). */
export function extractJson(text) {
  if (!text) return null;
  let t = text.trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  try { return JSON.parse(t); } catch {}
  const first = t.indexOf('{');
  const last = t.lastIndexOf('}');
  if (first !== -1 && last > first) {
    const slice = t.slice(first, last + 1);
    try { return JSON.parse(slice); } catch {}
  }
  return null;
}
