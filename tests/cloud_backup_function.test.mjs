import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gunzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';

let handler;
globalThis.Deno = {
  env: { get: (name) => ({ SUPABASE_URL: 'https://fixture.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'fixture-server-key' })[name] },
  serve: (value) => { handler = value; },
};
await import('../supabase/functions/coordination-backup/index.ts');

function request(token = 'fixture-server-key', method = 'POST') {
  return new Request('https://fixture.supabase.co/functions/v1/coordination-backup', {
    method, headers: { Authorization: `Bearer ${token}` },
  });
}

function backend({ corrupt = false, raw = '' } = {}) {
  const state = { uploaded: null, recorded: null, calls: 0 };
  const payloadText = raw || JSON.stringify({ format: 'quant-coordination-backup-v1', tables: Object.fromEntries(Array.from({ length: 20 }, (_, i) => ['table' + i, []])) });
  globalThis.fetch = async (url, options = {}) => {
    state.calls++;
    if (options.headers.apikey !== 'fixture-server-key') return Response.json({ error: 'permission denied' }, { status: 403 });
    if (url.endsWith('/quant_export_coordination_backup')) return Response.json(payloadText);
    if (url.includes('/object/authenticated/')) return new Response(corrupt ? 'corrupt' : state.uploaded);
    if (url.includes('/storage/v1/object/')) {
      state.uploaded = options.body;
      return Response.json({ Key: 'fixture' });
    }
    if (url.endsWith('/quant_record_coordination_backup')) {
      state.recorded = JSON.parse(options.body).p_result;
      return Response.json(null);
    }
    throw new Error('Unexpected request');
  };
  return state;
}

test('an anonymous or ordinary JWT cannot export a backup', async () => {
  const state = backend();
  assert.equal((await handler(request('ordinary-user-jwt'))).status, 401);
  assert.equal((await handler(request('', 'POST'))).status, 401);
  assert.equal((await handler(request('fixture-server-key', 'GET'))).status, 405);
  assert.equal(state.calls, 1);
});

test('successful backup is compressed, read back, hashed, and recorded', async () => {
  const state = backend();
  const response = await handler(request());
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.verified, true);
  assert.match(result.object_name, /^coordination\/slot-\d{2}\.json\.gz$/);
  assert.equal(createHash('sha256').update(state.uploaded).digest('hex'), result.sha256);
  const envelope = JSON.parse(gunzipSync(state.uploaded));
  assert.equal(createHash('sha256').update(envelope.payload_text).digest('hex'), envelope.payload_sha256);
  assert.equal(Object.keys(JSON.parse(envelope.payload_text).tables).length, 20);
  assert.deepEqual(state.recorded, Object.fromEntries(Object.entries(result).filter(([key]) => key !== 'verified')));
});

test('corrupted storage readback never creates a successful run record', async () => {
  const state = backend({ corrupt: true });
  assert.equal((await handler(request())).status, 500);
  assert.equal(state.recorded, null);
});

test('a snapshot exceeding the free budget is rejected before upload', async () => {
  const state = backend({ raw: 'x'.repeat(8 * 1024 * 1024 + 1) });
  assert.equal((await handler(request())).status, 500);
  assert.equal(state.uploaded, null);
});
