const KEY = 'spendctl_pending_submission';

/**
 * Persist a logical request's identity before network I/O. Different payloads
 * cannot silently replace a submission whose outcome may be unknown.
 */
export function prepareSubmission(storage, payload, createKey = () => crypto.randomUUID()) {
  const fingerprint = fingerprintPayload(payload);
  const raw = storage.getItem(KEY);
  if (raw) {
    const pending = JSON.parse(raw);
    if (pending.fingerprint !== fingerprint) throw new PendingSubmissionConflict(pending);
    return pending;
  }
  const pending = { key: createKey(), fingerprint, payload };
  storage.setItem(KEY, JSON.stringify(pending));
  return pending;
}

export function readPendingSubmission(storage) {
  try { const raw = storage.getItem(KEY); return raw ? JSON.parse(raw) : null; }
  catch { return null; }
}

export function clearPendingSubmission(storage) { storage.removeItem(KEY); }

export class PendingSubmissionConflict extends Error {
  constructor(pending) {
    super('An earlier submission has an unknown outcome. Retry that exact request before starting a new one.');
    this.pending = pending;
    this.pendingUnknown = true;
  }
}

function fingerprintPayload(payload) {
  let hash = 2166136261;
  for (const character of JSON.stringify(payload)) { hash ^= character.charCodeAt(0); hash = Math.imul(hash, 16777619); }
  return (hash >>> 0).toString(16);
}
