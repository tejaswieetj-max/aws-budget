export type Budget = { budget_micro: number; available_micro: number; reserved_micro: number; spent_micro: number; period: string };
export type Reservation = { reservation_id: string; status: string; created_at?: string; reserved_micro: number; estimate_micro: number; resource?: { instance_type?: string; ebs_gb?: number }; workflow_start_state?: string; workflow_execution_status?: string };
export type Estimate = { compute_micro: number; storage_micro: number; total_micro: number; reserve_micro?: number; reserve_multiplier?: number; source?: string; price_source?: string; price_snapshot_id?: string; breakdown?: Record<string, number> };
export type Alert = { alert_id: string; alert_type: string; created_at?: string; details?: Record<string, unknown> };
export class SpendctlApi {
  constructor(baseUrl: string, token: () => Promise<string>);
  request<T>(path: string, init?: RequestInit, idempotencyKey?: string): Promise<T>;
  estimate(payload: object): Promise<Estimate>;
  submit(payload: object, key: string): Promise<{ reservation: Reservation; reservation_id: string; workflow?: unknown }>;
  budget(team: string): Promise<{ budget: Budget }>;
  reservations(): Promise<{ items: Reservation[] }>;
  requestById(id: string): Promise<{ request: Reservation }>;
  cancel(id: string): Promise<{ request: Reservation; cleanup_state: string }>;
  updateBudget(team: string, budget_usd: string): Promise<unknown>;
  calibration(): Promise<{ sample_count: number; policies: unknown[] }>;
  alerts(): Promise<{ items: Alert[] }>;
}
export function formatUsd(micro: number | null | undefined): string;
