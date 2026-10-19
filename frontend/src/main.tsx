import React from 'react';
import ReactDOM from 'react-dom/client';
import { AuthProvider } from 'react-oidc-context';
import { App } from './App';
import './style.css';

const env = import.meta.env;
const redirectUri = env.VITE_COGNITO_REDIRECT_URI || `${window.location.origin}/callback`;
const authConfig = {
  authority: env.VITE_COGNITO_ISSUER,
  client_id: env.VITE_COGNITO_CLIENT_ID,
  redirect_uri: redirectUri,
  response_type: 'code',
  scope: 'openid email profile',
  post_logout_redirect_uri: env.VITE_COGNITO_LOGOUT_URI || window.location.origin,
  automaticSilentRenew: true,
  onSigninCallback: () => { window.history.replaceState({}, document.title, window.location.pathname); },
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><AuthProvider {...authConfig}><App /></AuthProvider></React.StrictMode>,
);
