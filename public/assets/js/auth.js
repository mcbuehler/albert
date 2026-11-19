// Login (Supabase Auth magic link). The session (access token, refreshed automatically)
// lives in supabase-js; api() adds it to every request.
/* global supabase */  // UMD build of supabase-js, loaded before this module

import { setStatus } from "./ui.js";

const loginSection = document.getElementById("login");
const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const appSection = document.getElementById("app");
const userBox = document.getElementById("user-box");
let supabaseClient = null;
let signedInUserId = null;
let handlers = { onSignedIn() {}, onSignedOut() {} };
// Public page settings from /api/page-config (Supabase login, calling hours), loaded by initAuth().
export let pageConfig = {};

export function isSignedIn() {
  return signedInUserId !== null;
}

export async function api(path, options = {}) {
  const { data } = await supabaseClient.auth.getSession();
  const headers = new Headers(options.headers || {});
  if (data.session) headers.set("Authorization", "Bearer " + data.session.access_token);
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    // The server rejected the login: drop it locally too, so a reload doesn't reuse it.
    await supabaseClient.auth.signOut({ scope: "local" });
    showLogin("Your session has ended. Please sign in again.");
  }
  return res;
}

function showLogin(message) {
  signedInUserId = null;
  appSection.hidden = true;
  userBox.hidden = true;
  loginSection.hidden = false;
  if (message) setStatus(loginStatus, "err", message);
  handlers.onSignedOut();  // e.g. don't leave customer data in the page
}

function showApp(session) {
  document.getElementById("user-email").textContent = session.user.email || "";
  loginSection.hidden = true;
  userBox.hidden = false;
  appSection.hidden = false;
  if (signedInUserId !== session.user.id) {  // first sign-in, not a token refresh
    signedInUserId = session.user.id;
    handlers.onSignedIn();
  }
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = loginForm.querySelector("button");
  btn.disabled = true;
  setStatus(loginStatus, "", "Sending…");
  const { error } = await supabaseClient.auth.signInWithOtp({
    email: loginForm.elements.email.value.trim(),
    // No sign-ups: only invited users get a link.
    options: { emailRedirectTo: location.origin + location.pathname, shouldCreateUser: false },
  });
  btn.disabled = false;
  if (error) {
    setStatus(loginStatus, "err", "Could not send the link: " + error.message);
  } else {
    setStatus(loginStatus, "ok", "Check your email and open the link in this browser.");
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await supabaseClient.auth.signOut();
  setStatus(loginStatus, "", "Signed out.");
});

// Create the Supabase client and follow the session. handlers: { onSignedIn(), onSignedOut() }.
export async function initAuth(callbacks) {
  handlers = { ...handlers, ...callbacks };
  // A magic link that failed (expired, already used) comes back with an error in the URL.
  const params = new URLSearchParams(location.search + "&" + location.hash.slice(1));
  const linkError = params.get("error_description");
  try {
    const res = await fetch("/api/page-config");
    const cfg = await res.json();
    if (!res.ok) throw new Error(cfg.error || res.status);
    pageConfig = cfg;
    supabaseClient = supabase.createClient(cfg.supabase_url, cfg.supabase_publishable_key, {
      auth: { flowType: "pkce", detectSessionInUrl: true, persistSession: true, autoRefreshToken: true },
    });
  } catch (err) {
    loginSection.hidden = false;
    setStatus(loginStatus, "err", "Login is unavailable: " + err.message);
    return;
  }
  supabaseClient.auth.onAuthStateChange((event, session) => {
    // Defer: supabase-js must not be called from inside this callback.
    setTimeout(() => {
      if (session) {
        if (location.search || location.hash) history.replaceState(null, "", location.pathname);  // drop ?code=
        showApp(session);
      } else {
        showLogin(linkError ? "Sign-in link failed: " + linkError : "");
      }
    }, 0);
  });
}
