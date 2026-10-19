export type Budget = { budget_micro: number; available_micro: number; reserved_micro: number; spent_micro: number; period: string };
export type Reservation = { reservation_id: string; status: string; created_at?: string; reserved_micro: number; estimate_micro: number; resource?: { instance_type?: string; ebs_gb?: number }; workflow_start_state?: string; workflow_execution_status?: string };
export type Estimate = { compute_micro: number; storage_micro: number; total_micro: number; reserve_micro?: number; reserve_multiplier?: number; source?: string; price_source?: string; price_snapshot_id?: string; breakdown?: Record<string, number> };
export type Alert = { alert_id: string; alert_type: string; created_at?: string; details?: Record<string, unknown> };
export type ApiError = Error & { code?: string; status?: number; pendingUnknown?: boolean };
