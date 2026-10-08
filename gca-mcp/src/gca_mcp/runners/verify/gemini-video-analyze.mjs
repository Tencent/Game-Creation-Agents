#!/usr/bin/env node
/**
 * gemini-video-analyze.mjs — Analyze recordings with pinned Doubao 2.0 input.
 * The legacy filename is retained for runner-path compatibility.
 *
 * DEFAULT MODE — direct whole-video understanding (fast):
 *   The entire recording is sent to the model in a SINGLE pass. No ffmpeg
 *   splitting, no per-segment calls, and no separate merge call. The one call
 *   returns the final consolidated report directly. This is much faster than
 *   the legacy segmented pipeline (1 API call vs. N segments + merge).
 *
 * LEGACY MODE — segmented analysis (opt-in via --split):
 *   - Long video auto-split: videos >segment-duration are split into segments
 *   - Rubric evidence re-check: per rubric item, extracts a <5s evidence clip
 *     and performs a targeted second-pass verification
 *   - Merges all segment analyses + re-check results into a final report
 *
 * If a video is too large to inline in one request, direct mode first tries to
 * downscale it with ffmpeg; only if that still fails does it fall back to --split.
 *
 * Usage:
 *   # Basic — direct whole-video analysis (default, no splitting)
 *   node gemini-video-analyze.mjs --video recording.webm
 *
 *   # Output to file
 *   node gemini-video-analyze.mjs --video recording.webm --output analysis.json
 *
 *   # Legacy segmented pipeline (split + rubric evidence re-check + merge)
 *   node gemini-video-analyze.mjs --video recording.webm --split --rubric .playtest/inputs/rubric.json
 *
 *   # Control segment length (only relevant with --split)
 *   node gemini-video-analyze.mjs --video recording.webm --split --segment-duration 15
 *
 *   # Skip re-check (only relevant with --split)
 *   node gemini-video-analyze.mjs --video recording.webm --split --rubric rubric.json --no-recheck
 *
 * Environment variables:
 *   ARK_API_KEY          — Volcengine ARK API key
 *   DOUBAO_API_ENDPOINT  — ARK OpenAI-compatible endpoint
 *   GCA_AUTO_INSTALL_FFMPEG — set to 1 to allow auto-installing ffmpeg when it is missing
 *
 * Output: structured JSON with segment analyses, rubric scores, and evidence clips.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, rmSync, readdirSync } from 'fs';
import { resolve, extname, join, basename } from 'path';
import { parseArgs } from 'node:util';
import { execSync } from 'node:child_process';

// ── Ensure ffmpeg/ffprobe are available (lazy check on first actual use) ──
// Auto-install is opt-in: it only runs when GCA_AUTO_INSTALL_FFMPEG=1.
let ffmpegEnsured = false;
function ensureFfmpeg() {
  if (ffmpegEnsured) return;
  ffmpegEnsured = true;
  try {
    execSync('ffprobe -version', { stdio: 'pipe' });
    return; // already working
  } catch { /* not available or broken */ }

  if (process.env.GCA_AUTO_INSTALL_FFMPEG !== '1') {
    console.error('Error: ffmpeg not found — install it or set GCA_AUTO_INSTALL_FFMPEG=1 to auto-install.');
    process.exit(1);
  }

  console.error('[setup] ffmpeg/ffprobe not found or broken, attempting auto-install...');

  const installers = [
    // RHEL / CentOS (dnf)
    { check: 'dnf --version', install: 'dnf install -y ffmpeg && (dnf install -y --nobest compat-openssl10 2>/dev/null || true) && ldconfig' },
    // RHEL / CentOS (yum)
    { check: 'yum --version', install: 'yum install -y ffmpeg && (yum install -y --nobest compat-openssl10 2>/dev/null || true) && ldconfig' },
    // Debian / Ubuntu
    { check: 'apt-get --version', install: 'apt-get update -qq && apt-get install -y ffmpeg' },
    // Alpine
    { check: 'apk --version', install: 'apk add --no-cache ffmpeg' },
  ];

  let installed = false;
  for (const { check, install } of installers) {
    try {
      execSync(check, { stdio: 'pipe' });
      console.error(`[setup] Installing via: ${install.split(' ')[0]}`);
      execSync(install, { stdio: 'pipe', timeout: 120000 });
      installed = true;
      break;
    } catch { continue; }
  }

  if (!installed) {
    // Fallback: download static ffmpeg binary
    console.error('[setup] No package manager worked, trying static ffmpeg binary...');
    try {
      execSync(
        `curl -sL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" | tar -xJ --strip-components=1 -C /usr/local/bin --wildcards '*/ffmpeg' '*/ffprobe'`,
        { stdio: 'pipe', timeout: 120000 }
      );
    } catch (e) {
      console.error('[setup] Static download failed:', e.message);
    }
  }

  // Verify installation
  try {
    const ver = execSync('ffprobe -version', { encoding: 'utf-8', stdio: 'pipe' }).split('\n')[0];
    console.error(`[setup] ✓ ${ver}`);
  } catch {
    console.error('[setup] ⚠ ffmpeg installation failed. Video will be treated as single segment (no splitting/clipping).');
  }
}

// ── CLI args ─────────────────────────────────────────────────────────────
const { values: args } = parseArgs({
  options: {
    video:      { type: 'string' },
    prompt:     { type: 'string' },
    rubric:     { type: 'string' },
    output:     { type: 'string' },
    'state-timeline': { type: 'string' },            // path to state-timeline.json
    endpoint:   { type: 'string' },            // overrides DOUBAO_API_ENDPOINT
    timeout:    { type: 'string', default: '300' },
    'max-tokens': { type: 'string', default: '8192' },
    'segment-duration': { type: 'string', default: '20' },
    'no-recheck': { type: 'boolean', default: false },
    'max-parallel': { type: 'string', default: '1' },
    // Default: direct whole-video analysis (no splitting). Pass --split to use
    // the legacy segmented pipeline (split → per-segment analysis → re-check → merge).
    split: { type: 'boolean', default: false },
  },
});

if (!args.video) {
  console.error(`Usage: node gemini-video-analyze.mjs --video <path-to-video>
  Options:
    --prompt <text>            Custom analysis prompt
    --output <path>            Write results to file instead of stdout
    --endpoint <url>           API endpoint URL (overrides DOUBAO_API_ENDPOINT)
    --timeout <seconds>        Request timeout per call
  Default: direct whole-video analysis (single pass, no splitting).
  Legacy segmented pipeline (opt-in):
    --split                    Split the video and run per-segment analysis + merge
    --rubric <path>            Path to rubric.json (enables evidence re-check; needs --split)
    --segment-duration <sec>   Segment length in seconds (default: 20; needs --split)
    --no-recheck               Skip rubric evidence re-check pass (needs --split)
    --max-parallel <n>         Max parallel API calls (default: 1; needs --split)`);
  process.exit(1);
}

const SEGMENT_DURATION = Number(args['segment-duration']);
const MAX_PARALLEL = Number(args['max-parallel']);
const TIMEOUT = Number(args.timeout);
const MAX_TOKENS = Number(args['max-tokens']);
// Max video bytes that can be inlined (base64) into a single request. The model
// The provider rejects payloads above ~20MB, so direct whole-video mode keeps the
// uploaded file under this; larger inputs are downscaled or fall back to --split.
const MAX_INLINE_BYTES = 20 * 1024 * 1024;

// ── API credentials ──────────────────────────────────────────────────────

function requireArkKey() {
  const arkKey = process.env.ARK_API_KEY;
  if (!arkKey) {
    console.error('Error: ARK_API_KEY not set.');
    process.exit(1);
  }
  return arkKey;
}

// ── Helpers ──────────────────────────────────────────────────────────────

function getMimeType(filePath) {
  const ext = extname(filePath).toLowerCase();
  return { '.webm': 'video/webm', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska' }[ext] || 'video/webm';
}

function getVideoDuration(videoPath) {
  ensureFfmpeg();
  let duration = NaN;

  // Method 1: ffprobe -show_format
  try {
    const out = execSync(
      `ffprobe -v quiet -print_format json -show_format "${videoPath}"`,
      { encoding: 'utf-8' }
    );
    duration = parseFloat(JSON.parse(out).format.duration);
  } catch {}

  // Method 2: ffprobe decode packets (handles webm without duration metadata)
  if (!duration || isNaN(duration)) {
    try {
      const out = execSync(
        `ffprobe -v error -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 "${videoPath}" | tail -1`,
        { encoding: 'utf-8', timeout: 30000 }
      );
      duration = parseFloat(out.trim());
      if (duration > 0) {
        console.error(`  Duration from packet scan: ${duration.toFixed(1)}s`);
      }
    } catch {}
  }

  // Method 3: state-timeline.json timestamps
  if (!duration || isNaN(duration)) {
    const timelinePath = resolve(videoPath, '..', 'state-timeline.json');
    if (existsSync(timelinePath)) {
      try {
        const timeline = JSON.parse(readFileSync(timelinePath, 'utf-8'));
        if (timeline.length > 0) {
          const lastEntry = timeline[timeline.length - 1];
          duration = (lastEntry.timestamp || lastEntry.time || lastEntry.t || 0) / 1000;
          if (duration > 0) {
            console.error(`  Duration from state-timeline: ${duration.toFixed(1)}s`);
          }
        }
      } catch {}
    }
  }

  // Method 4: estimate from file size (webm ~80KB/s at 800x450)
  if (!duration || isNaN(duration)) {
    const fileSizeBytes = readFileSync(videoPath).length;
    const estimatedDuration = fileSizeBytes / (80 * 1024);
    console.error(`  Warning: duration unknown. Estimated from file size: ${estimatedDuration.toFixed(1)}s`);
    duration = Math.max(estimatedDuration, 10);
  }

  return duration;
}

// ── Parse event time string to absolute seconds ─────────────────────────
function parseEventTime(timeStr, segStartSec) {
  if (timeStr == null) return null;
  const str = String(timeStr).trim();

  // Handle "42s", "42.0s"
  const simpleSec = str.match(/^(\d+(?:\.\d+)?)\s*s?$/);
  if (simpleSec) return segStartSec + parseFloat(simpleSec[1]);

  // Handle "0s - 5s", "42s - 60s"  → take the start
  const rangeMatch = str.match(/^(\d+(?:\.\d+)?)\s*s?\s*[-–]\s*(\d+(?:\.\d+)?)\s*s?$/);
  if (rangeMatch) return segStartSec + parseFloat(rangeMatch[1]);

  // Handle "00:42", "01:24" (mm:ss)
  const mmss = str.match(/^(\d+):(\d+)$/);
  if (mmss) return parseInt(mmss[1]) * 60 + parseInt(mmss[2]);

  // Handle "42.0s - 60.0s" with decimals
  const rangeDecimal = str.match(/^(\d+\.\d+)\s*s?\s*[-–]/);
  if (rangeDecimal) return segStartSec + parseFloat(rangeDecimal[1]);

  return null;
}

// ── Find best timestamp for a rubric item based on segment analysis data ──
function findBestTimestamp(rubricItem, parsedEvents, videoDuration) {
  const itemLower = rubricItem.toLowerCase();

  // Extract keywords from the rubric item for matching
  const itemKeywords = itemLower
    .replace(/[()\/,]/g, ' ')
    .split(/\s+/)
    .filter(w => w.length > 3 && !['with', 'when', 'from', 'that', 'this', 'does', 'have', 'show', 'each'].includes(w));

  // Score each event by keyword overlap with the rubric item
  let bestScore = 0;
  let bestTime = videoDuration * 0.3; // fallback: 30% into video

  for (const ev of parsedEvents) {
    const evText = ev.text.toLowerCase();
    let score = 0;

    for (const kw of itemKeywords) {
      if (evText.includes(kw)) score += 1;
    }

    // Bonus for matching event type
    if (ev.type === 'effect' && (itemLower.includes('effect') || itemLower.includes('explo') || itemLower.includes('flash') || itemLower.includes('particle'))) {
      score += 2;
    }
    if (ev.type === 'issue' && (itemLower.includes('artifact') || itemLower.includes('bug') || itemLower.includes('error'))) {
      score += 2;
    }

    // Specific rubric-to-event associations
    if (itemLower.includes('damage') && (evText.includes('damage') || evText.includes('hp') || evText.includes('hit'))) score += 2;
    if (itemLower.includes('death') && (evText.includes('death') || evText.includes('destroy') || evText.includes('killed'))) score += 2;
    if (itemLower.includes('explo') && (evText.includes('explo') || evText.includes('burst') || evText.includes('flash'))) score += 2;
    if (itemLower.includes('heart') && (evText.includes('heart') || evText.includes('health') || evText.includes('hp'))) score += 2;
    if (itemLower.includes('game over') && (evText.includes('game over') || evText.includes('dies') || evText.includes('death'))) score += 2;
    if (itemLower.includes('invincib') && (evText.includes('invincib') || evText.includes('flash') || evText.includes('blink') || evText.includes('damage'))) score += 2;

    if (score > bestScore) {
      bestScore = score;
      bestTime = ev.time;
    }
  }

  // If no good match found, use heuristic fallback
  if (bestScore === 0) {
    if (itemLower.includes('boot') || itemLower.includes('start') || itemLower.includes('spawn')) {
      bestTime = 2;
    } else if (itemLower.includes('game over') || itemLower.includes('end') || itemLower.includes('restart')) {
      bestTime = videoDuration * 0.85;
    } else {
      bestTime = videoDuration * 0.3;
    }
  }

  return Math.max(0, Math.min(bestTime, videoDuration - 5));
}

// ── Find relevant segment analysis context for a rubric item at a given time ──
function findSegmentContext(rubricItem, parsedEvents, clipStartSec, clipDuration) {
  const itemLower = rubricItem.toLowerCase();
  const clipEnd = clipStartSec + clipDuration;

  // Find events in or near the clip time window (± 3s tolerance)
  const tolerance = 3;
  const relevantEvents = parsedEvents.filter(ev =>
    ev.time >= (clipStartSec - tolerance) && ev.time <= (clipEnd + tolerance)
  );

  if (relevantEvents.length === 0) return null;

  // Filter to most relevant ones by keyword match
  const itemKeywords = itemLower
    .replace(/[()\/,]/g, ' ')
    .split(/\s+/)
    .filter(w => w.length > 3);

  const scored = relevantEvents.map(ev => {
    const evText = ev.text.toLowerCase();
    let score = 0;
    for (const kw of itemKeywords) {
      if (evText.includes(kw)) score++;
    }
    return { ...ev, relevance: score };
  });

  // Sort by relevance, take top 5
  scored.sort((a, b) => b.relevance - a.relevance);
  const top = scored.slice(0, 5);

  // Format as context string
  return top.map(ev =>
    `- [${ev.time.toFixed(1)}s] (${ev.type}): ${ev.text}`
  ).join('\n');
}

function splitVideo(videoPath, segmentDuration, outputDir) {
  ensureFfmpeg();
  mkdirSync(outputDir, { recursive: true });
  const ext = extname(videoPath);
  execSync(
    `ffmpeg -y -i "${videoPath}" -c copy -segment_time ${segmentDuration} -f segment -reset_timestamps 1 "${join(outputDir, `seg_%03d${ext}`)}"`,
    { encoding: 'utf-8', stdio: 'pipe' }
  );
  const segments = readdirSync(outputDir)
    .filter(f => f.startsWith('seg_'))
    .sort()
    .map(f => join(outputDir, f));
  return segments;
}

function extractClip(videoPath, startSec, durationSec, outputPath) {
  ensureFfmpeg();
  mkdirSync(resolve(outputPath, '..'), { recursive: true });
  execSync(
    `ffmpeg -y -ss ${startSec} -i "${videoPath}" -t ${durationSec} -c copy "${outputPath}"`,
    { encoding: 'utf-8', stdio: 'pipe' }
  );
  return outputPath;
}

// ── Downscale a too-large video so it fits in a single inline request ─────
// Returns the path to a compressed file under MAX_INLINE_BYTES, or null if
// compression is unavailable / still too large (caller should fall back to --split).
function compressVideo(videoPath, outputDir) {
  ensureFfmpeg();
  mkdirSync(outputDir, { recursive: true });
  const outPath = join(outputDir, `compressed_${basename(videoPath, extname(videoPath))}.mp4`);
  // Cap width at 640px, drop audio, moderate CRF — fast and broadly supported.
  const attempts = [
    `ffmpeg -y -i "${videoPath}" -vf "scale='min(640,iw)':-2" -c:v libx264 -preset veryfast -crf 30 -an "${outPath}"`,
    `ffmpeg -y -i "${videoPath}" -vf "scale='min(480,iw)':-2" -c:v libx264 -preset veryfast -crf 34 -an "${outPath}"`,
  ];
  for (const cmd of attempts) {
    try {
      execSync(cmd, { encoding: 'utf-8', stdio: 'pipe', timeout: 180000 });
      if (existsSync(outPath) && readFileSync(outPath).length <= MAX_INLINE_BYTES) {
        return outPath;
      }
    } catch { /* try next, or give up */ }
  }
  return null;
}

// ── Sleep helper ─────────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Pinned VLM policy ─────────────────────────────────────────────────────

const DOUBAO_VLM_MODEL = 'doubao-seed-2-0-pro-260215';

function vlmEndpoint() {
  const url = args.endpoint || process.env.DOUBAO_API_ENDPOINT
    || 'https://ark.cn-beijing.volces.com/api/v3/chat/completions';
  return { provider: 'ark-doubao', endpoint: url, model: DOUBAO_VLM_MODEL };
}

function isRateLimited(errMsg) {
  const low = String(errMsg || '').toLowerCase();
  return low.includes('rate') || low.includes('exhausted') || low.includes('quota') || low.includes('429');
}

// ── Pinned Doubao model API call ──────────────────────────────────────────

// One round-trip to a SINGLE model (ep). Returns { text, usage, finish_reason }
// on success, or { error, rateLimited } on failure (never throws).
async function callOnce(videoPath, prompt, label, ep) {
  const videoData = readFileSync(resolve(videoPath)).toString('base64');
  const mimeType = getMimeType(videoPath);

  if (
    ep.provider !== 'ark-doubao'
    || ep.model !== DOUBAO_VLM_MODEL
  ) {
    return { error: 'VLM endpoint violates the pinned Doubao 2.0 policy', rateLimited: false };
  }
  const payload = JSON.stringify({
    model: ep.model,
    messages: [{
      role: 'user',
      content: [
        { type: 'text', text: prompt },
        { type: 'video_url', video_url: { url: `data:${mimeType};base64,${videoData}` } },
      ],
    }],
    max_tokens: MAX_TOKENS,
    temperature: 0.1,
    stream: false,
  });

  const url = ep.endpoint;
  const authHeader = requireArkKey();

  try {
    const target = new URL(url);
    if (!['http:', 'https:'].includes(target.protocol)) {
      throw new Error(`unsupported API endpoint protocol: ${target.protocol}`);
    }
    const httpResponse = await fetch(target, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${authHeader}`,
        'X-Request-ID': `game-verify-${label}-${Date.now()}`,
      },
      body: payload,
      redirect: 'error',
      signal: AbortSignal.timeout(TIMEOUT * 1000),
    });
    const maxResponseBytes = 50 * 1024 * 1024;
    const declaredLength = Number(httpResponse.headers.get('content-length') || 0);
    if (Number.isFinite(declaredLength) && declaredLength > maxResponseBytes) {
      throw new Error(`API response too large: ${declaredLength} bytes`);
    }
    const result = await httpResponse.text();
    if (Buffer.byteLength(result, 'utf8') > maxResponseBytes) {
      throw new Error(`API response too large: over ${maxResponseBytes} bytes`);
    }

    const response = JSON.parse(result);
    if (!httpResponse.ok && !response.error) {
      return {
        error: `HTTP ${httpResponse.status}: ${result.substring(0, 200)}`,
        rateLimited: httpResponse.status === 429,
      };
    }

    if (response.error) {
      const msg = response.error.message || JSON.stringify(response.error);
      return {
        error: msg,
        rateLimited: httpResponse.status === 429 || isRateLimited(msg) || response.error.code === 429,
      };
    }
    const text = response.choices?.[0]?.message?.content || '';
    if (!text.trim()) return { error: 'empty response text', rateLimited: false };
    return {
      label, text,
      model: ep.model,
      usage: {
        promptTokenCount: response.usage?.prompt_tokens || 0,
        candidatesTokenCount: response.usage?.completion_tokens || 0,
        totalTokenCount: response.usage?.total_tokens || 0,
        thoughtsTokenCount: 0,
      },
      finish_reason: response.choices?.[0]?.finish_reason || 'stop',
    };
  } catch (e) {
    return { error: e.message?.substring(0, 200), rateLimited: isRateLimited(e.message) };
  }
}

// Run exactly one request against the pinned model.
async function callVlm(videoPath, prompt, label = '') {
  const fileSize = readFileSync(resolve(videoPath)).length;
  if (fileSize > 20 * 1024 * 1024) {
    console.error(`  [${label}] SKIP: segment too large (${(fileSize/1024/1024).toFixed(1)}MB > 20MB)`);
    return { error: 'segment too large', label };
  }

  const ep = vlmEndpoint();
  const result = await callOnce(videoPath, prompt, label, ep);
  return result.error
    ? { error: `VLM_REQUEST_FAILED — ${ep.model}: ${result.error}`, label }
    : result;
}

// ── Parallel execution with concurrency limit ────────────────────────────

async function parallelMap(items, fn, maxConcurrency) {
  const results = new Array(items.length);
  let index = 0;

  async function worker() {
    while (index < items.length) {
      const i = index++;
      if (i > 0) await sleep(15000); // 15s gap between API calls to avoid rate limits
      results[i] = await fn(items[i], i);
    }
  }

  const workers = Array.from({ length: Math.min(maxConcurrency, items.length) }, () => worker());
  await Promise.all(workers);
  return results;
}

// ── Prompts ──────────────────────────────────────────────────────────────

function buildSegmentPrompt(segIndex, totalSegs, segStartSec, stateSlice) {
  let stateContext = '';
  if (stateSlice && stateSlice.length > 0) {
    stateContext = `\n\n## Ground-Truth Game State (from state-extractor, authoritative)
The following is the ACTUAL game state captured programmatically during this segment. Use this to VERIFY your visual observations — if the state data contradicts what you think you see, TRUST THE STATE DATA.

\`\`\`json
${JSON.stringify(stateSlice, null, 1)}
\`\`\`

Key state fields:
- \`custom.score\`: actual score value
- \`custom.playerHP\`: actual player health
- \`custom.isGameOver\`: whether game ended
- \`groups.enemies.active\`: number of active enemies
- \`groups.playerBullets.active\`: bullets in flight
- \`objects[]\`: all game objects with positions (x, y) and velocities (vx, vy)
- Player position is in objects with texture "player" or in custom properties

Use this state data to:
- Confirm whether enemies actually died (group count decreases)
- Confirm whether player took damage (HP decreased)
- Understand why camera moved (player x position relative to world)
- Determine if effects occurred (objects with "explosion" texture appearing)
- Verify if player reached world edge vs disappeared (check player x vs world width)
`;
  }

  return `You are analyzing segment ${segIndex + 1}/${totalSegs} of a game recording (starts at ${segStartSec}s).
${stateContext}
For THIS segment only, report:
1. **Events**: List every observable game event with its approximate timestamp (relative to segment start). Cross-reference with state data when available.
2. **Visual elements**: Characters, enemies, projectiles, effects, UI changes visible in this segment.
3. **Issues found**: Only report as a BUG if the state data confirms abnormal behavior. If the state shows the player at world edge, camera stopping is EXPECTED. If state shows explosion objects spawning, the effect EXISTS even if brief.
4. **Effects & feedback**: Note ALL visual effects you see — explosions, flashes, particles, screen shake, damage indicators, death animations. Cross-reference with state data (look for explosion objects in the objects list).

CRITICAL RULES:
- If state data shows an "explosion" texture object appearing when an enemy dies → the explosion effect EXISTS. Report it as present even if visually brief.
- If player x position is near world width → player reaching the edge is NORMAL, not a bug.
- If camera moves to follow player position changes → camera is working correctly.
- If score increases while enemies.active decreases → enemies are being killed (even if off-screen).
- DO NOT report "missing feature" or "bug" unless the state data supports it.

Respond in JSON format:
{
  "segment": ${segIndex + 1},
  "time_range": "${segStartSec}s - ${segStartSec + SEGMENT_DURATION}s",
  "events": [{"time": "Xs", "description": "..."}],
  "visual_elements": ["..."],
  "effects_observed": [{"time": "Xs", "type": "...", "description": "color, size, duration"}],
  "issues": [{"time": "Xs", "severity": "high|medium|low", "description": "...", "state_evidence": "what state data shows"}],
  "notes": "..."
}`;
}

// ── Whole-video (direct mode) ground-truth state context ──────────────────
function buildWholeVideoStateContext(stateTimeline) {
  if (!stateTimeline || stateTimeline.length === 0) return '';

  // Downsample evenly so the prompt stays token-bounded on long recordings.
  const MAX_SNAPSHOTS = 80;
  let snaps = stateTimeline;
  if (snaps.length > MAX_SNAPSHOTS) {
    const step = Math.ceil(snaps.length / MAX_SNAPSHOTS);
    snaps = snaps.filter((_, i) => i % step === 0);
  }

  return `\n\n## Ground-Truth Game State (from state-extractor, authoritative)
These are ACTUAL game-state snapshots captured programmatically across the WHOLE recording. Each snapshot's \`t\` field is the time in seconds from the start of the video. Use this to VERIFY your visual observations — if the state data contradicts what you think you see, TRUST THE STATE DATA.

\`\`\`json
${JSON.stringify(snaps, null, 1)}
\`\`\`

Use this state data to:
- Confirm whether enemies actually died (group counts decrease) or the player took damage (HP decreases).
- Understand camera movement (player x relative to world width) and scene transitions.
- Determine if effects occurred (e.g. objects with an "explosion" texture appearing).
`;
}

// ── Whole-video (direct mode) analysis prompt ─────────────────────────────
// Produces the FINAL consolidated report in one pass — no segmentation, no
// separate merge step. Mirrors the section structure of buildMergePrompt but
// is grounded in the model watching the entire video directly.
function buildWholeVideoPrompt(duration, stateContext) {
  return `You are a senior game-quality analyst. You are watching a COMPLETE game recording (~${duration.toFixed(0)}s). The ENTIRE video is provided in this single request — it has NOT been split. Watch it end to end and produce a final, consolidated report in one pass.
${stateContext}
Report on ALL of the following, using ABSOLUTE timestamps (seconds from the start of the video):

1. **Game Overview**: type, genre, overall impression.
2. **Complete Event Timeline**: every observable gameplay event with its absolute timestamp. Cross-reference with the state data when available.
3. **All Issues Found**: a deduplicated list, each with timestamp, severity (high|medium|low), and description. Only report something as a BUG if the visuals (and state data, when present) confirm abnormal behavior.
4. **Visual Effects Inventory**: every distinct effect observed (explosions, flashes, particles, screen shake, damage indicators, death animations, environmental effects). Note brief effects too — short duration does NOT mean absent.
5. **Quality Scores (0-10, 10 = excellent)**: Visual Polish, Animation, Gameplay Flow, Bug Severity, Overall — each with a one-line rationale. Use "N/A" only when truly unobservable.
6. **Priority Fix/Testing List**: the top 5 items ordered by impact.

CRITICAL RULES:
- When state data shows an "explosion" texture object appearing as an enemy dies → the explosion effect EXISTS; report it even if visually brief.
- If the player's x position is near the world width → reaching the edge is NORMAL, not a bug.
- If the camera follows player position → the camera is working correctly.
- If score increases while active enemy count decreases → enemies are being killed (even off-screen).
- Do NOT report a "missing feature" or "bug" unless the evidence supports it. Do NOT fabricate mechanics.

Respond in well-structured markdown.`;
}

function buildRecheckPrompt(rubricItem, dimension, segmentContext, stateSlice) {
  const contextBlock = segmentContext
    ? `\n**Segment analysis context (from Pass 1 — what was already observed around this timestamp):**\n${segmentContext}\n\nUse this context to guide your observation. If the segment analysis already confirmed an effect exists (e.g., via state data like "explosionsActive: 2"), and you can see ANY visual manifestation of it in this clip (even brief), score it as PASS.\n`
    : '';

  const stateBlock = stateSlice && stateSlice.length > 0
    ? `\n**Game state data during this clip (programmatic ground truth):**\n\`\`\`json\n${JSON.stringify(stateSlice, null, 2)}\n\`\`\`\n\nThis state data is 100% accurate — it comes from the game engine directly. Use it to confirm or deny what you see in the video. For example:\n- If state shows "explosionsActive: 2", explosions ARE happening in the game even if visually brief.\n- If state shows "playerHP" decreasing, the player IS taking damage.\n- If state shows "enemyCount" decreasing, enemies ARE being destroyed.\n`
    : '';

  return `You are performing a PRECISE verification of ONE specific rubric item from a game quality checklist.

**Rubric item to verify (${dimension}):**
"${rubricItem}"
${contextBlock}${stateBlock}
Watch this short video clip (<5 seconds) VERY carefully, frame by frame. Your ONLY job is to determine whether this specific item passes or fails based on what you observe IN THE VIDEO combined with the state data.

Rules:
- Look for BRIEF effects (even 1-3 frames / 50-150ms). Short duration does NOT mean absent.
- A flash of color at an enemy's death position = explosion effect EXISTS (even if small/brief).
- State data is ground truth. If state confirms an event (e.g., explosionsActive > 0), and you see ANY corresponding visual change → score 1.
- Be precise: describe exactly what you see happen frame-by-frame at the critical moment.
- Only score 0 if BOTH the video shows nothing AND state data does not confirm the event.

Respond in JSON:
{
  "item": "${rubricItem}",
  "dimension": "${dimension}",
  "score": 0 or 1,
  "confidence": "high|medium|low",
  "evidence": "Precise description of what you observed in video + what state data confirms"
}`;
}

function buildMergePrompt(segmentResults, rubricScores) {
  return `You are a game quality evaluator. You have received analyses from multiple video segments of a game recording, plus rubric verification results. Synthesize them into a final consolidated report.

## Segment Analyses:
${segmentResults.map(r => `### Segment ${r.label}\n${r.text}`).join('\n\n')}

${rubricScores ? `## Rubric Re-check Results:\n${JSON.stringify(rubricScores, null, 2)}` : ''}

## Your Task:
Produce a FINAL consolidated analysis:

1. **Game Overview**: Type, genre, overall impression
2. **Complete Event Timeline**: Merge all segment timelines into one (use absolute timestamps)
3. **All Issues Found**: Deduplicated list with timestamps, severity, and description
4. **Visual Effects Inventory**: Every distinct effect observed across all segments (explosions, flashes, particles, etc.)
5. **Quality Scores** (0-10): Visual Polish, Animation, Gameplay Flow, Bug Severity, Overall
6. **Priority Fix List**: Top 5 issues ordered by impact

${rubricScores ? `7. **Final Rubric Scores**: Use the re-check results as the authoritative scores. If a re-check says an item passes with evidence, trust it over the segment analysis.` : ''}

Be precise and critical. Respond in well-structured markdown.`;
}

// ── Main pipeline ────────────────────────────────────────────────────────

async function main() {
  const videoPath = resolve(args.video);
  if (!existsSync(videoPath)) { console.error(`Error: Video not found: ${videoPath}`); process.exit(1); }

  const startTime = Date.now();
  const fileSize = readFileSync(videoPath).length;
  console.error(`=== Doubao 2.0 Video Analysis V2 ===`);
  console.error(`Video: ${videoPath} (${(fileSize/1024/1024).toFixed(1)}MB)`);

  // ── Step 1: Get duration and decide on splitting ──────────────────────
  const duration = getVideoDuration(videoPath);
  console.error(`Duration: ${duration.toFixed(1)}s | Segment size: ${SEGMENT_DURATION}s`);

  let segments = [];
  let evidenceDir = null;

  // Determine evidence output directory: next to the output file (preferred)
  // or the video. With the canonical per-scenario output path
  // .playtest/verify/recording/<scenario>/video-analysis.json this resolves to
  // .playtest/verify/recording/<scenario>/evidence/.
  if (args.output) {
    evidenceDir = join(resolve(args.output, '..'), 'evidence');
  } else {
    evidenceDir = join(resolve(videoPath, '..'), 'evidence');
  }
  // NOTE: do NOT create evidenceDir up front. In direct whole-video mode no
  // evidence is produced, so creating it would leave an empty `evidence/` dir.
  // splitVideo()/extractClip() create their own subdirs (recursively) only when
  // the legacy --split / rubric re-check paths actually write evidence.

  // ── Decide mode: direct whole-video (default) vs legacy segmented (--split) ──
  //   'direct' → one pass over the whole video, no splitting and no merge call.
  //   'split'  → legacy pipeline (split → per-segment analysis → re-check → merge).
  let mode = args.split ? 'split' : 'direct';
  let uploadPath = videoPath; // the file actually sent to the model in direct mode

  if (mode === 'direct' && fileSize > MAX_INLINE_BYTES) {
    // Too large to inline in one request — try to downscale before giving up on
    // direct mode. Only if that fails do we fall back to the segmented pipeline.
    console.error(`Video is ${(fileSize / 1024 / 1024).toFixed(1)}MB (> ${(MAX_INLINE_BYTES / 1024 / 1024) | 0}MB inline limit); attempting downscale...`);
    const compressed = compressVideo(videoPath, evidenceDir);
    if (compressed) {
      uploadPath = compressed;
      console.error(`  Downscaled → ${compressed} (${(readFileSync(compressed).length / 1024 / 1024).toFixed(1)}MB)`);
    } else {
      console.error(`  Downscale unavailable/insufficient; falling back to segmented (--split) mode.`);
      mode = 'split';
    }
  }

  if (mode === 'split' && duration > SEGMENT_DURATION + 5) {
    // Split video into segments — save in evidence dir
    const segDir = join(evidenceDir, 'segments');
    mkdirSync(segDir, { recursive: true });
    console.error(`Splitting into ~${Math.ceil(duration / SEGMENT_DURATION)} segments...`);
    segments = splitVideo(videoPath, SEGMENT_DURATION, segDir);
    console.error(`  Created ${segments.length} segments → ${segDir}`);
  } else if (mode === 'split') {
    segments = [videoPath];
    console.error(`Video short enough for single-pass segmented analysis`);
  } else {
    // direct mode: the whole (possibly downscaled) video is the single unit
    segments = [uploadPath];
    console.error(`Direct whole-video analysis (no splitting) → single pass`);
  }

  // ── Load state timeline if available ────────────────────────────────────
  let stateTimeline = null;
  const stateTimelinePath = args['state-timeline']
    ? resolve(args['state-timeline'])
    : join(resolve(videoPath, '..'), 'state-timeline.json');

  if (existsSync(stateTimelinePath)) {
    try {
      stateTimeline = JSON.parse(readFileSync(stateTimelinePath, 'utf-8'));
      console.error(`State timeline loaded: ${stateTimeline.length} snapshots from ${stateTimelinePath}`);
    } catch (e) {
      console.error(`Warning: Could not parse state timeline: ${e.message}`);
    }
  } else {
    console.error(`No state-timeline.json found (looked at ${stateTimelinePath}). Analyzing video-only.`);
  }

  // Helper: get state data for a time range
  function getStateSlice(startSec, endSec) {
    if (!stateTimeline) return null;
    return stateTimeline.filter(s => s.t >= startSec && s.t < endSec);
  }

  // ── Pass 1: video analysis (direct = one whole-video call; split = per-segment) ──
  let segmentResults;
  if (mode === 'direct') {
    const label = 'whole-video';
    console.error(`\n── Pass 1: Direct whole-video analysis ──`);
    const stateContext = buildWholeVideoStateContext(stateTimeline);
    console.error(`  [${label}] Analyzing 0s-${duration.toFixed(0)}s${stateTimeline ? ` (${stateTimeline.length} state snapshots)` : ''}...`);
    const result = await callVlm(uploadPath, buildWholeVideoPrompt(duration, stateContext), label);
    if (result.error) {
      console.error(`  [${label}] ERROR: ${result.error}`);
    } else {
      console.error(`  [${label}] Done (${result.usage?.candidatesTokenCount || '?'} tokens)`);
    }
    segmentResults = [result];
  } else {
    console.error(`\n── Pass 1: Segment analysis (${segments.length} segments, max ${MAX_PARALLEL} parallel) ──`);
    segmentResults = await parallelMap(segments, async (segPath, i) => {
      const segStart = i * SEGMENT_DURATION;
      const segEnd = Math.min(segStart + SEGMENT_DURATION, duration);
      const label = `seg${i + 1}`;
      const stateSlice = getStateSlice(segStart, segEnd);
      console.error(`  [${label}] Analyzing ${segStart}s-${segEnd}s${stateSlice ? ` (${stateSlice.length} state snapshots)` : ''}...`);
      const prompt = buildSegmentPrompt(i, segments.length, segStart, stateSlice);
      const result = await callVlm(segPath, prompt, label);
      if (result.error) {
        console.error(`  [${label}] ERROR: ${result.error}`);
      } else {
        console.error(`  [${label}] Done (${result.usage?.candidatesTokenCount || '?'} tokens)`);
      }
      return result;
    }, MAX_PARALLEL);
  }

  // ── Step 3: Rubric evidence re-check (legacy segmented mode only) ─────
  // Direct whole-video mode already inspects the full recording at full
  // fidelity, so the clip-based re-check (which exists to counter per-segment
  // myopia) is redundant and would re-introduce slow sequential calls. Use
  // --split if you want clip-level rubric re-check.
  let rubricRecheckResults = null;

  if (mode === 'direct' && args.rubric && !args['no-recheck']) {
    console.error(`\nNote: --rubric ignored in direct mode (no clip re-check). Pass --split for clip-level rubric re-check.`);
  }

  if (mode === 'split' && args.rubric && !args['no-recheck'] && existsSync(resolve(args.rubric))) {
    const rubric = JSON.parse(readFileSync(resolve(args.rubric), 'utf-8'));
    console.error(`\n── Pass 2: Rubric evidence re-check ──`);

    // Parse segment results to find timestamps for rubric items
    const allEvents = [];
    for (const r of segmentResults) {
      if (r.error || !r.text) continue;
      try {
        const parsed = JSON.parse(r.text);
        if (parsed.events) allEvents.push(...parsed.events.map(e => ({ ...e, segment: r.label })));
        if (parsed.effects_observed) allEvents.push(...parsed.effects_observed.map(e => ({ ...e, segment: r.label })));
      } catch {
        // Segment response wasn't strict JSON, that's ok
      }
    }

    // Identify which rubric items need re-check (focus on failed or uncertain items)
    // Strategy: check items related to visual feedback, effects, animations — things easily missed
    const recheckItems = [];
    const recheckKeywords = ['explo', 'effect', 'flash', 'animation', 'feedback', 'visual', 'particle', 'death', 'hit', 'damage', 'heart', 'health', 'decrease'];

    for (const dim of ['intention', 'visual', 'interaction']) {
      if (!rubric[dim]) continue;
      for (const item of rubric[dim]) {
        const itemLower = item.toLowerCase();
        if (recheckKeywords.some(kw => itemLower.includes(kw))) {
          recheckItems.push({ item, dimension: dim });
        }
      }
    }

    if (recheckItems.length > 0) {
      console.error(`  Found ${recheckItems.length} rubric items needing evidence re-check`);

      // ── Parse ALL events/effects from segment analyses with timestamps ──
      const parsedSegmentData = [];
      for (const r of segmentResults) {
        if (r.error || !r.text) continue;
        try {
          // Handle markdown-wrapped JSON
          let jsonText = r.text;
          const jsonMatch = jsonText.match(/```json\s*([\s\S]*?)\s*```/);
          if (jsonMatch) jsonText = jsonMatch[1];
          const parsed = JSON.parse(jsonText);

          const segIndex = segmentResults.indexOf(r);
          const segStartSec = segIndex * SEGMENT_DURATION;

          // Collect events with absolute timestamps
          if (parsed.events) {
            for (const ev of parsed.events) {
              const t = parseEventTime(ev.time, segStartSec);
              if (t !== null) {
                parsedSegmentData.push({ time: t, text: ev.description || '', type: 'event' });
              }
            }
          }
          // Collect effects with absolute timestamps
          if (parsed.effects_observed) {
            for (const ef of parsed.effects_observed) {
              const t = parseEventTime(ef.time, segStartSec);
              if (t !== null) {
                parsedSegmentData.push({ time: t, text: `${ef.type || ''} ${ef.description || ''}`, type: 'effect' });
              }
            }
          }
          // Collect issues with absolute timestamps
          if (parsed.issues) {
            for (const issue of parsed.issues) {
              const t = parseEventTime(issue.time, segStartSec);
              if (t !== null) {
                parsedSegmentData.push({ time: t, text: `${issue.description || ''}`, type: 'issue' });
              }
            }
          }
        } catch { /* segment response wasn't parseable */ }
      }

      console.error(`  Parsed ${parsedSegmentData.length} events/effects from segment analyses`);

      // ── For each rubric item, find the best matching timestamp from segment data ──
      const clipDir = join(evidenceDir, 'evidence-clips');
      mkdirSync(clipDir, { recursive: true });
      const clipDuration = Math.min(5, SEGMENT_DURATION); // < 5s for focused evidence
      const clipCache = new Map(); // startSec → clipPath (avoid extracting same clip twice)

      const recheckTasks = [];
      for (const { item, dimension } of recheckItems) {
        const bestTime = findBestTimestamp(item, parsedSegmentData, duration);
        // Snap to a clip start time (align to avoid overlapping short clips)
        const clipStart = Math.max(0, Math.min(Math.floor(bestTime), Math.floor(duration - clipDuration)));

        let clipInfo = clipCache.get(clipStart);
        if (!clipInfo) {
          const clipPath = join(clipDir, `evidence_clip_${clipStart}s.webm`);
          try {
            extractClip(videoPath, clipStart, clipDuration, clipPath);
            if (existsSync(clipPath) && readFileSync(clipPath).length > 1000) {
              clipInfo = { path: clipPath, startSec: clipStart };
              clipCache.set(clipStart, clipInfo);
            }
          } catch (e) {
            console.error(`  Warning: clip extraction failed at ${clipStart}s: ${e.message}`);
          }
        }

        if (clipInfo) {
          // Find relevant segment context for this rubric item
          const segContext = findSegmentContext(item, parsedSegmentData, clipInfo.startSec, clipDuration);
          recheckTasks.push({ item, dimension, clip: clipInfo, segmentContext: segContext });
        }
      }

      console.error(`  Extracted ${clipCache.size} evidence clips (${clipDuration}s each)`);

      console.error(`  Running ${recheckTasks.length} re-check verifications (model: ${DOUBAO_VLM_MODEL})...`);

      rubricRecheckResults = await parallelMap(recheckTasks, async (task, i) => {
        const label = `recheck-${i + 1}`;
        console.error(`    [${label}] "${task.item.substring(0, 50)}..." (clip@${task.clip.startSec}s)`);

        // Get state data slice for this clip's time range
        const stateSlice = getStateSlice(task.clip.startSec, task.clip.startSec + clipDuration);
        const prompt = buildRecheckPrompt(task.item, task.dimension, task.segmentContext, stateSlice);

        const result = await callVlm(task.clip.path, prompt, label);
        if (result.error) {
          console.error(`    [${label}] ERROR: ${result.error}`);
          return { item: task.item, dimension: task.dimension, score: 0, confidence: 'low', evidence: `API error: ${result.error}` };
        }
        try {
          // Handle markdown-wrapped JSON
          let jsonText = result.text;
          const jsonMatch = jsonText.match(/```json\s*([\s\S]*?)\s*```/);
          if (jsonMatch) jsonText = jsonMatch[1];
          const parsed = JSON.parse(jsonText);
          console.error(`    [${label}] → score=${parsed.score}, confidence=${parsed.confidence}`);
          return { ...parsed, model: result.model };
        } catch {
          // Try to extract score from non-JSON response
          const scoreMatch = result.text?.match(/"score"\s*:\s*(\d)/);
          return { item: task.item, dimension: task.dimension, score: scoreMatch ? Number(scoreMatch[1]) : 0, confidence: 'low', evidence: result.text?.substring(0, 200), model: result.model };
        }
      }, MAX_PARALLEL);
    } else {
      console.error(`  No rubric items match re-check criteria, skipping`);
    }
  }

  // ── Step 4: Final report ──────────────────────────────────────────────
  // Direct mode: the single whole-video call already produced the final
  // consolidated report — no separate merge call needed.
  // Split mode: merge all segment analyses (+ re-check) into the final report.
  let finalResult;
  if (mode === 'direct') {
    finalResult = segmentResults[0];
  } else {
    console.error(`\n── Pass 3: Merging ${segmentResults.filter(r => !r.error).length} segment analyses ──`);
    const validSegments = segmentResults.filter(r => !r.error && r.text);
    const mergePrompt = buildMergePrompt(validSegments, rubricRecheckResults);
    finalResult = await callVlm(
      // Use first segment (or full video if short) as visual context for merge
      segments.length === 1 ? segments[0] : segments[Math.floor(segments.length / 2)],
      mergePrompt,
      'final-merge'
    );
  }

  // ── Step 5: Compile output ────────────────────────────────────────────
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.error(`\n── Complete in ${elapsed}s ──`);

  const totalUsage = {
    prompt_tokens: 0, video_tokens: 0, output_tokens: 0, thinking_tokens: 0, total_tokens: 0, api_calls: 0,
  };
  // In direct mode finalResult IS segmentResults[0] — don't count it twice.
  const usageSources = mode === 'direct'
    ? [...segmentResults, ...(rubricRecheckResults || [])]
    : [...segmentResults, ...(rubricRecheckResults || []), finalResult];
  for (const r of usageSources.filter(Boolean)) {
    if (r.usage) {
      totalUsage.prompt_tokens += r.usage.promptTokenCount || 0;
      totalUsage.video_tokens += (r.usage.promptTokensDetails?.find(d => d.modality === 'VIDEO')?.tokenCount || 0);
      totalUsage.output_tokens += r.usage.candidatesTokenCount || 0;
      totalUsage.thinking_tokens += r.usage.thoughtsTokenCount || 0;
      totalUsage.total_tokens += r.usage.totalTokenCount || 0;
    }
    totalUsage.api_calls++;
  }

  const analysisErrors = [
    ...segmentResults
      .filter((result) => result?.error || !result?.text)
      .map((result, index) => `segment ${index + 1}: ${result?.error || 'missing analysis text'}`),
    ...(finalResult?.error ? [`final: ${finalResult.error}`] : []),
    ...(!finalResult?.text && !finalResult?.error ? ['final: missing analysis text'] : []),
  ];
  const analysisStatus = analysisErrors.length === 0 ? 'completed' : 'failed';
  const output = {
    video: videoPath,
    // Model/account that produced the final consolidated analysis — derived
    // from the account actually used (not the --model flag, which is inert).
    model: finalResult?.model || null,
    mode, // 'direct' (single whole-video pass) or 'split' (segmented + merge)
    analysis_status: analysisStatus,
    analysis_errors: analysisErrors,
    timestamp: new Date().toISOString(),
    duration_seconds: Number(elapsed),
    video_duration: duration,
    segments_analyzed: segments.length,
    recheck_items: rubricRecheckResults?.length || 0,
    evidence_dir: existsSync(evidenceDir) ? evidenceDir : null,
    usage: totalUsage,
    // Raw analyses (one entry per pass; a single whole-video entry in direct mode)
    segment_analyses: segmentResults.map((r, i) => ({
      segment: i + 1,
      time_range: mode === 'direct'
        ? `0s - ${duration.toFixed(1)}s`
        : `${i * SEGMENT_DURATION}s - ${Math.min((i + 1) * SEGMENT_DURATION, duration)}s`,
      ...(r.error ? { error: r.error } : { analysis: r.text, model: r.model }),
    })),
    // Rubric re-check results (split mode only)
    rubric_recheck: rubricRecheckResults || null,
    // Final consolidated analysis
    analysis: finalResult?.text || (finalResult?.error ? `Analysis failed: ${finalResult.error}` : 'Analysis failed'),
    finish_reason: finalResult?.finish_reason,
  };

  // Evidence files are kept in evidenceDir for review (only when produced).
  if (existsSync(evidenceDir)) console.error(`Evidence saved to: ${evidenceDir}`);

  const jsonOutput = JSON.stringify(output, null, 2);
  if (args.output) {
    writeFileSync(resolve(args.output), jsonOutput);
    console.error(`Results written to: ${resolve(args.output)}`);
  } else {
    console.log(jsonOutput);
  }
  if (analysisStatus !== 'completed') {
    throw new Error(`Video analysis incomplete: ${analysisErrors.join('; ')}`);
  }
}

main().catch(err => {
  console.error('Fatal error:', err.message);
  process.exit(2);
});
