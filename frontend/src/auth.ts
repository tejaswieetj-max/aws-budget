import { useEffect, useState } from 'react';

type Session = { id_token: string; access_token: string; profile: Record<string, unknown>; expires_at: number };
const SESSION_KEY = 'spendctl_cognito_session';
const STATE_KEY = 'spendctl_oidc_state';
const VERIFIER_KEY = 'spendctl_pkce_verifier';
const NONCE_KEY = 'spendctl_oidc_nonce';

export function useSpendctlAuth() {
  const [user, setUser] = useState<Session | null>(() => readSession());
  const [isLoading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let mounted = true;
    const url = new URL(window.location.href);
    const code = url.searchParams.get('code');
    const returnedState = url.searchParams.get('state');
    if (code) {
      void redeemCode(code, returnedState).then(session => {
        if (!mounted) return;
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
        setUser(session);
        window.history.replaceState({}, document.title, url.pathname);
      }).catch(cause => mounted && setError(cause instanceof Error ? cause : new Error('Cognito sign-in could not be completed')))
        .finally(() => mounted && setLoading(false));
    } else {
      const session = readSession();
      if (session && session.expires_at * 1000 <= Date.now()) {
        sessionStorage.removeItem(SESSION_KEY);
        setUser(null);
      } else setUser(session);
      setLoading(false);
    }
    return () => { mounted = false; };
  }, []);

  const removeUser = async () => { sessionStorage.removeItem(SESSION_KEY); setUser(null); };
  const signinRedirect = async () => {
    setError(null);
    const { domain, clientId, redirectUri } = config();
    const verifier = randomText(48);
    const state = randomText(24);
    const nonce = randomText(24);
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    sessionStorage.setItem(STATE_KEY, state);
    sessionStorage.setItem(NONCE_KEY, nonce);
    const challenge = base64Url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
    const authorize = new URL('/oauth2/authorize', domain);
    Object.entries({ client_id: clientId, response_type: 'code', scope: 'openid email profile',
      redirect_uri: redirectUri, code_challenge_method: 'S256', code_challenge: challenge, state, nonce })
      .forEach(([key, value]) => authorize.searchParams.set(key, value));
    window.location.assign(authorize.toString());
  };
  const signoutRedirect = async () => {
    const { domain, clientId } = config();
    sessionStorage.removeItem(SESSION_KEY);
    setUser(null);
    const logout = new URL('/logout', domain);
    logout.searchParams.set('client_id', clientId);
    logout.searchParams.set('logout_uri', import.meta.env.VITE_COGNITO_LOGOUT_URI || window.location.origin);
    window.location.assign(logout.toString());
  };

  return { user, isLoading, isAuthenticated: Boolean(user), activeNavigator: false,
    error, signinRedirect, signoutRedirect, removeUser };
}

async function redeemCode(code: string, returnedState: string | null): Promise<Session> {
  const expectedState = sessionStorage.getItem(STATE_KEY);
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  const expectedNonce = sessionStorage.getItem(NONCE_KEY);
  if (!returnedState || returnedState !== expectedState || !verifier || !expectedNonce)
    throw new Error('Cognito callback state validation failed. Sign in again.');
  const { domain, clientId, redirectUri } = config();
  const body = new URLSearchParams({ grant_type: 'authorization_code', client_id: clientId,
    code, redirect_uri: redirectUri, code_verifier: verifier });
  const response = await fetch(new URL('/oauth2/token', domain), {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body,
  });
  if (!response.ok) throw new Error(`Cognito token exchange failed (${response.status}).`);
  const token = await response.json();
  const payload = decodeJwt(token.id_token);
  if (payload.nonce !== expectedNonce) throw new Error('Cognito ID token nonce validation failed.');
  sessionStorage.removeItem(STATE_KEY); sessionStorage.removeItem(VERIFIER_KEY); sessionStorage.removeItem(NONCE_KEY);
  return { id_token: token.id_token, access_token: token.access_token, profile: payload,
    expires_at: Number(payload.exp ?? Math.floor(Date.now() / 1000) + Number(token.expires_in ?? 3600)) };
}

function config() {
  const domain = import.meta.env.VITE_COGNITO_HOSTED_UI_DOMAIN;
  const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
  const redirectUri = import.meta.env.VITE_COGNITO_REDIRECT_URI || `${window.location.origin}/callback`;
  if (!domain || !clientId) throw new Error('Set VITE_COGNITO_HOSTED_UI_DOMAIN and VITE_COGNITO_CLIENT_ID in frontend/.env.local.');
  return { domain: domain.replace(/\/$/, ''), clientId, redirectUri };
}
function readSession(): Session | null { try { const raw = sessionStorage.getItem(SESSION_KEY); return raw ? JSON.parse(raw) as Session : null; } catch { return null; } }
function randomText(length: number) { const bytes = crypto.getRandomValues(new Uint8Array(length)); return base64Url(bytes); }
function base64Url(bytes: Uint8Array) { return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function decodeJwt(token: string): Record<string, unknown> { const part = token.split('.')[1]; if (!part) throw new Error('Cognito returned an invalid ID token.'); return JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/'))); }
