import type { Alert, ApiError, Budget, Estimate, Reservation } from './types';

export class SpendctlApi {
  constructor(private baseUrl: string, private token: () => Promise<string>) {}

  async request<T>(path: string, init: RequestInit = {}, idempotencyKey?: string): Promise<T> {
    const bearer = await this.token();
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${bearer}`);
    if (init.body) headers.set('Content-Type', 'application/json');
    if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, '')}${path}`, { ...init, headers });
    } catch (cause) {
      const error = new Error('Network outcome is unknown. Retry this request with the same key.') as ApiError;
      error.pendingUnknown = Boolean(idempotencyKey);
      throw error;
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body?.error?.message ?? `Request failed (${response.status})`) as ApiError;
      error.code = body?.error?.code;
      error.status = response.status;
      error.pendingUnknown = error.code === 'PENDING_UNKNOWN' || response.status >= 500;
      throw error;
    }
    return body as T;
  }

  estimate(payload: object) { return this.request<Estimate>('/estimate', { method: 'POST', body: JSON.stringify(payload) }); }
  submit(payload: object, key: string) { return this.request<{ reservation: Reservation; reservation_id: string; workflow?: unknown }>('/requests', { method: 'POST', body: JSON.stringify(payload) }, key); }
  budget(team: string) { return this.request<{ budget: Budget }>(`/budgets/${encodeURIComponent(team)}`); }
  reservations() { return this.request<{ items: Reservation[] }>('/reservations'); }
  requestById(id: string) { return this.request<{ request: Reservation }>(`/requests/${encodeURIComponent(id)}`); }
  cancel(id: string) { return this.request<{ request: Reservation; cleanup_state: string }>(`/requests/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); }
  updateBudget(team: string, budget_usd: string) { return this.request(`/budgets/${encodeURIComponent(team)}`, { method: 'PUT', body: JSON.stringify({ budget_usd }) }); }
  calibration() { return this.request<{ sample_count: number; policies: unknown[] }>('/calibration/summary'); }
  alerts() { return this.request<{ items: Alert[] }>('/alerts'); }
}

export function formatUsd(micro: number | null | undefined): string {
  if (micro == null || !Number.isFinite(micro)) return '—';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(micro / 1_000_000);
}
