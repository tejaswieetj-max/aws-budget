import test from 'node:test';
import assert from 'node:assert/strict';
import { SpendctlApi, formatUsd } from '../src/api.mjs';

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
