import test from 'node:test';
import assert from 'node:assert/strict';
import { SpendctlApi, formatUsd } from '../src/api.mjs';
import { prepareSubmission } from '../src/idempotency.mjs';

test('API sends ID token and stable request idempotency key', async () => {
  const original = globalThis.fetch;
  let captured;
  globalThis.fetch = async (_url, init) => { captured = init; return new Response('{"reservation_id":"rsv-1"}', { status: 202 }); };
  try {
    const api = new SpendctlApi('https://api.example/v1', async () => 'signed-id-token');
    await api.submit({ team: 'team-a' }, 'stable-key');
    const headers = new Headers(captured.headers);
    assert.equal(headers.get('Authorization'), 'Bearer signed-id-token');
    assert.equal(headers.get('Idempotency-Key'), 'stable-key');
  } finally { globalThis.fetch = original; }
});

test('network error from a keyed request is explicitly ambiguous', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  try {
    const api = new SpendctlApi('https://api.example/v1', async () => 'token');
    await assert.rejects(api.submit({ team: 'team-a' }, 'same-key'), error => error.pendingUnknown === true);
  } finally { globalThis.fetch = original; }
});

test('401 API responses trigger local session clearing and return contract error', async () => {
  const originalFetch = globalThis.fetch;
  const originalDispatch = globalThis.dispatchEvent;
  let event;
  globalThis.dispatchEvent = value => { event = value; return true; };
  globalThis.fetch = async () => new Response('{"error":{"code":"UNAUTHENTICATED","message":"expired"}}', { status: 401 });
  try {
    const api = new SpendctlApi('https://api.example/v1', async () => 'expired');
    await assert.rejects(api.budget('team'), error => error.code === 'UNAUTHENTICATED' && error.status === 401);
    assert.equal(event.type, 'spendctl:unauthorized');
  } finally { globalThis.fetch = originalFetch; globalThis.dispatchEvent = originalDispatch; }
});

test('micro-USD formatting is safe for empty values', () => {
  assert.equal(formatUsd(1_234_567), '$1.23');
  assert.equal(formatUsd(undefined), '—');
});

test('same logical request reuses its idempotency key and changed input cannot overwrite unknown request', () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  const payload = { team: 'team-a', purpose: 'qa' };
  const first = prepareSubmission(storage, payload, () => 'stable-1');
  const replay = prepareSubmission(storage, payload, () => 'must-not-be-used');
  assert.equal(replay.key, first.key);
  assert.throws(() => prepareSubmission(storage, { ...payload, purpose: 'new' }, () => 'stable-2'), error => error.pendingUnknown === true);
  assert.equal(JSON.parse(values.get('spendctl_pending_submission')).key, 'stable-1');
});
