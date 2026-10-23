/* Signed-in shell: session guard, role portal, router, notifications, settings. */
const PORTALS = {};          // filled by clinician.js / specialist.js / admin.js
let ME = null;
let charts = [];
const ROLE = location.pathname.startsWith('/specialist') ? 'specialist'
  : location.pathname.startsWith('/admin') ? 'admin' : 'clinician';
document.documentElement.dataset.role = ROLE;

async function boot() {
  const user = await authUser().catch(() => null);
  if (!user) return location.replace(ROLE_LOGIN[ROLE]);
  try { ME = await loadProfile(); }
  catch (e) { await fbAuth.signOut(); return location.replace(ROLE_LOGIN[ROLE] + '?e=' + encodeURIComponent(e.message)); }
  if (ME.role !== ROLE) return location.replace(ROLE_HOME[ME.role]);   // never show another role's portal
  buildShell();
  window.addEventListener('hashchange', route);
  route();
  $('boot').remove();
  $('shell').classList.remove('hidden');
  loadNotifications();
  setInterval(loadNotifications, 60000);
}

function buildShell() {
  const P = PORTALS[ROLE];
  document.title = `${P.title} · PneumoScan`;
  $('logo').innerHTML = icon('logo');
  $('portalTag').innerHTML = icon(P.icon, 15) + P.title;
  $('meAvatar').textContent = initials(ME.full_name);
  $('meName').textContent = ME.full_name || ME.email;
  $('meRole').textContent = ROLE_NAME[ROLE] + (ME.hospital ? ' · ' + ME.hospital : '');
  $('logoutBtn').innerHTML = icon('logout', 16);
  $('logoutBtn').onclick = async () => { if (await confirmDialog('Sign out', 'You will need to sign in again to continue.', 'Sign out')) signOut(ROLE_LOGIN[ROLE]); };
  $('menuBtn').innerHTML = icon('menu');
  $('menuBtn').onclick = () => $('side').classList.toggle('open');
  $('bellBtn').innerHTML = icon('bell') + '<span class="dot hidden" id="bellDot"></span>';
  $('bellBtn').onclick = toggleNotifications;
  const renderThemeBtn = () => $('themeBtn').innerHTML = icon(isDark() ? 'sun' : 'moon');
  $('themeBtn').onclick = toggleTheme;
  document.addEventListener('themechange', () => { renderThemeBtn(); route(); });
  renderThemeBtn();
  const nav = [...P.nav, {section: 'Account'}, {id: 'settings', label: 'Settings', icon: 'settings'}];
  $('nav').innerHTML = nav.map(n => n.section ? `<div class="sec">${n.section}</div>`
    : `<a href="#/${n.id}" data-page="${n.id}">${icon(n.icon)}<span>${n.label}</span><span class="count hidden" data-count="${n.id}"></span></a>`).join('');
  document.addEventListener('click', e => {
    if (!e.target.closest('#notifPop') && !e.target.closest('#bellBtn')) $('notifPop').classList.add('hidden');
  });
}

function setCount(page, n) {
  const el = document.querySelector(`[data-count="${page}"]`);
  if (el) { el.textContent = n; el.classList.toggle('hidden', !n); }
}

/* Routes look like #/page or #/page/arg. */
let routeSeq = 0;
async function route() {
  const P = PORTALS[ROLE];
  const [page, ...args] = (location.hash.replace(/^#\/?/, '') || P.home).split('/');
  const def = page === 'settings' ? {title: 'Settings', render: renderSettings} : P.pages[page];
  if (!def) { location.hash = '#/' + P.home; return; }
  const seq = ++routeSeq;
  charts.forEach(c => c.destroy()); charts = [];
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.page === (def.nav || page)));
  $('side').classList.remove('open');
  $('pageTitle').textContent = def.title;
  $('pageCrumb').textContent = def.crumb || P.title;
  const view = $('view');
  view.innerHTML = '<div class="loading"><span class="spin" style="width:24px;height:24px"></span></div>';
  window.scrollTo(0, 0);
  try {
    const html = await def.render(...args.map(decodeURIComponent));
    if (seq !== routeSeq) return;
    if (typeof html === 'string') view.innerHTML = html;
    if (def.after) def.after(...args.map(decodeURIComponent));
  } catch (e) {
    if (seq !== routeSeq) return;
    view.innerHTML = emptyState('alert', 'Something went wrong', esc(e.message),
      `<button class="btn secondary" onclick="route()">${icon('refresh')}Try again</button>`);
  }
}
const go = hash => { if (location.hash === hash) route(); else location.hash = hash; };

/* ---------- shared view helpers ---------- */
function emptyState(ico, title, text = '', action = '') {
  return `<div class="empty"><div class="ico">${icon(ico)}</div><h4>${title}</h4><p>${text}</p>${action ? `<div style="margin-top:14px">${action}</div>` : ''}</div>`;
}
function statCard(ico, value, label, tone = '', detail = '') {
  return `<div class="card stat"><div class="ico ${tone}">${icon(ico)}</div><div><div class="v">${value}</div><div class="l">${label}</div>${detail ? `<div class="d">${detail}</div>` : ''}</div></div>`;
}
function card(title, body, {sub = '', action = '', flush = false} = {}) {
  return `<div class="card"><div class="card-h"><div><h3>${title}</h3>${sub ? `<div class="sub">${sub}</div>` : ''}</div><span class="spacer"></span>${action}</div>
    <div class="card-b ${flush ? 'flush' : ''}">${body}</div></div>`;
}
function intro(title, text, action = '') {
  return `<div class="page-intro row wrap"><div style="flex:1;min-width:240px"><h2>${title}</h2><p>${text}</p></div>${action}</div>`;
}
function waitLabel(iso) {
  const h = hoursSince(iso);
  return `<span class="wait ${h > 24 ? 'late' : h > 4 ? 'mid' : ''}">${duration(h)}</span>`;
}
function chartColors() {
  const s = getComputedStyle(document.documentElement);
  const v = n => s.getPropertyValue(n).trim();
  return {text: v('--text-2'), grid: v('--border'), ok: v('--ok'), danger: v('--danger'), warn: v('--warn'),
          gray: v('--muted'), accent: v('--accent'), info: v('--info')};
}
function makeChart(id, config) {
  const el = $(id);
  if (!el || !window.Chart) return;
  const c = chartColors();
  Chart.defaults.color = c.text;
  Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
  Chart.defaults.borderColor = c.grid;
  charts.push(new Chart(el, config));
}

/* Radiograph with a Grad-CAM overlay whose opacity the user controls. */
function viewer(original, heatmap, id = 'vw') {
  if (!original && !heatmap) return emptyState('image', 'No image stored');
  return `<div class="viewer ${original && heatmap ? '' : 'single'}" id="${id}">
      ${original ? `<img src="${original}" alt="Chest radiograph" onload="this.parentElement.style.aspectRatio=this.naturalWidth+'/'+this.naturalHeight">` : ''}
      ${heatmap ? `<img class="overlay" src="${heatmap}" alt="Grad-CAM heatmap" style="opacity:${original ? .75 : 1}">` : ''}
    </div>
    ${heatmap && original ? `<div class="viewer-ctrl"><span class="small muted" style="white-space:nowrap">${icon('layers', 15)} Heatmap</span>
      <input type="range" min="0" max="100" value="75" oninput="document.querySelector('#${id} .overlay').style.opacity=this.value/100">
      <button class="btn sm secondary" onclick="viewImage('${id}')">${icon('eye', 15)}Enlarge</button></div>
      <p class="small muted" style="margin-top:8px">Warm colours show regions that drove the model's output. Check they fall on the lung fields.</p>`
    : heatmap ? '' : `<p class="small muted" style="margin-top:8px">No heatmap: the image was rejected before classification.</p>`}`;
}
function viewImage(id) {
  const imgs = [...document.querySelectorAll(`#${id} img`)];
  const op = (imgs[1] && imgs[1].style.opacity) || 1;
  const ar = imgs[0].naturalWidth ? imgs[0].naturalWidth + '/' + imgs[0].naturalHeight : '1';
  openModal({title: 'Radiograph', wide: true, body: `<div class="viewer" style="aspect-ratio:${ar};max-height:72vh;margin:0 auto">${imgs.map((i, k) =>
    `<img src="${i.src}" ${k ? `style="opacity:${op}"` : ''}>`).join('')}</div>`});
}
function confidenceMeter(conf, gate) {
  return `<div class="row small" style="margin-bottom:6px"><span class="label">Calibrated confidence</span><span class="spacer"></span><b>${pct(conf)}</b></div>
    <div class="meter"><i style="width:${(conf || 0) * 100}%;background:${conf >= gate ? 'var(--ok)' : 'var(--warn)'}"></i><b style="left:${(gate || 0) * 100}%" title="Safety gate"></b></div>
    <div class="row small muted" style="margin-top:6px"><span>0%</span><span class="spacer"></span><span>Safety gate ${pct(gate)}</span><span class="spacer"></span><span>100%</span></div>`;
}
const confOf = p => p == null ? null : Math.max(p, 1 - p);

/* ---------- notifications ---------- */
let NOTES = [];
async function loadNotifications() {
  try {
    const d = await apiJSON('/api/notifications');
    NOTES = d.notifications;
    const dot = $('bellDot');
    dot.textContent = d.unread > 9 ? '9+' : d.unread;
    dot.classList.toggle('hidden', !d.unread);
    if (!$('notifPop').classList.contains('hidden')) renderNotifications();
    if (PORTALS[ROLE].onNotify) PORTALS[ROLE].onNotify(d);
  } catch (_) { /* offline: keep last state */ }
}
function renderNotifications() {
  $('notifPop').innerHTML = `<div class="card-h"><h3>Notifications</h3><span class="spacer"></span>
      <button class="btn sm ghost" onclick="markAllRead()">Mark all read</button></div>
    <div style="max-height:420px;overflow:auto">${NOTES.length ? NOTES.map(n => `
      <div class="notif ${n.read ? '' : 'unread'}" onclick="openNotification('${esc(n.link)}')">
        <div class="avatar" style="width:30px;height:30px">${icon('bell', 15)}</div>
        <div><div class="t1">${esc(n.title)}</div><div class="t2">${esc(n.body)}</div><div class="t3">${ago(n.created_at)}</div></div></div>`).join('')
      : emptyState('bell', "You're all caught up", 'New referrals and review results appear here.')}</div>`;
}
function toggleNotifications() {
  const pop = $('notifPop');
  pop.classList.toggle('hidden');
  if (!pop.classList.contains('hidden')) renderNotifications();
}
async function markAllRead() {
  await apiJSON('/api/notifications/read', {method: 'POST'}).catch(e => toast(e.message, 'error'));
  loadNotifications();
}
function openNotification(link) {
  $('notifPop').classList.add('hidden');
  markAllRead();
  if (link) go('#/' + link);
}

/* ---------- settings (all roles) ---------- */
function renderSettings() {
  const pref = themePref();
  const opt = (v, ico, label) => `<button type="button" class="${pref === v ? 'on' : ''}" onclick="setTheme('${v}')">${icon(ico, 15)}${label}</button>`;
  return `${intro('Settings', 'Manage your profile, appearance and security.')}
  <div class="grid g-1-1">
    <div class="stack">
      ${card('Appearance', `<div class="row wrap"><div style="flex:1;min-width:200px"><div style="font-weight:700">Theme</div>
          <div class="small muted">Light is the default. System follows your device setting.</div></div>
          <div class="seg">${opt('light', 'sun', 'Light')}${opt('dark', 'moon', 'Dark')}${opt('system', 'monitor', 'System')}</div></div>`)}
      ${ROLE === 'specialist' ? card('Availability', `<div class="row"><div style="flex:1"><div style="font-weight:700">Accepting new referrals</div>
          <div class="small muted">When off, clinicians cannot send you new cases. Cases already in your queue stay there.</div></div>
          <label class="switch"><input type="checkbox" id="availToggle" ${ME.available !== false ? 'checked' : ''} onchange="setAvailability(this.checked)"><span></span></label></div>`) : ''}
      ${card('Profile', `<form class="stack" onsubmit="saveProfile(event)">
          <div class="field"><label>Full name</label><input class="input" id="pfName" value="${esc(ME.full_name)}" required></div>
          <div class="field"><label>Health facility</label><input class="input" id="pfHosp" value="${esc(ME.hospital || '')}"></div>
          <div class="field"><label>Email</label><input class="input" value="${esc(ME.email)}" disabled></div>
          <div class="row"><span class="badge b-accent plain">${ROLE_NAME[ROLE]}</span><span class="small muted">Roles are assigned by an administrator.</span>
            <span class="spacer"></span><button class="btn" id="pfBtn">Save profile</button></div></form>`)}
    </div>
    <div class="stack">
      ${card('Change password', `<form class="stack" onsubmit="changePassword(event)">
          <div class="field"><label>Current password</label><input class="input" type="password" id="pwOld" autocomplete="current-password" required></div>
          <div class="field"><label>New password</label><input class="input" type="password" id="pwNew" minlength="8" autocomplete="new-password" required></div>
          <div class="field"><label>Confirm new password</label><input class="input" type="password" id="pwNew2" minlength="8" autocomplete="new-password" required></div>
          <div class="row"><span class="spacer"></span><button class="btn" id="pwBtn">${icon('lock', 16)}Update password</button></div></form>`)}
      ${card('Session', `<div class="stack small">
          <div class="row"><span class="muted">Signed in as</span><span class="spacer"></span><b>${esc(ME.email)}</b></div>
          <div class="row"><span class="muted">Member since</span><span class="spacer"></span><span>${fmtDate(ME.created_at, false)}</span></div>
          <div class="row"><span class="muted">Session storage</span><span class="spacer"></span><span>This browser tab only</span></div>
          <div class="disclaimer">${icon('alert', 16)}<span>PneumoScan is a research prototype for decision support. It is not a certified medical device and does not replace clinical judgement.</span></div>
          <div class="row"><span class="spacer"></span><button class="btn secondary" onclick="signOut(ROLE_LOGIN[ROLE])">${icon('logout', 16)}Sign out</button></div></div>`)}
    </div>
  </div>`;
}
async function saveProfile(e) {
  e.preventDefault();
  const body = {full_name: $('pfName').value.trim(), hospital: $('pfHosp').value.trim() || null};
  if (!body.full_name) return toast('Name cannot be empty', 'error');
  await withBusy($('pfBtn'), 'Saving…', async () => {
    try {
      await apiJSON('/api/users/me/profile', {method: 'PATCH', json: body});
      Object.assign(ME, body);
      $('meName').textContent = ME.full_name; $('meAvatar').textContent = initials(ME.full_name);
      $('meRole').textContent = ROLE_NAME[ROLE] + (ME.hospital ? ' · ' + ME.hospital : '');
      toast('Profile saved', 'success');
    } catch (err) { toast(err.message, 'error'); }
  });
}
async function changePassword(e) {
  e.preventDefault();
  const old = $('pwOld').value, nw = $('pwNew').value;
  if (nw.length < 8) return toast('New password must be at least 8 characters', 'error');
  if (nw !== $('pwNew2').value) return toast('New passwords do not match', 'error');
  await withBusy($('pwBtn'), 'Updating…', async () => {
    try {
      const u = fbAuth.currentUser;
      await u.reauthenticateWithCredential(firebase.auth.EmailAuthProvider.credential(u.email, old));
      await u.updatePassword(nw);
      e.target.reset();
      toast('Password updated', 'success');
    } catch (err) { toast(err.code ? authMessage(err) : err.message, 'error'); }
  });
}
async function setAvailability(on) {
  try {
    await apiJSON('/api/users/me/availability', {method: 'PATCH', json: {available: on}});
    ME.available = on;
    toast(on ? 'You are accepting new referrals' : 'New referrals paused', 'success');
    document.querySelectorAll('[data-avail]').forEach(el => el.checked = on);
  } catch (err) { toast(err.message, 'error'); route(); }
}

function downloadCSV(name, header, rows) {
  const q = v => { const s = String(v ?? ''); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const csv = [header, ...rows].map(r => r.map(q).join(',')).join('\n');
  const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  const a = Object.assign(document.createElement('a'), {href: url, download: name});
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
const searchBox = (id, placeholder, handler) => `<div class="input-wrap" style="min-width:240px;flex:1;max-width:360px">
  <input class="input" id="${id}" placeholder="${placeholder}" oninput="${handler}" style="padding-left:36px">
  <span style="position:absolute;left:11px;top:11px;color:var(--muted)">${icon('search', 17)}</span></div>`;
