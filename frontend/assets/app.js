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
  const gateReq = apiJSON('/api/model-info').catch(() => ({gate: {}}));
  try { ME = await loadProfile(); }
  catch (e) { await fbAuth.signOut(); return location.replace(ROLE_LOGIN[ROLE] + '?e=' + encodeURIComponent(e.message)); }
  if (ME.role !== ROLE) return location.replace(ROLE_HOME[ME.role]);   // never show another role's portal
  if (ME.mfa_required && !ME.mfa_verified) return location.replace(ROLE_LOGIN[ROLE]);
  if (ME.language && !localStorage.getItem('ps-lang') && window.setLang) setLang(ME.language, false);
  GATE_INFO = (await gateReq).gate || {};
  buildShell();
  window.addEventListener('hashchange', route);
  route();
  $('boot').remove();
  $('shell').classList.remove('hidden');
  loadNotifications();
  setInterval(loadNotifications, 30000);
  fbAuth.onAuthStateChanged(u => { if (!u) location.replace(ROLE_LOGIN[ROLE]); });
  watchIdle();
}

/* Signs out after 30 minutes without interaction, with a warning 2 minutes before. */
const IDLE_MS = 30 * 60 * 1000, WARN_MS = 28 * 60 * 1000;
let lastActive = Date.now(), idleWarned = false;
function watchIdle() {
  const bump = () => { lastActive = Date.now(); idleWarned = false; };
  ['pointerdown', 'keydown', 'scroll', 'touchstart'].forEach(ev => addEventListener(ev, bump, {passive: true}));
  setInterval(() => {
    const idle = Date.now() - lastActive;
    if (idle > IDLE_MS) signOut(ROLE_LOGIN[ROLE] + '?out=idle');
    else if (idle > WARN_MS && !idleWarned) { idleWarned = true; toast('You will be signed out in 2 minutes due to inactivity.', 'info'); }
  }, 15000);
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
  $('logoutBtn').onclick = confirmSignOut;
  $('menuBtn').innerHTML = icon('menu');
  $('menuBtn').onclick = () => $('side').classList.toggle('open');
  $('bellBtn').innerHTML = icon('bell') + '<span class="dot hidden" id="bellDot"></span>';
  $('bellBtn').onclick = toggleNotifications;
  const renderThemeBtn = () => $('themeBtn').innerHTML = icon(isDark() ? 'sun' : 'moon');
  $('themeBtn').onclick = toggleTheme;
  document.addEventListener('themechange', () => { renderThemeBtn(); route(); });
  renderThemeBtn();
  mountLangSelect($('langSel'));
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
  const def = page === 'settings' ? {title: 'Settings', render: renderSettings}
    : page === 'messages' && ROLE !== 'admin' ? {title: 'Messages', crumb: 'Clinician–specialist conversations', render: renderMessages}
    : P.pages[page];
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
/* Confidence the gate requires for the side the model leans to (two one-sided thresholds). */
let GATE_INFO = {};
function gateConf(p) {
  const g = GATE_INFO, c = g.confidence_threshold;
  const tp = g.pneumonia_threshold ?? (c != null ? Math.max(c, 1 - c) : null);
  const tn = g.normal_threshold ?? (tp != null ? 1 - tp : null);
  if (p == null || tp == null) return null;
  return p >= 0.5 ? tp : 1 - tn;
}

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
    setCount('messages', d.unread_messages || 0);
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
      : emptyState('bell', "You're all caught up", 'New referrals, messages and review results appear here.')}</div>`;
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
      ${card('Language', `<div class="row wrap"><div style="flex:1;min-width:200px"><div style="font-weight:700">Display language</div>
          <div class="small muted">Applies to menus, buttons and labels. Clinical notes are not translated.</div></div>
          <div class="seg">${LANGS.map(([v, l]) => `<button type="button" class="${currentLang() === v ? 'on' : ''}" onclick="setLang('${v}');route()">${l}</button>`).join('')}</div></div>`)}
      ${ROLE !== 'clinician' ? card('Urgent alerts', `<form class="stack" onsubmit="saveAlerts(event)">
          <div class="small muted">${ROLE === 'specialist' ? 'Get an email when an urgent referral arrives or waits too long.' : 'Get an email when an urgent referral is overdue.'}</div>
          <div class="row"><div style="flex:1"><b>Email alerts</b><div class="small muted">${esc(ME.email)}</div></div><label class="switch"><input type="checkbox" id="alEmail" ${ME.alert_email !== false ? 'checked' : ''}><span></span></label></div>
          <div class="row"><button type="button" class="btn secondary sm" id="alTest" onclick="testAlert()">${icon('send', 14)}Send test alert</button><span class="spacer"></span><button class="btn" id="alBtn">Save alerts</button></div></form>`) : ''}
      ${ROLE === 'specialist' ? card('Availability', `<div class="row"><div style="flex:1"><div style="font-weight:700">Accepting new referrals</div>
          <div class="small muted">When off, clinicians cannot send you new cases. Cases already in your queue stay there.</div></div>
          <label class="switch"><input type="checkbox" id="availToggle" ${ME.available !== false ? 'checked' : ''} onchange="setAvailability(this.checked)"><span></span></label></div>`) : ''}
      ${card('Profile', `<form class="stack" onsubmit="saveProfile(event)">
          <div class="field"><label>Full name</label><input class="input" id="pfName" value="${esc(ME.full_name)}" required></div>
          <div class="field"><label>Health facility</label><input class="input" id="pfHosp" value="${esc(ME.hospital || '')}"></div>
          <div class="field"><label>Email</label><input class="input" value="${esc(ME.email)}" disabled></div>
          <div class="row"><span class="badge b-accent plain">${ROLE_NAME[ROLE]}</span><span class="small muted">Roles are assigned by an administrator.</span>
            <span class="spacer"></span><button class="btn" id="pfBtn">Save profile</button></div></form>`)}
      ${ROLE !== 'admin' ? card('Help & support', `<div class="help-list">
          ${helpRow('faq', 'question', 'Frequently asked questions', 'Results, referrals, reports and your account.')}
          ${helpRow('privacy', 'shield', 'Privacy policy', 'What PneumoScan stores and who can see it.')}
          ${helpRow('terms', 'file', 'Terms of service', 'Conditions for using this research prototype.')}</div>
          <p class="small muted" style="margin-top:12px">Need more help? Contact your PneumoScan administrator.</p>`) : ''}
    </div>
    <div class="stack">
      ${card('Change password', `<form class="stack" onsubmit="changePassword(event)">
          <div class="field"><label>Current password</label><input class="input" type="password" id="pwOld" autocomplete="current-password" required></div>
          <div class="field"><label>New password</label><input class="input" type="password" id="pwNew" minlength="8" autocomplete="new-password" required></div>
          <div class="field"><label>Confirm new password</label><input class="input" type="password" id="pwNew2" minlength="8" autocomplete="new-password" required></div>
          <div class="row"><span class="spacer"></span><button class="btn" id="pwBtn">${icon('lock', 16)}Update password</button></div></form>`)}
      ${ROLE !== 'clinician' ? card('Two-factor sign-in', ME.mfa_enabled
        ? `<div class="stack"><div class="row"><span class="badge b-ok">${icon('shield', 13)} On</span><span class="small muted">${ME.mfa_method === 'email' ? `A 6-digit code is emailed to ${esc(ME.email)} at every sign-in.` : 'A 6-digit code from your authenticator app is needed at every sign-in. You can also ask for a code by email.'}</span></div>
            <div class="row"><span class="spacer"></span><button class="btn secondary" onclick="mfaDisable()">Turn off</button></div></div>`
        : `<div class="stack"><div class="row"><span class="badge b-warn">Off</span><span class="small muted">Protect your account with an authenticator app such as Google Authenticator or Microsoft Authenticator.</span></div>
            <div class="row wrap"><span class="spacer"></span><button class="btn secondary" onclick="mfaEmailSetup()">${icon('send', 16)}Use email codes</button><button class="btn" onclick="mfaSetup()">${icon('lock', 16)}Use authenticator app</button></div></div>`) : ''}
      ${card('Session', `<div class="stack small">
          <div class="row"><span class="muted">Signed in as</span><span class="spacer"></span><b>${esc(ME.email)}</b></div>
          <div class="row"><span class="muted">Member since</span><span class="spacer"></span><span>${fmtDate(ME.created_at, false)}</span></div>
          <div class="row"><span class="muted">Session storage</span><span class="spacer"></span><span>This browser tab only</span></div>
          ${disclaimerBox()}
          <div class="row"><span class="spacer"></span><button class="btn secondary" onclick="confirmSignOut()">${icon('logout', 16)}Sign out</button></div></div>`)}
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
async function saveAlerts(e) {
  e.preventDefault();
  const body = {alert_email: $('alEmail').checked};
  await withBusy($('alBtn'), 'Saving…', async () => {
    try { const r = await apiJSON('/api/users/me/profile', {method: 'PATCH', json: body}); ME.alert_email = r.alert_email; toast('Alert settings saved', 'success'); }
    catch (err) { toast(err.message, 'error'); }
  });
}
async function testAlert() {
  await withBusy($('alTest'), 'Sending…', async () => {
    try {
      const r = await apiJSON('/api/users/me/test-alert', {method: 'POST'});
      if (r.status === 'sent') toast(`Test email sent to ${ME.email}. Check the inbox (and spam).`, 'success');
      else if (r.status === 'failed') toast(`The test email could not be sent: ${r.error || 'unknown error'}. Ask your administrator to check Email settings.`, 'error');
      else toast('Test alert logged. Email is in test mode, so nothing was sent.', 'info');
    } catch (err) { toast(err.message, 'error'); }
  });
}
async function mfaSetup() {
  let s;
  try { s = await apiJSON('/api/auth/mfa/setup', {method: 'POST'}); } catch (e) { return toast(e.message, 'error'); }
  const m = openModal({title: 'Set up two-factor sign-in', body: `
    <ol class="mfa-steps"><li>Open an authenticator app on your phone and scan this QR code.</li>
      <li>If you cannot scan it, enter the key manually.</li><li>Type the 6-digit code the app shows.</li></ol>
    <div class="mfa-qr">${s.qr_svg ? `<img src="${s.qr_svg}" alt="QR code for your authenticator app">` : ''}
      <div><div class="label">Setup key</div><code class="mfa-key">${esc(s.secret.replace(/(.{4})/g, '$1 ').trim())}</code></div></div>
    <div class="field" style="margin-top:14px"><label>6-digit code</label><input class="input mfa-code" id="mfaCode" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="123456"></div>`,
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="mfaOn">Turn on</button>`});
  setTimeout(() => m.q('#mfaCode').focus(), 50);
  m.q('#mfaOn').onclick = () => withBusy(m.q('#mfaOn'), 'Verifying…', async () => {
    try {
      const r = await apiJSON('/api/auth/mfa/enable', {method: 'POST', json: {code: m.q('#mfaCode').value}});
      sessionStorage.setItem('ps-mfa', r.mfa_token);
      ME.mfa_enabled = true; m.close();
      openModal({title: 'Save your recovery codes', body: `<p class="muted" style="margin-bottom:12px">Two-factor sign-in is on. If you lose your phone, each code below signs you in once. Store them somewhere safe; they will not be shown again.</p>
        <div class="recovery">${r.recovery_codes.map(c => `<code>${esc(c)}</code>`).join('')}</div>`,
        footer: `<button class="btn secondary" onclick="navigator.clipboard.writeText('${r.recovery_codes.join('\\n')}').then(() => toast('Copied', 'success'))">Copy</button><button class="btn" data-close>Done</button>`});
      route();
    } catch (e) { toast(e.message, 'error'); }
  });
}
async function mfaEmailSetup() {
  let r;
  try { r = await apiJSON('/api/auth/mfa/email/setup', {method: 'POST'}); } catch (e) { return toast(e.message, 'error'); }
  const m = openModal({title: 'Set up email codes', body: `<p class="muted" style="margin-bottom:12px">We emailed a 6-digit code to <b>${esc(r.sent_to)}</b>. Enter it below. It expires in 10 minutes.</p>
    <div class="field"><label>6-digit code</label><input class="input mfa-code" id="mfaECode" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="123456"></div>`,
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="mfaEOn">Turn on</button>`});
  setTimeout(() => m.q('#mfaECode').focus(), 50);
  m.q('#mfaEOn').onclick = () => withBusy(m.q('#mfaEOn'), 'Verifying…', async () => {
    try {
      const x = await apiJSON('/api/auth/mfa/email/enable', {method: 'POST', json: {code: m.q('#mfaECode').value}});
      sessionStorage.setItem('ps-mfa', x.mfa_token);
      Object.assign(ME, {mfa_enabled: true, mfa_method: 'email'}); m.close();
      openModal({title: 'Save your recovery codes', body: `<p class="muted" style="margin-bottom:12px">Two-factor sign-in is on. If you cannot get email, each code below signs you in once. Store them somewhere safe; they will not be shown again.</p>
        <div class="recovery">${x.recovery_codes.map(c => `<code>${esc(c)}</code>`).join('')}</div>`, footer: `<button class="btn" data-close>Done</button>`});
      route();
    } catch (e) { toast(e.message, 'error'); }
  });
}
function mfaDisable() {
  const m = openModal({title: 'Turn off two-factor sign-in', body: `<p class="muted" style="margin-bottom:12px">Enter a current code from your authenticator app, or a recovery code.</p>
    <div class="field"><label>Code</label><input class="input mfa-code" id="mfaOff" autocomplete="one-time-code"></div>`,
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn danger" id="mfaOffBtn">Turn off</button>`});
  m.q('#mfaOffBtn').onclick = () => withBusy(m.q('#mfaOffBtn'), 'Turning off…', async () => {
    try { await apiJSON('/api/auth/mfa/disable', {method: 'POST', json: {code: m.q('#mfaOff').value}}); ME.mfa_enabled = false; sessionStorage.removeItem('ps-mfa'); m.close(); toast('Two-factor sign-in turned off', 'success'); route(); }
    catch (e) { toast(e.message, 'error'); }
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


/* ---------- clinical context (WHO IMCI-style flags) ---------- */
const DANGER_SIGNS = {chest_indrawing: 'Chest indrawing', unable_to_feed: 'Unable to drink / feed', lethargy: 'Lethargic or unconscious',
  convulsions: 'Convulsions', stridor: 'Stridor when calm', cyanosis: 'Central cyanosis', grunting: 'Grunting', vomiting_everything: 'Vomits everything'};
function fastBreathingLimit(age) {
  if (age == null || age === '') return null;
  return age < 1 ? 50 : age < 5 ? 40 : 30;
}
function clinicalFlags(c, age) {
  if (!c) return [];
  const flags = [];
  (c.danger_signs || []).forEach(k => flags.push({tone: 'danger', text: DANGER_SIGNS[k] || k}));
  if (c.spo2 != null && c.spo2 < 90) flags.push({tone: 'danger', text: `Hypoxaemia (SpO₂ ${c.spo2}%)`});
  else if (c.spo2 != null && c.spo2 < 94) flags.push({tone: 'warn', text: `Low SpO₂ (${c.spo2}%)`});
  const lim = fastBreathingLimit(age);
  if (c.respiratory_rate != null && lim && c.respiratory_rate >= lim) flags.push({tone: 'warn', text: `Fast breathing (${c.respiratory_rate}/min, limit ${lim})`});
  if (c.temperature_c != null && c.temperature_c >= 38) flags.push({tone: 'warn', text: `Fever (${c.temperature_c} °C)`});
  return flags;
}
const isSevere = (c, age) => clinicalFlags(c, age).some(f => f.tone === 'danger');
function clinicalCard(c, age) {
  if (!c || (c.respiratory_rate == null && c.spo2 == null && c.temperature_c == null && !(c.danger_signs || []).length))
    return card('Clinical context', '<p class="muted small">No vital signs were recorded with this scan.</p>');
  const flags = clinicalFlags(c, age);
  return card('Clinical context', `<div class="kv"><div><div class="k">Resp. rate</div><div class="v">${c.respiratory_rate ?? '—'}<span class="small muted">${c.respiratory_rate != null ? ' /min' : ''}</span></div></div>
      <div><div class="k">SpO₂</div><div class="v">${c.spo2 != null ? c.spo2 + '%' : '—'}</div></div>
      <div><div class="k">Temperature</div><div class="v">${c.temperature_c != null ? c.temperature_c + ' °C' : '—'}</div></div></div>
    ${flags.length ? `<div class="flag-list" style="margin-top:12px">${flags.map(f => `<span class="badge b-${f.tone}">${esc(f.text)}</span>`).join('')}</div>` : '<p class="small muted" style="margin-top:10px">No WHO danger signs recorded.</p>'}
    ${isSevere(c, age) ? `<div class="form-error" style="margin-top:12px">Severe-illness criteria present: manage as severe pneumonia and refer urgently regardless of the X-ray result.</div>` : ''}`,
    {sub: 'Recorded at the time of the scan'});
}

/* ---------- clinician–specialist chat ---------- */
const CHATS = {};
const QUICK = {
  clinician: ['Any update on this case?', 'The child\'s condition has worsened.', 'Additional history: ', 'Thank you, doctor.'],
  specialist: ['Reviewing now.', 'Please send RR, SpO₂ and temperature.', 'Please repeat the X-ray with better positioning.', 'Start treatment per guideline while I review.'],
};
function chatBox(escId, {tall = false, closed = false} = {}) {
  setTimeout(() => chatMount(escId), 0);
  return `<div class="chat ${tall ? 'tall' : ''}" id="chat-${esc(escId)}">
    <div class="chat-log"><div class="loading"><span class="spin"></span></div></div>
    ${closed ? '' : `<div class="chips">${(QUICK[ROLE] || []).map(q => `<button type="button" class="chip" onclick="chatQuick('${esc(escId)}', this.textContent)">${esc(q)}</button>`).join('')}</div>`}
    <form class="chat-input" onsubmit="chatSend(event, '${esc(escId)}')">
      <textarea class="input" rows="1" maxlength="2000" placeholder="Write a message… (Enter to send, Shift+Enter for a new line)"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();this.form.requestSubmit();}"
        oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,140)+'px'"></textarea>
      <button class="btn" aria-label="Send">${icon('send', 16)}</button></form></div>`;
}
function chatQuick(escId, text) {
  const ta = document.querySelector(`#chat-${escId} textarea`);
  ta.value = text; ta.focus(); ta.setSelectionRange(text.length, text.length);
}
async function chatMount(escId) {
  const old = CHATS[escId];
  if (old) clearInterval(old.timer);
  const st = CHATS[escId] = {messages: [], last: null, otherRead: null, timer: null};
  await chatPoll(escId, true);
  st.timer = setInterval(() => {
    if (!document.getElementById('chat-' + escId)) { clearInterval(st.timer); delete CHATS[escId]; return; }
    if (!document.hidden) chatPoll(escId);
  }, 4000);
}
async function chatPoll(escId, first = false) {
  const st = CHATS[escId];
  if (!st || st.busy) return;
  st.busy = true;
  try {
    const d = await apiJSON(`/api/escalations/${encodeURIComponent(escId)}/messages` + (st.last ? `?after=${encodeURIComponent(st.last)}` : ''));
    const fresh = d.messages.filter(m => !st.messages.some(x => x.message_id === m.message_id));
    st.messages.push(...fresh);
    if (st.messages.length) st.last = st.messages[st.messages.length - 1].created_at;
    const readChanged = d.other_read_at !== st.otherRead;
    st.otherRead = d.other_read_at;
    if (first || fresh.length || readChanged) chatRender(escId, first || fresh.some(m => m.sender_id === ME.user_id));
    if (first || fresh.length) chatMarkRead(escId);
    if (fresh.length || first) loadNotifications();
  } catch (e) {
    const log = document.querySelector(`#chat-${escId} .chat-log`);
    if (log && first) log.innerHTML = emptyState('alert', 'Conversation unavailable', esc(e.message));
  } finally { st.busy = false; }
}
/* Opening a thread reads it: clear its unread count everywhere it is shown. */
function chatMarkRead(escId) {
  const c = CONVS.find(x => x.escalation_id === escId);
  if (c && c.unread) { c.unread = 0; if (document.getElementById('convList')) $('convList').innerHTML = convItems(escId); }
  if (typeof spEscs !== 'undefined') { const e = spEscs.find(x => x.escalation_id === escId); if (e) e.unread = 0; }
}
function chatRender(escId, forceBottom) {
  const log = document.querySelector(`#chat-${escId} .chat-log`);
  const st = CHATS[escId];
  if (!log || !st) return;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  if (!st.messages.length) {
    log.innerHTML = emptyState('chat', 'No messages yet', ROLE === 'clinician' ? 'Ask the specialist a question or add clinical details.' : 'Ask the referring clinician for more information.');
    return;
  }
  let day = '', html = '';
  const myLast = [...st.messages].reverse().find(m => m.sender_id === ME.user_id);
  st.messages.forEach(m => {
    const d = new Date(m.created_at).toDateString();
    if (d !== day) { day = d; html += `<div class="chat-day">${d === new Date().toDateString() ? 'Today' : fmtDate(m.created_at, false)}</div>`; }
    const me = m.sender_id === ME.user_id;
    const seen = me && m === myLast && st.otherRead && st.otherRead >= m.created_at;
    html += `<div class="msg ${me ? 'me' : ''}">${me ? '' : `<div class="who">${esc(m.sender_name || '')} · ${ROLE_NAME[m.sender_role] || ''}</div>`}${esc(m.text)}
      <div class="meta">${new Date(m.created_at).toLocaleTimeString(uiLocale(), {hour: '2-digit', minute: '2-digit'})}${me ? (seen ? ' · Seen' : ' · Sent') : ''}</div></div>`;
  });
  log.innerHTML = html;
  if (forceBottom || nearBottom) log.scrollTop = log.scrollHeight;
}
async function chatSend(e, escId) {
  e.preventDefault();
  const ta = e.target.querySelector('textarea'), btn = e.target.querySelector('button');
  const text = ta.value.trim();
  if (!text) return;
  btn.disabled = true;
  try {
    const m = await apiJSON(`/api/escalations/${encodeURIComponent(escId)}/messages`, {method: 'POST', json: {text}});
    ta.value = ''; ta.style.height = 'auto';
    const st = CHATS[escId];
    if (st && !st.messages.some(x => x.message_id === m.message_id)) { st.messages.push(m); st.last = m.created_at; chatRender(escId, true); }
  } catch (err) { toast(err.message, 'error'); }
  finally { btn.disabled = false; ta.focus(); }
}

/* ---------- messages page (clinician + specialist) ---------- */
let CONVS = [];
async function renderMessages(escId) {
  CONVS = (await apiJSON('/api/conversations')).conversations;
  const cur = CONVS.find(c => c.escalation_id === escId);
  setTimeout(() => {
    const t = setInterval(async () => {
      if (!document.getElementById('convList')) return clearInterval(t);
      try { CONVS = (await apiJSON('/api/conversations')).conversations; $('convList').innerHTML = convItems(escId); } catch (_) {}
    }, 10000);
  });
  const caseLink = c => ROLE === 'specialist' ? `#/review/${esc(c.escalation_id)}` : `#/case/${esc(c.prediction_id)}`;
  return `${intro('Messages', ROLE === 'clinician' ? 'Talk to the specialists reviewing your referrals.' : 'Talk to the clinicians who referred cases to you.')}
  <div class="card"><div class="msgs-layout ${cur ? 'open' : ''}">
    <div class="conv-list" id="convList">${convItems(escId)}</div>
    <div class="thread">${cur ? `<div class="card-h row wrap"><a class="btn sm ghost mobile-back" href="#/messages">${icon('back', 15)}</a>
        <div class="avatar">${initials(cur.other_name)}</div>
        <div style="flex:1 1 180px;min-width:150px"><h3>${esc(cur.other_name || 'Unassigned')}</h3><div class="sub">${esc(cur.patient_name || 'Unknown patient')} · referral ${esc(cur.escalation_id)}</div></div>
        ${priorityBadge(cur.priority)}${escBadge(cur.status)}<a class="btn sm secondary" href="${caseLink(cur)}">${icon('eye', 15)}${ROLE === 'specialist' ? 'Open review' : 'Open case'}</a></div>
        ${chatBox(cur.escalation_id, {tall: true})}`
      : `<div class="thread-empty" style="height:100%;min-height:420px">${emptyState('chat', CONVS.length ? 'Select a conversation' : 'No conversations yet',
          CONVS.length ? 'Each referral has its own thread.' : (ROLE === 'clinician' ? 'Refer a case to a specialist to start a conversation.' : 'Conversations appear when cases are referred to you.'))}</div>`}</div>
  </div></div>`;
}
function convItems(active) {
  if (!CONVS.length) return `<div style="padding:20px" class="small muted">No referrals yet.</div>`;
  return CONVS.map(c => `<div class="conv ${c.escalation_id === active ? 'on' : ''}" onclick="go('#/messages/${esc(c.escalation_id)}')">
    <div class="avatar">${initials(c.patient_name)}</div>
    <div style="flex:1;min-width:0"><div class="row" style="gap:6px"><b style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(c.patient_name || 'Unknown')}</b><span class="spacer"></span><span class="small muted" style="white-space:nowrap">${c.updated_at ? ago(c.updated_at) : ''}</span></div>
      <div class="small" style="color:var(--text-2)">${esc(c.other_name || '')}${c.priority === 'urgent' ? ' · <span style="color:var(--danger);font-weight:700">urgent</span>' : ''}</div>
      <div class="preview">${c.last_message ? (c.last_message.sender_id === ME.user_id ? 'You: ' : '') + esc(c.last_message.text) : 'No messages yet'}</div></div>
    ${c.unread && c.escalation_id !== active ? `<span class="count">${c.unread}</span>` : ''}</div>`).join('');
}

function confirmSignOut() {
  confirmDialog('Sign out', 'You will need to sign in again to continue.', 'Sign out')
    .then(ok => { if (ok) signOut(ROLE_LOGIN[ROLE]); });
}

/* ---------- help & support (clinician, specialist) ---------- */
const helpRow = (kind, ico, title, sub) => `<button type="button" class="help-row" onclick="openHelp('${kind}')">
  <span class="help-ico">${icon(ico, 17)}</span><span class="help-txt"><b>${title}</b><span class="small muted">${sub}</span></span>${icon('arrow', 16)}</button>`;
const faqItem = (q, a) => `<details class="faq"><summary>${q}</summary><div>${a}</div></details>`;

const FAQ_COMMON = [
  ['What do the four results mean?', `<ul>
    <li><b>Cleared</b>: the model is confident it found no pneumonia at its operating point. This is not a clinical clearance.</li>
    <li><b>Flagged urgent</b>: the model is confident it found pneumonia. Prioritise clinical assessment.</li>
    <li><b>Escalated</b>: the model is not confident either way, so the case should be reviewed by a specialist.</li>
    <li><b>Rejected</b>: the image was not assessed because it does not look like a usable paediatric chest X-ray (wrong image type, poor quality or unusual view).</li></ul>`],
  ['What does the heatmap show?', 'The Grad-CAM heatmap highlights the image regions that most influenced the model. It is not an outline of a lesion and is not shown for rejected images.'],
  ['Is a PneumoScan result a diagnosis?', `No. ${DISCLAIMER}`],
  ['How do messages work?', 'Each referral has a conversation between the referring clinician and the specialist. Open <b>Messages</b> in the menu. Messages are limited to 2,000 characters.'],
  ['Why was I signed out?', 'You are signed out after 30 minutes without activity, with a warning 2 minutes before. Sessions are kept for this browser tab only, so closing the tab also signs you out.'],
  ['I forgot my password. What do I do?', 'Use <b>Forgot password?</b> on the sign-in page to receive a reset email. If you are signed in, you can change it under <b>Settings → Change password</b>.'],
];
const FAQ_ROLE = {
  clinician: [
    ['Which images can I upload?', 'A frontal paediatric chest X-ray as a JPEG or PNG file, up to 10 MB.'],
    ['How do I refer a case to a specialist?', 'Open the result and choose <b>Refer to a specialist</b>. Only specialists who are currently accepting referrals are listed. Set the priority, add notes, and send. You are notified when the specialist starts the review and when it is returned.'],
    ['How do I record the final diagnosis?', 'On the case, choose <b>Record final diagnosis</b>. This records the confirmed outcome alongside the model\'s result and is used for performance monitoring.'],
    ['How do I get a PDF report?', 'Use the <b>PDF report</b> button on a result. The report includes the triage result, confidence values and the disclaimer.'],
  ],
  specialist: [
    ['How do referrals reach me?', 'Clinicians refer cases to you from their results. New referrals appear in your <b>Review queue</b>, urgent first and then oldest first, and you receive a notification.'],
    ['What happens when I open a referral?', 'Opening a new referral starts the review. The referring clinician is notified that it has been acknowledged.'],
    ['How do I return my review?', 'Fill in <b>Your review</b> on the case (diagnosis, findings and recommendation) and choose <b>Submit review</b>. The clinician is notified, and the case moves to <b>Completed</b>.'],
    ['How do I stop receiving new referrals?', 'Turn off <b>Accepting new referrals</b> under <b>Settings → Availability</b>. Cases already in your queue stay there.'],
  ],
};

const HELP = {
  faq: {title: 'Frequently asked questions', body: () =>
    `<div class="faq-list">${[...(FAQ_ROLE[ROLE] || []), ...FAQ_COMMON].map(([q, a]) => faqItem(q, a)).join('')}</div>`},
  privacy: {title: 'Privacy policy', body: () => `<div class="legal">
    <p class="small muted">PneumoScan research prototype · applies to all users of this application.</p>
    <h4>1. What we store</h4>
    <ul><li><b>Account data</b>: your name, email, role, health facility and availability, managed through Firebase Authentication and Cloud Firestore.</li>
      <li><b>Patient data</b>: the patient details you register, such as name, age, sex and clinical findings.</li>
      <li><b>Scans and results</b>: a downscaled copy of each uploaded X-ray, its heatmap, the model result and confidence values.</li>
      <li><b>Workflow data</b>: referrals, specialist reviews, final diagnoses, messages and notifications.</li>
      <li><b>Audit log</b>: a record of key actions, such as sign-in, uploads, referrals and user changes, with the time and user.</li></ul>
    <h4>2. How it is used</h4>
    <p>Data is used only to run the triage workflow, route referrals, produce reports, and monitor the model's performance for research. It is not sold or used for advertising.</p>
    <h4>3. Who can see it</h4>
    <p>Access is controlled by role. Clinicians see their own patients and cases. Specialists see the cases referred to them. Administrators manage accounts and can view monitoring data and the audit log.</p>
    <h4>4. Storage and security</h4>
    <p>Data is stored in Google Firebase (Cloud Firestore) and sent over encrypted connections. Every request is checked against your signed-in identity and role. Sessions end after 30 minutes of inactivity.</p>
    <h4>5. Your responsibilities</h4>
    <p>Enter only the patient information needed for triage. Use test or de-identified data unless the study's ethics approval permits otherwise.</p>
    <h4>6. Retention and your rights</h4>
    <p>Data is kept for the duration of the research project. To access, correct or delete your account or the data you entered, contact your PneumoScan administrator.</p></div>`},
  terms: {title: 'Terms of service', body: () => `<div class="legal">
    <p class="small muted">PneumoScan research prototype · by signing in you agree to these terms.</p>
    <h4>1. Research use only</h4>
    <p>${DISCLAIMER} PneumoScan is not a certified medical device.</p>
    <h4>2. Clinical responsibility</h4>
    <p>All clinical decisions remain the responsibility of the treating clinician. Results, heatmaps and reports support review; they do not replace clinical examination or specialist judgement.</p>
    <h4>3. Accounts</h4>
    <p>Accounts are for named, authorised users only. Keep your password private, do not share your account, and sign out on shared computers. Roles are assigned by an administrator.</p>
    <h4>4. Acceptable use</h4>
    <ul><li>Upload only paediatric chest X-rays that you are authorised to use.</li>
      <li>Do not try to access data or functions outside your role.</li>
      <li>Do not use the system to interfere with its operation or with other users.</li></ul>
    <h4>5. Availability</h4>
    <p>The prototype is provided as is, without guarantees of availability or accuracy, and may change or be withdrawn during the research project.</p>
    <h4>6. Monitoring</h4>
    <p>Key actions are recorded in an audit log, and final diagnoses and specialist reviews are compared with model results to evaluate performance.</p>
    <h4>7. Contact</h4>
    <p>For questions about these terms, contact your PneumoScan administrator.</p></div>`},
};
function openHelp(kind) {
  const h = HELP[kind];
  if (h) openModal({title: h.title, wide: true, body: h.body(), footer: '<button class="btn" data-close>Close</button>'});
}
