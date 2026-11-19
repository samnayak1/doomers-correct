/* Predictions tab: post a dated prediction, browse everyone's, vote, report.
 *
 * Posting needs an account. Voting does not: an anonymous visitor gets a
 * voter cookie for solving one invisible Cap proof-of-work, the first time
 * they vote, and keeps it. Signed-in visitors vote as their account.
 *
 * Every piece of user-written text goes in through textContent - never
 * innerHTML - because this is the one place the site shows text strangers
 * typed.
 */
import { apiFetch, current, gate, onSession, requireLogin } from '/auth.js';

const $ = (id) => document.getElementById(id);
let onEvent = () => {};

const state = { sort: 'upcoming', next: null, loaded: false, busy: false, rules: null, pending: null };
const SORTS = [['upcoming', 'Upcoming'], ['due', 'Due'], ['newest', 'Newest'], ['top', 'Top']];

const fmtDate = (iso) => new Date(`${iso.slice(0, 10)}T00:00:00Z`)
  .toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('data-') || k.startsWith('aria-')) n.setAttribute(k, v);
    else n[k] = v;
  }
  n.append(...kids.filter(Boolean));
  return n;
}

/* ── list ──────────────────────────────────────────────────────────────── */
function card(p, { linked = false } = {}) {
  const head = el('div', { class: 'pred-meta' },
    el('span', { class: 'pred-handle', text: p.handle }),
    el('span', { class: 'sep', text: '·' }),
    el('span', { text: `for ${fmtDate(p.target_date)}` }),
    p.due ? el('span', { class: 'pill due', text: 'Due' }) : null,
    p.hidden ? el('span', { class: 'pill bad', text: 'Hidden by moderators' }) : null,
  );
  const text = el('p', { class: 'pred-text', text: p.text });

  const thumb = (value, label, count) => el('button', {
    type: 'button', class: 'vote', 'data-vote': String(value),
    'aria-pressed': String(p.my_vote === value),
    'aria-label': `${label} (${count})`,
    disabled: p.mine || p.hidden,
    title: p.mine ? 'This is your prediction' : label,
  }, el('span', { 'aria-hidden': 'true', text: value === 1 ? '👍' : '👎' }), el('span', { class: 'n', text: String(count) }));

  const foot = el('div', { class: 'pred-foot' },
    thumb(1, 'Agree', p.up), thumb(-1, 'Disagree', p.down),
    el('span', { class: 'pred-made', text: `made ${fmtDate(p.created)}` }),
    p.mine || p.hidden ? null : el('button', { type: 'button', class: 'linkish report', 'data-report': '1', text: 'Report' }),
  );
  const li = el('li', { class: `pred${linked ? ' linked' : ''}${p.mine ? ' mine' : ''}`, id: linked ? '' : `prediction-${p.id}`, 'data-id': String(p.id) },
    head, text, foot);
  li._p = p;
  return li;
}

function renderSorts() {
  $('pred-sort').replaceChildren(...SORTS.map(([k, label]) => el('button', {
    type: 'button', class: 'chip', 'data-sort': k, 'aria-pressed': String(state.sort === k), text: label,
  })));
}

async function load({ more = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  const list = $('pred-list');
  const status = $('pred-list-status');
  if (!more) { list.replaceChildren(); status.textContent = 'Loading…'; }
  try {
    const q = new URLSearchParams({ sort: state.sort });
    if (more && state.next) q.set('cursor', state.next);
    const d = await apiFetch(`/api/predictions?${q}`);
    applyRules(d.rules);
    state.next = d.next;
    list.append(...d.items.map((p) => card(p)));
    status.textContent = list.children.length ? '' : {
      upcoming: 'No upcoming predictions yet. Be the first.',
      due: 'Nothing has come due yet.',
      newest: 'No predictions yet. Be the first.',
      top: 'No predictions yet. Be the first.',
    }[state.sort];
    $('pred-more').hidden = !state.next;
    state.loaded = true;
  } catch (err) {
    status.textContent = `Could not load predictions — ${err.message}`;
  } finally {
    state.busy = false;
  }
}

/* ── one prediction, from the link in a reminder email ─────────────────── */
async function showLinked(id) {
  const box = $('pred-linked');
  try {
    const p = await apiFetch(`/api/predictions/${id}`);
    box.replaceChildren(el('p', { class: 'pred-linked-title', text: 'The prediction you followed a link to' }),
      el('ul', { class: 'pred-list' }, card(p, { linked: true })));
    box.hidden = false;
  } catch {
    box.hidden = true;
  }
}

/* ── voting ────────────────────────────────────────────────────────────── */
let capScript = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error('could not load the verification script'));
    document.head.appendChild(s);
  });
}

/** Solve one invisible Cap challenge and trade the token for a voter cookie. */
async function becomeVoter() {
  const endpoint = state.rules?.captcha;
  if (!endpoint) throw new Error('Voting is not available right now.');
  capScript ||= loadScript('/vendor/cap.min.js');
  await capScript;
  const cap = new window.Cap({ apiEndpoint: endpoint });
  let token = '';
  cap.addEventListener('solve', (e) => { token = e.detail.token; });
  try { await cap.solve(); } catch { /* checked below */ }
  token ||= cap.token || '';
  cap.widget?.remove();
  if (!token) throw new Error('Verification did not complete. Please try again.');
  await apiFetch('/api/voter', { method: 'POST', body: { token } });
}

function paintVotes(li, d) {
  li._p = { ...li._p, up: d.up, down: d.down, my_vote: d.my_vote };
  li.querySelectorAll('button.vote').forEach((b) => {
    const v = Number(b.dataset.vote);
    const n = v === 1 ? d.up : d.down;
    b.setAttribute('aria-pressed', String(d.my_vote === v));
    b.querySelector('.n').textContent = String(n);
    b.setAttribute('aria-label', `${v === 1 ? 'Agree' : 'Disagree'} (${n})`);
  });
}

async function vote(li, value) {
  const p = li._p;
  const next = p.my_vote === value ? 0 : value;   // clicking your vote again removes it
  const before = { up: p.up, down: p.down, my_vote: p.my_vote };
  // Optimistic: show the change now, put it back if the server says no.
  paintVotes(li, {
    up: p.up + (next === 1) - (p.my_vote === 1),
    down: p.down + (next === -1) - (p.my_vote === -1),
    my_vote: next,
  });
  li.classList.add('voting');
  const send = () => apiFetch(`/api/predictions/${p.id}/vote`, { method: 'PUT', body: { value: next } });
  try {
    let d;
    try {
      d = await send();
    } catch (err) {
      if (err.code !== 'voter_required') throw err;
      flash(li, 'Checking you are human (one time only)…');
      await becomeVoter();
      d = await send();
      flash(li, '');
    }
    paintVotes(li, d);
    onEvent(`prediction-vote-${next === 1 ? 'up' : next === -1 ? 'down' : 'clear'}`, 'Prediction vote');
  } catch (err) {
    paintVotes(li, before);
    flash(li, err.message);
  } finally {
    li.classList.remove('voting');
  }
}

function flash(li, msg) {
  let s = li.querySelector('.pred-flash');
  if (!msg) { s?.remove(); return; }
  if (!s) { s = el('p', { class: 'pred-flash', role: 'status' }); li.append(s); }
  s.textContent = msg;
}

/* ── reporting ─────────────────────────────────────────────────────────── */
const REASONS = ['Spam or advertising', 'Abusive or hateful', 'Personal information', 'Something else'];

function openReport(li) {
  if (li.querySelector('.pred-report')) return;
  const sel = el('select', { 'aria-label': 'Why are you reporting this?' },
    ...REASONS.map((r) => el('option', { value: r, text: r })));
  const sendBtn = el('button', { type: 'submit', class: 'tool', text: 'Send report' });
  const cancel = el('button', { type: 'button', class: 'linkish', text: 'Cancel' });
  const f = el('form', { class: 'pred-report' }, sel, sendBtn, cancel);
  cancel.addEventListener('click', () => f.remove());
  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    sendBtn.disabled = true;
    const send = () => apiFetch(`/api/predictions/${li._p.id}/report`, { method: 'POST', body: { reason: sel.value } });
    try {
      try { await send(); } catch (err) {
        if (err.code !== 'voter_required') throw err;
        await becomeVoter();
        await send();
      }
      f.replaceWith(el('p', { class: 'pred-flash', role: 'status', text: 'Thanks — a moderator will take a look.' }));
      li.querySelector('[data-report]')?.remove();
      onEvent('prediction-report', 'Prediction reported');
    } catch (err) {
      sendBtn.disabled = false;
      flash(li, err.message);
    }
  });
  li.append(f);
  sel.focus();
}

/* ── the form ──────────────────────────────────────────────────────────── */
function applyRules(rules) {
  if (!rules) return;
  state.rules = rules;
  const date = $('pred-date');
  date.min = rules.min_date;
  date.max = rules.max_date;
  $('pred-text').maxLength = rules.max_chars;
  $('pred-remind-time').textContent = `${rules.reminder_time} IST`;
  counter();
}

function counter() {
  const max = state.rules?.max_chars || 280;
  const n = $('pred-text').value.trim().length;
  $('pred-count').textContent = `${n}/${max}`;
  $('pred-count').classList.toggle('over', n > max);
}

function resetConfirm() {
  state.pending = null;
  $('pred-confirm').hidden = true;
  $('pred-post').hidden = false;
}

function validate() {
  const text = $('pred-text').value.trim().replace(/\s+/g, ' ');
  const date = $('pred-date').value;
  const r = state.rules;
  if (text.length < 10) return 'Write a little more - at least 10 characters.';
  if (r && text.length > r.max_chars) return `Keep it to ${r.max_chars} characters.`;
  if (!date) return 'Pick a date for your prediction.';
  if (r && (date < r.min_date || date > r.max_date)) {
    return `Pick a date between ${fmtDate(r.min_date)} and ${fmtDate(r.max_date)}.`;
  }
  return null;
}

async function post() {
  const status = $('pred-status');
  const body = state.pending;
  $('pred-yes').disabled = true;
  status.textContent = 'Posting…';
  try {
    const p = await apiFetch('/api/predictions', { method: 'POST', body, loginReason: 'make a prediction' });
    $('pred-form').reset();
    counter();
    resetConfirm();
    status.textContent = body.remind
      ? `Posted. We will email you on ${fmtDate(p.target_date)}.`
      : 'Posted.';
    onEvent('prediction-post', 'Prediction posted');
    // Show it where it belongs: newest first is the one place it is certain to be.
    state.sort = 'newest';
    renderSorts();
    await load();
    document.getElementById(`prediction-${p.id}`)?.classList.add('fresh');
  } catch (err) {
    status.textContent = err.message;
    resetConfirm();
  } finally {
    $('pred-yes').disabled = false;
  }
}

function renderReminderNote(me) {
  const note = $('pred-reminders-off');
  note.hidden = !(me?.logged_in && me.reminders_off);
}

/* ── wiring ────────────────────────────────────────────────────────────── */
function wire() {
  $('pred-sort').addEventListener('click', (e) => {
    const b = e.target.closest('[data-sort]');
    if (!b || b.dataset.sort === state.sort) return;
    state.sort = b.dataset.sort;
    renderSorts();
    load();
  });
  $('pred-more').addEventListener('click', () => load({ more: true }));

  document.querySelector('#tab-predictions').addEventListener('click', (e) => {
    const li = e.target.closest('li.pred');
    if (!li) return;
    const v = e.target.closest('button.vote');
    if (v && !v.disabled && !li.classList.contains('voting')) { vote(li, Number(v.dataset.vote)); return; }
    if (e.target.closest('[data-report]')) openReport(li);
  });

  // Signed out: the form is there to read, and using it asks for an account.
  // On click and typing rather than focus - closing the dialog hands focus
  // back to the field, and a focus handler would reopen it straight away.
  const form = $('pred-form');
  form.addEventListener('click', (e) => {
    if (!e.target.closest('input, textarea, label, button')) return;
    if (!gate('make a prediction')) e.preventDefault();
  }, true);
  form.addEventListener('keydown', (e) => {
    if (['Tab', 'Shift', 'Escape'].includes(e.key) || !e.target.matches('input, textarea')) return;
    if (!gate('make a prediction')) e.preventDefault();
  }, true);
  $('pred-text').addEventListener('input', () => { counter(); resetConfirm(); });
  $('pred-date').addEventListener('input', resetConfirm);

  $('pred-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (!gate('make a prediction')) return;
    const problem = validate();
    $('pred-status').textContent = problem || '';
    if (problem) return;
    // No edits and no deletes, ever - so say so before it is posted.
    state.pending = {
      text: $('pred-text').value.trim(),
      target_date: $('pred-date').value,
      remind: $('pred-remind').checked,
    };
    $('pred-confirm-date').textContent = fmtDate(state.pending.target_date);
    $('pred-post').hidden = true;
    $('pred-confirm').hidden = false;
    $('pred-yes').focus();
  });
  $('pred-yes').addEventListener('click', post);
  $('pred-no').addEventListener('click', () => { resetConfirm(); $('pred-text').focus(); });

  $('pred-reminders-on').addEventListener('click', async () => {
    try {
      await apiFetch('/api/me/reminders', { method: 'POST', body: { on: true }, loginReason: 'change email reminders' });
      const me = current();
      if (me) me.reminders_off = false;
      renderReminderNote(me);
    } catch (err) {
      $('pred-status').textContent = err.message;
    }
  });
}

/** Called when the Predictions tab is shown. Loads lazily, once. */
export function showPredictions() {
  const m = location.hash.match(/^#prediction-(\d+)$/);
  if (m) showLinked(m[1]);
  if (!state.loaded) load();
}

export function initPredictions({ track } = {}) {
  if (track) onEvent = track;
  renderSorts();
  wire();
  counter();
  onSession((me) => {
    renderReminderNote(me);
    $('pred-signed-out').hidden = !me?.enabled || me.logged_in;
    // Votes and "mine" flags depend on who is asking.
    if (state.loaded) load();
  });
  $('pred-login')?.addEventListener('click', () => requireLogin('make a prediction'));
}
