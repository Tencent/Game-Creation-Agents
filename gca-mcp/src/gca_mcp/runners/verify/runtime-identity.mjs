import { createHash } from 'node:crypto';


export const RUNTIME_IDENTITY_OPTIONS = {
  'epoch-url': { type: 'string' },
  'runtime-token-url': { type: 'string' },
  'runtime-token': { type: 'string' },
  'runtime-receipt-sha256': { type: 'string' },
  'pipeline-run-id': { type: 'string' },
  'build-digest': { type: 'string' },
  'verify-run-id': { type: 'string' },
};


function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}


function requireArg(args, key) {
  const value = args[key];
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`runtime identity requires --${key}`);
  }
  return value;
}


function canonicalUrl(value) {
  return new URL(value).href;
}


export async function verifyRuntimeIdentity(page, args) {
  const epochUrl = requireArg(args, 'epoch-url');
  const tokenUrl = requireArg(args, 'runtime-token-url');
  const token = requireArg(args, 'runtime-token');
  const receiptSha256 = requireArg(args, 'runtime-receipt-sha256');
  const pipelineRunId = requireArg(args, 'pipeline-run-id');
  const buildDigest = requireArg(args, 'build-digest');
  const verifyRunId = requireArg(args, 'verify-run-id');

  if (args.url !== epochUrl) {
    throw new Error(`runtime URL mismatch: --url=${args.url} but --epoch-url=${epochUrl}`);
  }
  if (canonicalUrl(page.url()) !== canonicalUrl(epochUrl)) {
    throw new Error(`runtime page redirected: ${page.url()} does not equal ${epochUrl}`);
  }

  const fetched = await page.evaluate(async ({ runtimeTokenUrl }) => {
    const response = await fetch(runtimeTokenUrl, {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'cache-control': 'no-cache' },
    });
    return {
      ok: response.ok,
      status: response.status,
      url: response.url,
      redirected: response.redirected,
      text: await response.text(),
    };
  }, { runtimeTokenUrl: tokenUrl });

  if (!fetched.ok) {
    throw new Error(`runtime token fetch failed: HTTP ${fetched.status} from ${tokenUrl}`);
  }
  if (fetched.redirected || canonicalUrl(fetched.url) !== canonicalUrl(tokenUrl)) {
    throw new Error(`runtime token URL redirected: ${fetched.url} does not equal ${tokenUrl}`);
  }
  if (sha256(fetched.text) !== receiptSha256) {
    throw new Error('runtime token receipt bytes do not match the current verify run');
  }

  let receipt;
  try {
    receipt = JSON.parse(fetched.text);
  } catch (error) {
    throw new Error(`runtime token response is not JSON: ${error.message}`);
  }
  const expected = {
    schema_version: 1,
    producer: 'gca-mcp.begin_verify_run',
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    verify_run_id: verifyRunId,
    url: epochUrl,
    runtime_token: token,
    runtime_token_sha256: sha256(token),
    runtime_token_url: tokenUrl,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (receipt?.[key] !== value) {
      throw new Error(`runtime token receipt ${key} mismatch`);
    }
  }

  return {
    schema_version: 1,
    source: 'runtime-identity.mjs',
    verified: true,
    verified_at: new Date().toISOString(),
    epoch_url: epochUrl,
    page_url: epochUrl,
    runtime_token_url: tokenUrl,
    fetched_url: tokenUrl,
    runtime_token_sha256: expected.runtime_token_sha256,
    runtime_receipt_sha256: receiptSha256,
    pipeline_run_id: pipelineRunId,
    build_digest: buildDigest,
    verify_run_id: verifyRunId,
  };
}
