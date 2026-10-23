/* PneumoScan shared browser helpers: theme, Firebase session, API, icons, toasts, modals. */
(function initTheme() {
  const pref = localStorage.getItem('ps-theme') || 'light';
  const dark = pref === 'dark' || (pref === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();

function setTheme(pref) {
  localStorage.setItem('ps-theme', pref);
  const dark = pref === 'dark' || (pref === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.dispatchEvent(new CustomEvent('themechange'));
}
const themePref = () => localStorage.getItem('ps-theme') || 'light';
const isDark = () => document.documentElement.dataset.theme === 'dark';
function toggleTheme() { setTheme(isDark() ? 'light' : 'dark'); }

const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const ICON_PATHS = {
  logo: '<path d="M12 3v8"/><path d="M12 11c-1 1-2.5 1.5-4 1.5"/><path d="M12 11c1 1 2.5 1.5 4 1.5"/><path d="M7.5 6C5 7 3 11 3 16c0 2.5 1.5 4 3.5 4S10 19 10 16v-5.5C10 8 9 6 7.5 6z"/><path d="M16.5 6C19 7 21 11 21 16c0 2.5-1.5 4-3.5 4S14 19 14 16v-5.5C14 8 15 6 16.5 6z"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
  check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  tick: '<polyline points="20 6 9 17 4 12"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  question: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  ban: '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  chart: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  menu: '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
  arrow: '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
  back: '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
  steth: '<path d="M4.8 2.3A.3.3 0 1 0 5 2H4a2 2 0 0 0-2 2v5a6 6 0 0 0 6 6 6 6 0 0 0 6-6V4a2 2 0 0 0-2-2h-1a.2.2 0 1 0 .3.3"/><path d="M8 15v1a6 6 0 0 0 6 6 6 6 0 0 0 6-6v-4"/><circle cx="20" cy="10" r="2"/>',
  clipboard: '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  heart: '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78L12 21.23l8.84-8.84a5.5 5.5 0 0 0 0-7.78z"/>',
  globe: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  power: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
};
const icon = (name, size) => `<svg class="i" viewBox="0 0 24 24"${size ? ` style="width:${size}px;height:${size}px"` : ''}>${ICON_PATHS[name] || ''}</svg>`;

const STATE = {
  CLEARED:        {label: 'Cleared',        short: 'Normal',     icon: 'check',    tone: 'ok',
                   desc: 'Model is confident the radiograph is normal.'},
  FLAGGED_URGENT: {label: 'Flagged urgent', short: 'Pneumonia',  icon: 'alert',    tone: 'danger',
                   desc: 'Model is confident pneumonia is present.'},
  ESCALATED:      {label: 'Escalated',      short: 'Uncertain',  icon: 'question', tone: 'warn',
                   desc: 'Model confidence is below the safety gate.'},
  REJECTED:       {label: 'Rejected',       short: 'Not assessed', icon: 'ban',    tone: 'gray',
                   desc: 'Input does not look like a paediatric chest X-ray.'},
};
const stateBadge = s => `<span class="badge b-${esc(s)}">${esc((STATE[s] || {label: s || '—'}).label)}</span>`;
const ESC_STATUS = {sent: 'Awaiting review', pending: 'In review', resolved: 'Resolved'};
const escBadge = s => `<span class="badge b-${esc(s)}">${esc(ESC_STATUS[s] || s)}</span>`;
const outcomeBadge = o => o ? `<span class="badge b-${esc(o)}">${esc(o[0].toUpperCase() + o.slice(1))}</span>` : '<span class="muted">—</span>';
const priorityBadge = p => p === 'urgent' ? '<span class="badge b-urgent">Urgent</span>' : '<span class="badge b-routine">Routine</span>';
const ROLE_HOME = {clinician: '/clinician', specialist: '/specialist', admin: '/admin'};
const ROLE_LOGIN = {clinician: '/login', specialist: '/specialist/login', admin: '/admin/login'};
const ROLE_NAME = {clinician: 'Clinician', specialist: 'Specialist', admin: 'Administrator'};

function fmtDate(iso, withTime = true) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleDateString(undefined, {day: '2-digit', month: 'short', year: 'numeric'}) +
    (withTime ? ' · ' + d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'}) : '');
}
function hoursSince(iso) { return iso ? (Date.now() - new Date(iso).getTime()) / 36e5 : 0; }
function ago(iso) {
  if (!iso) return '—';
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  if (m < 1440) return Math.round(m / 60) + ' h ago';
  return Math.round(m / 1440) + ' d ago';
}
function duration(h) {
  if (h == null) return '—';
  if (h < 1) return Math.max(1, Math.round(h * 60)) + ' min';
  if (h < 48) return h.toFixed(1) + ' h';
  return (h / 24).toFixed(1) + ' d';
}
const pct = (x, d = 1) => x == null ? '—' : (x * 100).toFixed(d) + '%';
const initials = name => (name || '?').split(/\s+/).filter(w => w && !/^dr\.?$/i.test(w)).slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';

function toast(msg, type = '') {
  let box = $('toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = icon(type === 'error' ? 'alert' : type === 'success' ? 'tick' : 'bell') + `<span>${esc(msg)}</span>`;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* Opens a modal. `body` is HTML; returns {el, close}. Elements with data-close close it. */
function openModal({title, body, footer = '', wide = false, onOpen}) {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
      <div class="modal-h"><h3>${title}</h3><span class="spacer"></span>
        <button class="icon-btn" data-close aria-label="Close" style="width:32px;height:32px">${icon('x')}</button></div>
      <div class="modal-b">${body}</div>${footer ? `<div class="modal-f">${footer}</div>` : ''}</div>`;
  const close = () => { bg.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  bg.addEventListener('click', e => { if (e.target === bg || e.target.closest('[data-close]')) close(); });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(bg);
  const m = {el: bg, close, q: sel => bg.querySelector(sel)};
  if (onOpen) onOpen(m);
  const first = bg.querySelector('input:not([type=hidden]),select,textarea');
  if (first) first.focus();
  return m;
}
function confirmDialog(title, text, okLabel = 'Confirm', danger = false) {
  return new Promise(resolve => {
    const m = openModal({title, body: `<p class="muted">${text}</p>`,
      footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn ${danger ? 'danger' : ''}" id="cdOk">${esc(okLabel)}</button>`});
    m.q('#cdOk').onclick = () => { m.close(); resolve(true); };
    m.el.addEventListener('click', e => { if (e.target.closest('[data-close]') || e.target === m.el) resolve(false); });
  });
}
async function withBusy(btn, label, fn) {
  const html = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>${esc(label)}`;
  try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = html; }
}

/* ---------------- Firebase + API ---------------- */
let fbAuth = null;
const fbReady = fetch('/api/firebase-config')
  .then(r => r.ok ? r.json() : r.json().then(d => Promise.reject(new Error(d.detail || 'Firebase is not configured'))))
  .then(cfg => {
    firebase.initializeApp(cfg);
    fbAuth = firebase.auth();
    fbAuth.setPersistence(firebase.auth.Auth.Persistence.SESSION);
    if (cfg.authEmulatorUrl) fbAuth.useEmulator(cfg.authEmulatorUrl, {disableWarnings: true});
    return fbAuth;
  });
fbReady.catch(err => toast('Sign-in unavailable: ' + err.message, 'error'));

/* Resolves with the current Firebase user (or null) once the session has been restored. */
const authUser = () => fbReady.then(a => new Promise(res => { const off = a.onAuthStateChanged(u => { off(); res(u); }); }));

async function api(url, opts = {}) {
  await fbReady;
  const headers = {...(opts.headers || {})};
  if (fbAuth.currentUser) headers.Authorization = 'Bearer ' + await fbAuth.currentUser.getIdToken();
  if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; opts = {...opts, body: JSON.stringify(opts.json)}; }
  return fetch(url, {...opts, headers});
}
async function apiJSON(url, opts = {}) {
  const res = await api(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : `Request failed (${res.status})`);
  }
  return data;
}
async function downloadReport(pid) {
  try {
    const res = await api(`/api/report/${encodeURIComponent(pid)}`);
    if (!res.ok) throw new Error((await res.json()).detail || 'Report unavailable');
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement('a'), {href: url, download: `pneumoscan_report_${pid}.pdf`});
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) { toast(e.message, 'error'); }
}

const AUTH_ERRORS = {
  'auth/invalid-credential': 'Incorrect email or password.',
  'auth/invalid-login-credentials': 'Incorrect email or password.',
  'auth/wrong-password': 'Incorrect email or password.',
  'auth/user-not-found': 'Incorrect email or password.',
  'auth/user-disabled': 'This account has been deactivated. Contact your administrator.',
  'auth/invalid-email': 'Enter a valid email address.',
  'auth/email-already-in-use': 'An account with this email already exists.',
  'auth/weak-password': 'Password must be at least 8 characters.',
  'auth/too-many-requests': 'Too many attempts. Wait a moment and try again.',
  'auth/network-request-failed': 'Network error. Check your connection.',
  'auth/requires-recent-login': 'Sign in again before changing your password.',
  'auth/missing-password': 'Enter your password.',
};
const authMessage = err => AUTH_ERRORS[err.code] || err.message;

async function loadProfile() { return apiJSON('/api/auth/me'); }
async function signOut(to = '/') { try { await fbAuth.signOut(); } catch (_) {} location.href = to; }
