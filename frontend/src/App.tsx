import { useEffect, useMemo, useState } from 'react';
import type React from 'react';
import { useSpendctlAuth } from './auth';
import { SpendctlApi, formatUsd } from './api.mjs';
import type { Alert, Budget, Estimate, Reservation } from './types';

type Page = 'dashboard' | 'estimate' | 'requests' | 'budgets' | 'alerts' | 'research';
const pageLabels: Record<Page, string> = { dashboard: 'Overview', estimate: 'Cost estimator', requests: 'Requests', budgets: 'Budgets', alerts: 'Alerts', research: 'Calibration' };

export function App() {
  const auth = useSpendctlAuth();
  const [page, setPage] = useState<Page>('dashboard');
  const [notice, setNotice] = useState('');
  const [team, setTeam] = useState('');
  const [budget, setBudget] = useState<Budget | null>(null);
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [summary, setSummary] = useState<{ sample_count: number; policies: unknown[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [instanceType, setInstanceType] = useState('t3.micro');
  const [ebsGb, setEbsGb] = useState(8);
  const [runtime, setRuntime] = useState(1);
  const [purpose, setPurpose] = useState('');
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [budgetUsd, setBudgetUsd] = useState('');
  const [selectedRequest, setSelectedRequest] = useState<Reservation | null>(null);

  const claims = auth.user?.profile ?? {};
  const trustedTeam = String(claims['custom:team'] ?? claims.team ?? '');
  const groupsClaim = claims['cognito:groups'];
  const groups = Array.isArray(groupsClaim) ? groupsClaim.map(String) : typeof groupsClaim === 'string' ? groupsClaim.replace(/[\[\]'\"]/g, '').split(/[ ,]+/) : [];
  const isAdmin = groups.includes('admins');
  const api = useMemo(() => new SpendctlApi(import.meta.env.VITE_API_BASE_URL ?? '', async () => {
    if (!auth.user?.id_token) throw new Error('Your session expired. Sign in again.');
    return auth.user.id_token;
  }), [auth.user?.id_token]);

  useEffect(() => { setTeam(trustedTeam); }, [trustedTeam]);
  useEffect(() => {
    const handleUnauthorized = () => { void auth.removeUser(); };
    window.addEventListener('spendctl:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('spendctl:unauthorized', handleUnauthorized);
  }, [auth.removeUser]);
  useEffect(() => {
    if (!auth.isAuthenticated || !team || page === 'estimate') return;
    let active = true;
    setBusy(true); setNotice('');
    const jobs: Promise<unknown>[] = [];
    if (['dashboard', 'budgets'].includes(page)) jobs.push(api.budget(team).then(r => { if (active) setBudget(r.budget); }));
    if (['dashboard', 'requests'].includes(page)) jobs.push(api.reservations().then(r => { if (active) setReservations(r.items); }));
    if (page === 'alerts' && isAdmin) jobs.push(api.alerts().then(r => { if (active) setAlerts(r.items); }));
    if (page === 'research' && isAdmin) jobs.push(api.calibration().then(r => { if (active) setSummary(r); }));
    Promise.all(jobs).catch(e => active && setNotice(errorText(e))).finally(() => active && setBusy(false));
    return () => { active = false; };
  }, [api, auth.isAuthenticated, isAdmin, page, team, refreshToken]);

  if (auth.isLoading || auth.activeNavigator) return <main className="center"><div className="spinner" aria-label="Loading sign-in" /> Loading secure session…</main>;
  if (auth.error) return <main className="center"><section className="auth-card"><h1>Sign-in failed</h1><p role="alert">{auth.error.message}</p><button onClick={() => auth.signinRedirect()}>Try again</button></section></main>;
  if (!auth.isAuthenticated) return <main className="center"><section className="auth-card"><div className="brand-mark">S</div><p className="eyebrow">CLOUD SPEND CONTROL</p><h1>Spend with confidence.</h1><p>Reserve budget before infrastructure starts. Your team access is verified by Cognito and the API.</p><button onClick={() => auth.signinRedirect()}>Continue with secure sign-in</button><small>Sign-in uses your configured Cognito hosted UI.</small></section></main>;
  if (!trustedTeam) return <main className="center"><section className="auth-card"><h1>Team claim missing</h1><p role="alert">Your verified sign-in has no team claim. Ask an administrator to assign your team.</p><button className="secondary" onClick={() => auth.signoutRedirect()}>Sign out</button></section></main>;

  const refresh = () => { setNotice(''); setRefreshToken(value => value + 1); };
  const runEstimate = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setNotice(''); setEstimate(null);
    try { setEstimate(await api.estimate({ team, instance_type: instanceType, region: 'ap-south-1', ebs_gb: ebsGb, duration_hours: runtime, purpose })); }
    catch (e) { setNotice(errorText(e)); }
    finally { setBusy(false); }
  };
  const submitRequest = async () => {
    if (!estimate || !window.confirm(`Reserve ${formatUsd(estimate.total_micro)} estimate using the 1.10 margin and start the managed request?`)) return;
    setBusy(true); setNotice('');
    const payload = { team, instance_type: instanceType, region: 'ap-south-1', ebs_gb: ebsGb, duration_hours: runtime, purpose };
    const storageKey = 'spendctl_pending_submission';
    let pending: { key: string; fingerprint: string };
    const fingerprint = fingerprintPayload(payload);
    try {
      const saved = sessionStorage.getItem(storageKey);
      pending = saved ? JSON.parse(saved) : { key: crypto.randomUUID(), fingerprint };
      if (pending.fingerprint !== fingerprint) pending = { key: crypto.randomUUID(), fingerprint };
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
      const result = await api.submit(payload, pending.key);
      sessionStorage.removeItem(storageKey);
      setNotice(`Request ${result.reservation_id} · ${result.reservation.status} · ${result.reservation.workflow_start_state ?? 'workflow pending'}`);
      setPage('requests');
    } catch (e) {
      const message = errorText(e);
      const unknown = Boolean((e as { pendingUnknown?: boolean }).pendingUnknown);
      if (!unknown) sessionStorage.removeItem(storageKey);
      setNotice(unknown ? `${message} The same idempotency key is retained for a safe retry.` : message);
    } finally { setBusy(false); }
  };
  const cancelRequest = async (row: Reservation) => {
    if (!window.confirm(`Request cancellation for ${row.reservation_id}? Cleanup runs asynchronously; funds remain reserved until verified.`)) return;
    setBusy(true); setNotice('');
    try { const result = await api.cancel(row.reservation_id); setNotice(`Cancellation accepted. Cleanup: ${result.cleanup_state}.`); setPage('dashboard'); }
    catch (e) { setNotice(errorText(e)); }
    finally { setBusy(false); }
  };
  const saveBudget = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setNotice('');
    try { await api.updateBudget(team, budgetUsd); setNotice('Budget created for this team. Existing budgets are never reset.'); setPage('budgets'); }
    catch (e) { setNotice(errorText(e)); }
    finally { setBusy(false); }
  };

  return <div className="shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">S</span><span>spendctl<small>FINANCIAL OPERATIONS</small></span></div><div className="team-chip"><span className="status-dot" />{team}</div><nav aria-label="Main navigation">{(Object.keys(pageLabels) as Page[]).filter(p => (p !== 'alerts' && p !== 'research') || isAdmin).map(p => <button key={p} className={page === p ? 'nav-item active' : 'nav-item'} onClick={() => setPage(p)}><NavIcon name={p} />{pageLabels[p]}</button>)}</nav><div className="sidebar-foot"><div className="avatar">{String(claims.email ?? 'U').slice(0, 1).toUpperCase()}</div><div className="user-label">{String(claims.email ?? 'Signed in')}<small>{isAdmin ? 'Administrator' : 'Developer'}</small></div><button className="icon-button" aria-label="Sign out" onClick={() => auth.signoutRedirect()}>↗</button></div></aside>
    <main className="main"><header className="topbar"><div><span className="eyebrow">SPENDCTL / {pageLabels[page].toUpperCase()}</span><h1>{page === 'dashboard' ? 'Overview' : pageLabels[page]}</h1></div><div className="top-actions"><span className="environment-pill"><span className="status-dot" />LIVE API</span><button className="secondary refresh" onClick={refresh} disabled={busy}>↻ <span>Refresh</span></button></div></header>
      {notice && <div className="notice" role="status">{notice}</div>}
      {busy && <div className="loading-bar" role="status">Syncing secure account data…</div>}
      {!team && <div className="notice warning">Your session has no team claim.</div>}
      {page === 'dashboard' && <Dashboard budget={budget} reservations={reservations} onPage={setPage} />}
      {page === 'estimate' && <section className="panel estimator"><div className="panel-heading"><div><span className="eyebrow">PLAN A MANAGED RESOURCE</span><h2>Estimate your spend</h2><p>Region and supported machine sizes are fixed by policy.</p></div><span className="tag">Mumbai · ap-south-1</span></div><div className="estimate-layout"><form onSubmit={runEstimate} className="form-grid"><label>Instance type<select value={instanceType} onChange={e => setInstanceType(e.target.value)}><option>t3.micro</option><option>t3.small</option></select></label><label>gp3 storage (GB)<input type="number" min={1} max={30} value={ebsGb} onChange={e => setEbsGb(Number(e.target.value))} required /></label><label>Runtime (hours)<input type="number" min={1} max={24} value={runtime} onChange={e => setRuntime(Number(e.target.value))} required /></label><label className="wide">Purpose<input value={purpose} onChange={e => setPurpose(e.target.value)} minLength={3} maxLength={256} placeholder="e.g. temporary QA environment" required /></label><button disabled={busy} type="submit">{busy ? 'Calculating…' : 'Calculate estimate'} <span>→</span></button></form><div className="estimate-card">{estimate ? <><span className="eyebrow">ESTIMATE BREAKDOWN</span><h3>{formatUsd(estimate.total_micro)}</h3><Breakdown label="Compute" value={estimate.compute_micro} /><Breakdown label="gp3 storage" value={estimate.storage_micro} /><Breakdown label="Reserve at 1.10×" value={estimate.reserve_micro ?? Math.ceil(estimate.total_micro * 1.1)} strong /><p className="provenance">Pricing: {estimate.price_source ?? estimate.source ?? 'provider unreported'}{estimate.price_snapshot_id ? ` · ${estimate.price_snapshot_id}` : ''}</p>{(estimate.price_source ?? estimate.source)?.includes('fallback') && <p className="warning-text">Fallback price is unverified. Treat this as an estimate, not a live quote.</p>}<button className="submit-request" disabled={busy} onClick={submitRequest}>Review and submit request <span>→</span></button></> : <div className="estimate-empty"><div className="empty-icon">◈</div><h3>Your estimate will appear here</h3><p>Enter a supported configuration to see compute and storage separately.</p></div>}</div></div></section>}
      {page === 'requests' && <section className="panel"><PanelTitle eyebrow="TEAM ACTIVITY" title="Requests & reservations" count={reservations.length} /><ReservationTable rows={reservations} onCancel={cancelRequest} onDetails={async row => { setBusy(true); try { setSelectedRequest((await api.requestById(row.reservation_id)).request); } catch (e) { setNotice(errorText(e)); } finally { setBusy(false); } }} />{!reservations.length && !busy && <Empty title="No requests yet" body="Create a cost estimate to start a managed request." action={() => setPage('estimate')} />}</section>}
      {page === 'budgets' && <><section className="panel"><PanelTitle eyebrow="MONTHLY CONTROL" title="Team budget" /><BudgetView budget={budget} /></section>{isAdmin && <section className="panel admin-form"><span className="eyebrow">ADMINISTRATOR ACTION</span><h2>Create a monthly budget</h2><p>Creates a budget only if one does not already exist for the month.</p><form onSubmit={saveBudget}><label>Budget in USD<input value={budgetUsd} onChange={e => setBudgetUsd(e.target.value)} inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,6})?" placeholder="250.00" required /></label><button disabled={busy}>Create budget</button></form></section>}</>}
      {page === 'alerts' && <section className="panel"><PanelTitle eyebrow="ADMINISTRATOR VIEW" title="Active alerts" count={alerts.length} />{alerts.length ? <div className="alert-list">{alerts.map(a => <article className="alert-row" key={a.alert_id}><span className="alert-icon">!</span><div><strong>{a.alert_type}</strong><p>{JSON.stringify(a.details ?? {})}</p></div><time>{a.created_at ? new Date(a.created_at).toLocaleString() : 'Time unavailable'}</time></article>)}</div> : <Empty title="No alerts returned" body="The API returned no alert records for this account." />}</section>}
      {page === 'research' && <section className="panel"><PanelTitle eyebrow="READ-ONLY CALIBRATION" title="Reserve policy evidence" /><div className="research-banner"><strong>Recommendations only</strong><span>This view cannot change production reserve policy. API-reported CostSamples require live AWS verification.</span></div>{summary ? <><div className="metric-grid"><Metric label="Final attributed samples" value={String(summary.sample_count)} /><Metric label="Current production reserve" value="1.10×" /><Metric label="Data source" value="CostSamples" /></div><div className="policy-grid">{summary.policies.map((p, i) => <article className="policy-card" key={i}><pre>{JSON.stringify(p, null, 2)}</pre></article>)}</div>{summary.sample_count === 0 && <div className="notice warning">No final attributed calibration samples are available. No values are fabricated.</div>}</> : !busy && <Empty title="Calibration unavailable" body="Only administrators can load the secured calibration summary." />}</section>}
      <footer className="footer">Financial decisions use integer micro-USD on the server. Prices and account activity depend on configured AWS services.</footer>
      {selectedRequest && <div className="modal-backdrop" role="presentation" onClick={() => setSelectedRequest(null)}><section className="request-modal" role="dialog" aria-modal="true" aria-labelledby="request-modal-title" onClick={e => e.stopPropagation()}><button className="modal-close" aria-label="Close request details" onClick={() => setSelectedRequest(null)}>×</button><span className="eyebrow">RESERVATION DETAILS</span><h2 id="request-modal-title">{selectedRequest.reservation_id}</h2><p>Status: <b>{selectedRequest.status}</b> · Workflow: <b>{selectedRequest.workflow_start_state ?? selectedRequest.workflow_execution_status ?? 'unknown'}</b></p><pre>{JSON.stringify(selectedRequest, null, 2)}</pre>{selectedRequest.status === 'ACTIVE' && <button className="danger-fill" onClick={() => { setSelectedRequest(null); void cancelRequest(selectedRequest); }}>Request cancellation</button>}</section></div>}
    </main>
  </div>;
}

function Dashboard({ budget, reservations, onPage }: { budget: Budget | null; reservations: Reservation[]; onPage: (page: Page) => void }) {
  const total = budget?.budget_micro ?? 0;
  const used = (budget?.reserved_micro ?? 0) + (budget?.spent_micro ?? 0);
  const utilization = total ? Math.min(100, used / total * 100) : 0;
  return <>{budget && utilization >= 50 && <div className={`notice ${utilization >= 80 ? 'warning' : ''}`}>{utilization >= 100 ? 'Budget is fully utilized.' : utilization >= 80 ? 'Budget utilization has reached the 80% threshold.' : 'Budget utilization has reached the 50% threshold.'} These indicators use the latest API budget snapshot.</div>}<section className="balance-hero"><div><span className="eyebrow">AVAILABLE TO RESERVE · {budget?.period ?? 'CURRENT PERIOD'}</span><strong>{formatUsd(budget?.available_micro)}</strong><p>Remaining budget for {budget?.period ?? 'this period'}</p></div><div className="utilization"><div className="ring" style={{ '--progress': `${utilization}%` } as React.CSSProperties}><span>{Math.round(utilization)}<small>%</small></span></div><span>Budget utilized</span></div></section>{!budget && <div className="notice warning">No monthly budget is available for this team. Ask an administrator to create one before submitting requests.</div>}<div className="metric-grid"><Metric label="Total budget" value={formatUsd(budget?.budget_micro)} /><Metric label="Reserved" value={formatUsd(budget?.reserved_micro)} /><Metric label="Spent" value={formatUsd(budget?.spent_micro)} /><Metric label="Requests" value={String(reservations.length)} /></div><section className="panel recent"><PanelTitle eyebrow="LATEST ACTIVITY" title="Recent requests" action={<button className="text-button" onClick={() => onPage('requests')}>View all →</button>} /><ReservationTable rows={reservations.slice(0, 5)} onCancel={() => undefined} compact /><div className="panel-bottom"><button className="secondary" onClick={() => onPage('requests')}>Open requests</button><button onClick={() => onPage('estimate')}>New estimate <span>→</span></button></div></section></>;
}

function BudgetView({ budget }: { budget: Budget | null }) { if (!budget) return <Empty title="No budget found" body="Ask an administrator to create this team's monthly budget." />; return <><div className="balance-hero budget-hero"><div><span className="eyebrow">TOTAL MONTHLY BUDGET · {budget.period}</span><strong>{formatUsd(budget.budget_micro)}</strong><p>Available + reserved + spent</p></div><span className="tag good">Ledger-reported</span></div><div className="metric-grid"><Metric label="Available" value={formatUsd(budget.available_micro)} /><Metric label="Reserved" value={formatUsd(budget.reserved_micro)} /><Metric label="Spent" value={formatUsd(budget.spent_micro)} /></div></>; }
function ReservationTable({ rows, onCancel, onDetails, compact = false }: { rows: Reservation[]; onCancel: (row: Reservation) => void; onDetails?: (row: Reservation) => void; compact?: boolean }) { if (!rows.length) return null; return <div className="table-wrap"><table><thead><tr><th>Request</th><th>Configuration</th><th>Reserve</th><th>Status</th><th>Workflow</th>{!compact && <th>Action</th>}</tr></thead><tbody>{rows.map(row => <tr key={row.reservation_id}><td><strong>{row.reservation_id.slice(0, 16)}…</strong><small>{row.created_at ? new Date(row.created_at).toLocaleDateString() : 'Recent'}</small></td><td>{row.resource?.instance_type ?? 'EC2'}<small>{row.resource?.ebs_gb ?? '—'} GB gp3</small></td><td>{formatUsd(row.reserved_micro)}</td><td><span className={`status ${row.status.toLowerCase()}`}>{row.status}</span></td><td>{row.workflow_start_state ?? row.workflow_execution_status ?? '—'}</td>{!compact && <td><button className="text-button" onClick={() => onDetails?.(row)}>Details</button>{row.status === 'ACTIVE' && <button className="text-button danger" onClick={() => onCancel(row)}>Cancel</button>}</td>}</tr>)}</tbody></table></div>; }
function PanelTitle({ eyebrow, title, count, action }: { eyebrow: string; title: string; count?: number; action?: React.ReactNode }) { return <div className="panel-title"><div><span className="eyebrow">{eyebrow}</span><h2>{title} {count !== undefined && <span className="count">{count}</span>}</h2></div>{action}</div>; }
function Metric({ label, value }: { label: string; value: string }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function Breakdown({ label, value, strong = false }: { label: string; value: number; strong?: boolean }) { return <div className={strong ? 'breakdown strong' : 'breakdown'}><span>{label}</span><b>{formatUsd(value)}</b></div>; }
function Empty({ title, body, action }: { title: string; body: string; action?: () => void }) { return <div className="empty-state"><div className="empty-icon">⌁</div><h3>{title}</h3><p>{body}</p>{action && <button className="secondary" onClick={action}>Create estimate</button>}</div>; }
function NavIcon({ name }: { name: string }) { const icons: Record<Page, string> = { dashboard: '▦', estimate: '◈', requests: '≡', budgets: '◷', alerts: '△', research: '⌁' }; return <span className="nav-icon" aria-hidden="true">{icons[name]}</span>; }
function errorText(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please retry.'; }
function fingerprintPayload(payload: object) { let hash = 2166136261; for (const character of JSON.stringify(payload)) { hash ^= character.charCodeAt(0); hash = Math.imul(hash, 16777619); } return (hash >>> 0).toString(16); }
