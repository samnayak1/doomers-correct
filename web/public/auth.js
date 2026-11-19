/* Accounts: who is signed in, the "sign up or log in" modal, and a fetch
 * wrapper that keeps a session alive.
 *
 * Tokens live in HttpOnly cookies, so this file never sees one. It only asks
 * /api/me who the cookies belong to. The ID token lasts an hour; when the API
 * answers {code: "expired"}, apiFetch refreshes it once and retries.
 *
 * Signed-in features stay visible to everyone. Using one while signed out
 * opens the modal instead (gate / requireLogin), so a visitor sees what an
 * account gets them before being asked for one.
 */

const $ = (id) => document.getElementById(id);

let me = null;          // last /api/me answer
let loading = null;
const listeners = new Set();

/** Calls fn(me) now if known, and again whenever sign-in state changes. */
export function onSession(fn) {
  listeners.add(fn);
  if (me) fn(me);
}

function setMe(d) {
  me = d;
  renderAccount();
  listeners.forEach((fn) => { try { fn(me); } catch { /* one listener never breaks another */ } });
}

async function refresh() {
  try {
    const r = await fetch('/api/auth/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    return r.ok;
  } catch { return false; }
}

export function session({ reload = false } = {}) {
  if (reload) loading = null;
  loading ||= (async () => {
    let d;
    try {
      d = await (await fetch('/api/me')).json();
      if (d.expired && await refresh()) d = await (await fetch('/api/me')).json();
    } catch {
      d = { enabled: false, logged_in: false };
    }
    setMe(d);
    return d;
  })();
  return loading;
}

export const current = () => me;
export const isLoggedIn = () => !!me?.logged_in;

/** An Error carrying the API's message, status and code. */
function apiError(r, d) {
  const e = new Error(typeof d.detail === 'string' ? d.detail : `Something went wrong (${r.status}).`);
  e.status = r.status;
  e.code = d.code;
  return e;
}

/** JSON in, JSON out. Refreshes an expired session once; on login_required
 * it marks the page signed out and opens the modal, then throws. */
export async function apiFetch(url, { method = 'GET', body, loginReason } = {}) {
  const go = () => fetch(url, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let r = await go();
  let d = await r.json().catch(() => ({}));
  if (r.status === 401 && d.code === 'expired' && await refresh()) {
    r = await go();
    d = await r.json().catch(() => ({}));
  }
  if (!r.ok) {
    if (r.status === 401 && d.code === 'login_required') {
      if (me?.logged_in) setMe({ ...me, logged_in: false });
      if (loginReason) requireLogin(loginReason);
    }
    throw apiError(r, d);
  }
  return d;
}

/* ── the modal ─────────────────────────────────────────────────────────── */
function loginHref(signup) {
  const back = location.pathname + location.search + location.hash;
  return `/api/auth/login?return_to=${encodeURIComponent(back)}${signup ? '&signup=1' : ''}`;
}

/** Open the "sign up or log in" dialog. `reason` finishes "...to <reason>." */
export function requireLogin(reason, { note = '' } = {}) {
  const dlg = $('login-modal');
  if (!dlg) return;
  $('login-reason').textContent = reason ? `to ${reason}` : 'to continue';
  $('login-note').textContent = note;
  $('login-note').hidden = !note;
  $('login-signup').href = loginHref(true);
  $('login-login').href = loginHref(false);
  if (!dlg.open) dlg.showModal();
}

/** True if signed in. Otherwise opens the modal and returns false. While the
 * session is still loading, or accounts are switched off, it lets the action
 * through - the server still refuses anything that really needs a login. */
export function gate(reason) {
  if (!me || !me.enabled || me.logged_in) return true;
  requireLogin(reason);
  return false;
}

/* ── the account control in the masthead ──────────────────────────────── */
function renderAccount() {
  const box = $('account');
  if (!box) return;
  box.hidden = !me?.enabled;
  if (!me?.enabled) return;
  if (me.logged_in) {
    const name = document.createElement('span');
    name.className = 'acct-name';
    name.textContent = me.handle;
    name.title = 'Your public name on predictions';
    const out = document.createElement('button');
    out.type = 'button';
    out.className = 'linkish';
    out.textContent = 'Log out';
    out.addEventListener('click', logout);
    box.replaceChildren(name, out);
  } else {
    const a = document.createElement('a');
    a.className = 'acct-login';
    a.href = loginHref(false);
    a.textContent = 'Log in';
    // Recomputed on click: the hash (which tab is open) may have changed.
    a.addEventListener('click', () => { a.href = loginHref(false); });
    const up = document.createElement('a');
    up.className = 'acct-signup';
    up.href = loginHref(true);
    up.textContent = 'Sign up';
    up.addEventListener('click', () => { up.href = loginHref(true); });
    box.replaceChildren(a, up);
  }
}

async function logout() {
  try {
    const d = await apiFetch('/api/auth/logout', { method: 'POST', body: {} });
    location.href = d.logout_url || '/';
  } catch {
    location.reload();
  }
}

export function initAuth() {
  const dlg = $('login-modal');
  dlg?.addEventListener('click', (e) => {
    // A click on the backdrop (the dialog element itself) closes it.
    if (e.target === dlg || e.target.closest('[data-close]')) dlg.close();
  });
  // Back from a login that did not complete (cancelled, expired link).
  const u = new URL(location);
  if (u.searchParams.get('login') === 'failed') {
    u.searchParams.delete('login');
    history.replaceState(null, '', u);
    session().then(() => requireLogin('', { note: 'That login did not complete. Please try again.' }));
  }
  return session();
}
