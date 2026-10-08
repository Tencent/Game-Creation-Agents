import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import {
  verifyRuntimeIdentity,
} from '../../../src/gca_mcp/runners/verify/runtime-identity.mjs';


const sha256 = (value) => createHash('sha256').update(value).digest('hex');


function fixture() {
  const epochUrl = 'http://localhost:8080/';
  const tokenUrl = `${epochUrl}.playtest/verify/runtime/runtime-token.json?verify_run_id=verify-1`;
  const token = 'runtime-secret';
  const receipt = {
    schema_version: 1,
    producer: 'gca-mcp.begin_verify_run',
    pipeline_run_id: 'pipeline-1',
    build_digest: 'build-1',
    verify_run_id: 'verify-1',
    url: epochUrl,
    runtime_token: token,
    runtime_token_sha256: sha256(token),
    runtime_token_url: tokenUrl,
  };
  const text = JSON.stringify(receipt);
  const args = {
    url: epochUrl,
    'epoch-url': epochUrl,
    'runtime-token-url': tokenUrl,
    'runtime-token': token,
    'runtime-receipt-sha256': sha256(text),
    'pipeline-run-id': receipt.pipeline_run_id,
    'build-digest': receipt.build_digest,
    'verify-run-id': receipt.verify_run_id,
  };
  return { args, epochUrl, tokenUrl, text };
}


function pageFixture(pageUrl, response) {
  return {
    url: () => pageUrl,
    evaluate: async () => response,
  };
}


test('runtime identity binds the page and fetched receipt URLs', async () => {
  const { args, epochUrl, tokenUrl, text } = fixture();
  const evidence = await verifyRuntimeIdentity(
    pageFixture(epochUrl, {
      ok: true,
      status: 200,
      url: tokenUrl,
      redirected: false,
      text,
    }),
    args,
  );

  assert.equal(evidence.verified, true);
  assert.equal(evidence.page_url, epochUrl);
  assert.equal(evidence.fetched_url, tokenUrl);
  assert.equal(evidence.runtime_receipt_sha256, sha256(text));
});


test('runtime identity rejects page and token redirects', async () => {
  const { args, epochUrl, tokenUrl, text } = fixture();
  await assert.rejects(
    verifyRuntimeIdentity(
      pageFixture('http://localhost:9999/', {
        ok: true,
        status: 200,
        url: tokenUrl,
        redirected: false,
        text,
      }),
      args,
    ),
    /runtime page redirected/,
  );
  await assert.rejects(
    verifyRuntimeIdentity(
      pageFixture(epochUrl, {
        ok: true,
        status: 200,
        url: 'http://localhost:9999/runtime-token.json',
        redirected: true,
        text,
      }),
      args,
    ),
    /runtime token URL redirected/,
  );
});
