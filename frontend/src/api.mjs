/** Typed-at-the-boundary API wrapper; server authorization remains authoritative. */
export class SpendctlApi {
  constructor(baseUrl, token) { this.baseUrl = baseUrl; this.token = token; }
  async request(path, init = {}, idempotencyKey) {
    const bearer = await this.token();
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${bearer}`);
    if (init.body) headers.set('Content-Type', 'application/json');
    if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), 15_000);
    let response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, '')}${path}`, { ...init, headers, signal: controller.signal });
    } catch {
      const error = new Error('Network outcome is unknown. Retry this request with the same key.');
      error.pendingUnknown = Boolean(idempotencyKey);
      throw error;
    } finally { globalThis.clearTimeout(timeout); }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) globalThis.dispatchEvent?.(new Event('spendctl:unauthorized'));
      const error = new Error(body?.error?.message ?? `Request failed (${response.status})`);
      error.code = body?.error?.code;
      error.status = response.status;
      error.pendingUnknown = error.code === 'PENDING_UNKNOWN' || response.status >= 500;
      throw error;
    }
    return body;
  }
  estimate(payload) { return this.request('/estimate', { method: 'POST', body: JSON.stringify(payload) }); }
  submit(payload, key) { return this.request('/requests', { method: 'POST', body: JSON.stringify(payload) }, key); }
  budget(team) { return this.request(`/budgets/${encodeURIComponent(team)}`); }
  reservations() { return this.request('/reservations'); }
  requestById(id) { return this.request(`/requests/${encodeURIComponent(id)}`); }
  cancel(id) { return this.request(`/requests/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); }
  updateBudget(team, budget_usd) { return this.request(`/budgets/${encodeURIComponent(team)}`, { method: 'PUT', body: JSON.stringify({ budget_usd }) }); }
  calibration() { return this.request('/calibration/summary'); }
  alerts() { return this.request('/alerts'); }
}
export function formatUsd(micro) {
  if (micro == null || !Number.isFinite(micro)) return '—';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(micro / 1_000_000);
}
