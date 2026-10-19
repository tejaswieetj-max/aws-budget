export type PendingSubmission = { key: string; fingerprint: string; payload: Record<string, unknown> };
export class PendingSubmissionConflict extends Error { pending: PendingSubmission; pendingUnknown: true; }
export function prepareSubmission(storage: Storage, payload: Record<string, unknown>, createKey?: () => string): PendingSubmission;
export function readPendingSubmission(storage: Storage): PendingSubmission | null;
export function clearPendingSubmission(storage: Storage): void;
