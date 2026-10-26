/* Admin portal: overview, users, model monitoring, referrals, audit log. */
PORTALS.admin = {
  title: 'Administration', icon: 'shield', home: 'overview',
  nav: [{section: 'Oversight'},
    {id: 'overview', label: 'Overview', icon: 'grid'},
    {id: 'monitoring', label: 'Model monitoring', icon: 'activity'},
    {id: 'referrals', label: 'Referrals', icon: 'send'},
    {section: 'Management'},
    {id: 'users', label: 'Users', icon: 'users'},
    {id: 'audit', label: 'Audit log', icon: 'file'}],
  pages: {
    overview: {title: 'Overview', render: adOverview, after: adOverviewCharts},
    monitoring: {title: 'Model monitoring', render: adMonitoring, after: adMonitoringCharts},
    referrals: {title: 'Referrals', render: adReferrals, after: () => adFilterReferrals()},
    users: {title: 'Users', render: adUsers, after: () => adFilterUsers()},
    audit: {title: 'Audit log', render: adAudit, after: () => adFilterAudit()},
  },
};
const STATE_COLOR = c => ({CLEARED: c.ok, FLAGGED_URGENT: c.danger, ESCALATED: c.warn, REJECTED: c.gray});
let adMon = null, adStats = null, adUsersList = [];

async function adOverview() {
  [adStats, adMon, adUsersList] = await Promise.all([apiJSON('/api/admin/stats'), apiJSON('/api/admin/monitoring'),
    apiJSON('/api/admin/users').then(d => d.users)]);
  const by = r => adUsersList.filter(u => u.role === r && u.is_active !== false).length;
  const ref = adMon.referrals, openRefs = Object.values(ref.open_by_priority).reduce((a, b) => a + b, 0);
  const topUsers = Object.entries(adStats.by_user || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const max = Math.max(1, ...topUsers.map(x => x[1]));
  return `${intro('System overview', 'Activity across all clinicians, specialists and the triage model.')}
    <div class="grid g4" style="margin-bottom:18px">
      ${statCard('scan', adStats.total_predictions, 'Radiographs analysed', '', `${adMon.daily.slice(-7).reduce((a, d) => a + d.CLEARED + d.FLAGGED_URGENT + d.ESCALATED + d.REJECTED, 0)} in the last 7 days`)}
      ${statCard('zap', pct(adStats.autonomy_rate), 'Autonomy rate', 'ok', `Validated: ${pct(adMon.expected.autonomy_rate)}`)}
      ${statCard('send', openRefs, 'Open referrals', 'warn', `${ref.open_by_priority.urgent || 0} urgent · median turnaround ${duration(ref.median_turnaround_h)}`)}
      ${statCard('users', adUsersList.filter(u => u.is_active !== false).length, 'Active users', 'info', `${by('clinician')} clinicians · ${by('specialist')} specialists · ${by('admin')} admins`)}
    </div>
    <div class="grid g-2-1" style="margin-bottom:18px">
      ${card('Daily triage volume', '<div class="chart-box"><canvas id="chDaily"></canvas></div>', {sub: 'Last 14 days by decision'})}
      ${card('Decision mix', '<div class="chart-box"><canvas id="chMix"></canvas></div>', {sub: 'All time'})}
    </div>
    <div class="grid g-1-1">
      ${card('Most active clinicians', topUsers.length ? `<div class="bar-list">${topUsers.map(([n, v]) => `<div><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n)}</span><div class="bar"><i style="width:${v / max * 100}%"></i></div><b>${v}</b></div>`).join('')}</div>` : '<p class="muted small">No scans yet.</p>')}
      ${card('Specialist agreement', adMon.agreement.confirmed ? `<div class="row" style="gap:20px"><div class="stat" style="padding:0"><div class="v">${pct(adMon.agreement.rate)}</div></div>
          <p class="muted small">of ${adMon.agreement.confirmed} confirmed case(s), the model's leaning matched the confirmed diagnosis.</p></div>
          <a class="btn sm secondary" style="margin-top:14px" href="#/monitoring">Open monitoring ${icon('arrow', 14)}</a>`
        : emptyState('target', 'No confirmed outcomes yet', 'Agreement appears once specialists or clinicians confirm diagnoses.'))}
    </div>
    <div style="margin-top:18px">${card('Send an announcement', `<form class="stack" onsubmit="adAnnounce(event)">
        <div class="grid g-2-1" style="gap:12px"><div class="field"><label>Title</label><input class="input" id="anTitle" maxlength="80" placeholder="e.g. Scheduled maintenance tonight"></div>
          <div class="field"><label>Audience</label><select class="input" id="anAud"><option value="all">Everyone</option><option value="clinician">Clinicians</option><option value="specialist">Specialists</option><option value="admin">Administrators</option></select></div></div>
        <div class="field"><label>Message</label><textarea class="input" id="anBody" maxlength="500" placeholder="Appears in each recipient's notifications."></textarea></div>
        <div class="row"><span class="small muted">Sent as an in-app notification to active accounts.</span><span class="spacer"></span><button class="btn" id="anBtn">${icon('megaphone', 16)}Send announcement</button></div></form>`)}</div>`;
}
async function adAnnounce(e) {
  e.preventDefault();
  const body = {title: $('anTitle').value.trim(), body: $('anBody').value.trim(), audience: $('anAud').value};
  if (!body.title || !body.body) return toast('Add a title and a message', 'error');
  await withBusy($('anBtn'), 'Sending…', async () => {
    try { const r = await apiJSON('/api/admin/announce', {method: 'POST', json: body}); e.target.reset(); toast(`Announcement sent to ${r.sent} user(s)`, 'success'); }
    catch (err) { toast(err.message, 'error'); }
  });
}
function adOverviewCharts() {
  const c = chartColors(), sc = STATE_COLOR(c);
  makeChart('chDaily', {type: 'bar', data: {labels: adMon.daily.map(d => d.date.slice(5)),
    datasets: Object.keys(STATE).map(s => ({label: STATE[s].label, data: adMon.daily.map(d => d[s]), backgroundColor: sc[s], borderRadius: 4, stack: 'a'}))},
    options: {maintainAspectRatio: false, plugins: {legend: {position: 'bottom', labels: {boxWidth: 10}}}, scales: {x: {grid: {display: false}}, y: {beginAtZero: true, ticks: {precision: 0}}}}});
  const st = adStats.by_state || {};
  makeChart('chMix', {type: 'doughnut', data: {labels: Object.keys(STATE).map(s => STATE[s].label),
    datasets: [{data: Object.keys(STATE).map(s => st[s] || 0), backgroundColor: Object.keys(STATE).map(s => sc[s]), borderWidth: 0}]},
    options: {maintainAspectRatio: false, cutout: '68%', plugins: {legend: {position: 'bottom', labels: {boxWidth: 10}}}}});
}

/* ---------- monitoring ---------- */
let adCard = null;
async function adMonitoring() {
  [adMon, adCard] = await Promise.all([apiJSON('/api/admin/monitoring'), apiJSON('/api/model-card')]);
  const L = adMon.live, E = adMon.expected, m = adMon.agreement.matrix;
  const cmp = (label, live, exp, goodHigh, ico) => {
    const diff = live == null || exp == null ? null : live - exp;
    const bad = diff != null && Math.abs(diff) > .1 && (goodHigh ? diff < 0 : diff > 0);
    return statCard(ico, pct(live), label, bad ? 'danger' : 'ok',
      `Validated ${pct(exp)}${diff == null ? '' : ` · <b style="color:var(--${bad ? 'danger' : 'ok'})">${diff >= 0 ? '+' : ''}${(diff * 100).toFixed(1)} pts</b>`}`);
  };
  const tr = adCard.test_results || {}, U = tr.ungated_all_images || {}, G = tr.gated_accepted_only || {};
  const metric = k => `<tr><td>${{sensitivity: 'Sensitivity', specificity: 'Specificity', precision: 'Precision', f1: 'F1 score', accuracy: 'Accuracy', auc: 'ROC AUC'}[k]}</td>
    <td class="mono">${U[k] != null ? U[k].toFixed(3) : '—'}</td><td class="mono"><b>${G[k] != null ? G[k].toFixed(3) : '—'}</b></td></tr>`;
  const g = adCard.gate || {};
  const aucs = Object.entries(g.val_auc_by_model || {}).sort((a, b) => b[1] - a[1]);
  return `${intro('Model monitoring', `Live behaviour of ${esc(adCard.architecture || 'the model')} across ${adMon.total} analysed radiograph(s), compared with its held-out test results. A drift of more than 10 points is flagged.`)}
    <div class="grid g3" style="margin-bottom:18px">
      ${cmp('Autonomy rate', L.autonomy_rate, E.autonomy_rate, true, 'zap')}
      ${cmp('Escalation rate', L.escalation_rate, E.escalation_rate, false, 'question')}
      ${cmp('OOD rejection rate', L.rejection_rate, E.rejection_rate, false, 'ban')}
    </div>
    <div class="grid g-1-1" style="margin-bottom:18px">
      ${card('Calibrated probability distribution', '<div class="chart-box"><canvas id="chHist"></canvas></div>', {sub: 'P(pneumonia) for classified scans. Mass near 0.5 means more escalations.'})}
      ${card('AI vs confirmed diagnosis', adMon.agreement.confirmed ? `<div class="cm"><div></div><div class="h">Confirmed pneumonia</div><div class="h">Confirmed normal</div>
          <div class="h" style="text-align:right">AI pneumonia</div><div class="c good">${m.pneumonia.pneumonia}</div><div class="c bad">${m.pneumonia.normal}</div>
          <div class="h" style="text-align:right">AI normal</div><div class="c bad">${m.normal.pneumonia}</div><div class="c good">${m.normal.normal}</div></div>
          <div class="row" style="margin-top:16px"><span class="muted small">Agreement</span><span class="spacer"></span><b>${pct(adMon.agreement.rate)}</b></div>
          <p class="small muted" style="margin-top:6px">Escalated cases count with the model's leaning. Missed pneumonia (bottom-left) is the most important error.</p>`
        : emptyState('target', 'No confirmed outcomes yet', 'Confirmed diagnoses from specialists and clinicians build this matrix.'), {sub: `${adMon.agreement.confirmed} confirmed case(s)`})}
    </div>
    <div class="grid g-1-1" style="margin-bottom:18px">
      ${card('Specialist referrals', `<div class="kv"><div><div class="k">Open · urgent</div><div class="v">${adMon.referrals.open_by_priority.urgent || 0}</div></div>
          <div><div class="k">Open · routine</div><div class="v">${adMon.referrals.open_by_priority.routine || 0}</div></div>
          <div><div class="k">Resolved</div><div class="v">${adMon.referrals.resolved}</div></div>
          <div><div class="k">Median turnaround</div><div class="v">${duration(adMon.referrals.median_turnaround_h)}</div></div>
          <div><div class="k">Mean turnaround</div><div class="v">${duration(adMon.referrals.mean_turnaround_h)}</div></div></div>`)}
      ${card('Safety gate', `<div class="kv"><div><div class="k">Temperature</div><div class="v">${g.temperature != null ? g.temperature.toFixed(3) : '—'}</div></div>
          <div><div class="k">Pneumonia if P ≥</div><div class="v">${(g.pneumonia_threshold ?? g.confidence_threshold)?.toFixed(4) ?? '—'}</div></div>
          <div><div class="k">Normal if P ≤</div><div class="v">${(g.normal_threshold ?? (1 - g.confidence_threshold))?.toFixed(4) ?? '—'}</div></div>
          <div><div class="k">OOD distance limit</div><div class="v">${g.distance_threshold != null ? g.distance_threshold.toFixed(0) : '—'}</div></div>
          <div><div class="k">ECE after scaling</div><div class="v">${g.ece_after != null ? g.ece_after.toFixed(4) : '—'}</div></div></div>
          <p class="small muted" style="margin-top:12px">${esc(g.note || '')}</p>`)}
    </div>
    <div class="grid g-2-1">
      ${card('Model card · held-out test set', `<div class="table-wrap"><table class="t"><thead><tr><th>Metric</th><th>All images (n=${U.n ?? '—'})</th><th>Accepted by gate (n=${G.n ?? '—'})</th></tr></thead>
          <tbody>${['sensitivity', 'specificity', 'precision', 'f1', 'accuracy', 'auc'].map(metric).join('')}</tbody></table></div>
          <div class="card-b small muted">Of ${U.n ?? '—'} test images, ${tr.accepted ?? '—'} were decided autonomously, ${tr.escalated_low_confidence ?? '—'} escalated for low confidence and ${tr.rejected_out_of_distribution ?? '—'} rejected as out-of-distribution.
          Confusion on accepted cases: TP ${G.tp ?? '—'}, TN ${G.tn ?? '—'}, FP ${G.fp ?? '—'}, FN ${G.fn ?? '—'}.</div>`, {flush: true})}
      ${card('Validation AUC by architecture', `<div class="bar-list">${aucs.map(([k, v]) => `<div><span>${esc(k)}${k === g.selected_model ? ' ★' : ''}</span><div class="bar"><i style="width:${Math.max(0, (v - .95) / .05 * 100)}%"></i></div><b>${v.toFixed(3)}</b></div>`).join('')}</div>
          <p class="small muted" style="margin-top:10px">Bars are scaled from 0.95 to 1.00. ★ deployed model.</p>`)}
    </div>`;
}
function adMonitoringCharts() {
  const c = chartColors();
  makeChart('chHist', {type: 'bar', data: {labels: adMon.probability_histogram.map((_, i) => `${(i / 10).toFixed(1)}–${((i + 1) / 10).toFixed(1)}`),
    datasets: [{label: 'Scans', data: adMon.probability_histogram, borderRadius: 5,
      backgroundColor: adMon.probability_histogram.map((_, i) => i < 3 ? c.ok : i > 6 ? c.danger : c.warn)}]},
    options: {maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {grid: {display: false}}, y: {beginAtZero: true, ticks: {precision: 0}}}}});
}

/* ---------- referrals ---------- */
let adRefs = [], adRefStatus = '';
async function adReferrals() {
  adRefs = (await apiJSON('/api/escalations?images=false')).escalations;
  const seg = (v, l) => `<button class="${adRefStatus === v ? 'on' : ''}" onclick="adRefStatus='${v}';this.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('on'));this.classList.add('on');adFilterReferrals()">${l}</button>`;
  return `${intro('All referrals', 'Every case sent to a specialist, with waiting and turnaround times.')}
    <div class="card"><div class="card-h"><div class="seg">${seg('', 'All')}${seg('sent', 'Awaiting')}${seg('pending', 'In review')}${seg('resolved', 'Resolved')}</div></div><div id="adRefTable"></div></div>`;
}
function adFilterReferrals() {
  const rows = adRefs.filter(r => !adRefStatus || r.status === adRefStatus);
  $('adRefTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Patient</th><th>Clinician</th><th>Specialist</th><th>Priority</th><th>Status</th><th>Wait / turnaround</th><th>Diagnosis</th><th>AI</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><b>${esc(r.patient_name || 'Unknown')}</b><div class="small muted">${fmtDate(r.created_at)}</div></td><td>${esc(r.clinician_name || '—')}</td><td>${esc(r.specialist_name || '—')}</td>
      <td>${priorityBadge(r.priority)}</td><td>${escBadge(r.status)}</td><td>${r.status === 'resolved' ? duration((new Date(r.resolved_at) - new Date(r.created_at)) / 36e5) : waitLabel(r.created_at)}</td>
      <td>${outcomeBadge(r.confirmed_outcome)}</td><td>${r.ai_agrees == null ? '<span class="muted">—</span>' : r.ai_agrees ? '<span class="badge b-ok">Agrees</span>' : '<span class="badge b-danger">Disagrees</span>'}</td></tr>`).join('')}</tbody></table></div>`
    : emptyState('send', 'No referrals', '');
}

/* ---------- users ---------- */
async function adUsers() {
  adUsersList = (await apiJSON('/api/admin/users')).users.sort((a, b) => (a.full_name || '').localeCompare(b.full_name || ''));
  return `${intro('Users', 'Create accounts, assign roles, and activate or deactivate access. Deactivated users cannot sign in.',
      `<button class="btn" onclick="adNewUser()">${icon('plus')}Add user</button>`)}
    <div class="card"><div class="card-h row wrap">${searchBox('usSearch', 'Search name or email…', 'adFilterUsers()')}<span class="spacer"></span>
      <select class="input" id="usRole" style="width:170px" onchange="adFilterUsers()"><option value="">All roles</option><option value="clinician">Clinicians</option><option value="specialist">Specialists</option><option value="admin">Admins</option></select></div>
      <div id="usTable"></div></div>`;
}
function adFilterUsers() {
  const q = ($('usSearch')?.value || '').toLowerCase(), r = $('usRole')?.value;
  const rows = adUsersList.filter(u => (!r || u.role === r) && (!q || (u.full_name || '').toLowerCase().includes(q) || (u.email || '').toLowerCase().includes(q)));
  $('usTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>User</th><th>Role</th><th>Facility</th><th>Active</th><th>Takes referrals</th><th>Joined</th></tr></thead><tbody>
    ${rows.map(u => { const self = u.user_id === ME.user_id; return `<tr><td><div class="row"><div class="avatar">${initials(u.full_name)}</div><div><b>${esc(u.full_name || '—')}</b>${self ? ' <span class="badge b-accent plain">You</span>' : ''}<div class="small muted">${esc(u.email)}</div></div></div></td>
      <td><select class="input" style="height:34px;width:140px" ${self ? 'disabled' : ''} onchange="adUpdateUser('${esc(u.user_id)}',{role:this.value})">${['clinician', 'specialist', 'admin'].map(x => `<option value="${x}" ${u.role === x ? 'selected' : ''}>${ROLE_NAME[x]}</option>`).join('')}</select></td>
      <td class="small">${esc(u.hospital || '—')}</td>
      <td><label class="switch"><input type="checkbox" ${u.is_active !== false ? 'checked' : ''} ${self ? 'disabled' : ''} onchange="adUpdateUser('${esc(u.user_id)}',{is_active:this.checked})"><span></span></label></td>
      <td>${u.role === 'specialist' ? `<label class="switch"><input type="checkbox" ${u.available !== false ? 'checked' : ''} onchange="adUpdateUser('${esc(u.user_id)}',{available:this.checked})"><span></span></label>` : '<span class="muted">—</span>'}</td>
      <td class="small muted">${fmtDate(u.created_at, false)}</td></tr>`; }).join('')}</tbody></table></div>`
    : emptyState('users', 'No users match', '');
}
async function adUpdateUser(uid, change) {
  try {
    await apiJSON(`/api/admin/users/${encodeURIComponent(uid)}`, {method: 'PATCH', json: change});
    Object.assign(adUsersList.find(u => u.user_id === uid) || {}, change);
    toast('User updated', 'success');
  } catch (e) { toast(e.message, 'error'); }
  adFilterUsers();
}
function adNewUser() {
  const m = openModal({title: 'Add user', body: `
    <div class="field"><label>Full name *</label><input class="input" id="nuName" placeholder="Dr. Jane Uwase"></div>
    <div class="field"><label>Email *</label><input class="input" id="nuEmail" type="email" placeholder="name@hospital.rw"></div>
    <div class="grid g2" style="gap:12px"><div class="field"><label>Role</label><select class="input" id="nuRole"><option value="clinician">Clinician</option><option value="specialist">Specialist</option><option value="admin">Administrator</option></select></div>
      <div class="field"><label>Facility</label><input class="input" id="nuHosp"></div></div>
    <div class="field"><label>Temporary password *</label><div class="input-wrap"><input class="input" id="nuPass" type="text" minlength="8">
      <button type="button" class="reveal" onclick="$('nuPass').value=Array.from(crypto.getRandomValues(new Uint8Array(9)),b=>'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'[b%57]).join('')">Generate</button></div>
      <div class="small muted">Share it securely; the user should change it under Settings.</div></div>`,
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="nuSave">Create account</button>`});
  m.q('#nuSave').onclick = () => withBusy(m.q('#nuSave'), 'Creating…', async () => {
    const body = {full_name: m.q('#nuName').value.trim(), email: m.q('#nuEmail').value.trim(), role: m.q('#nuRole').value,
                  hospital: m.q('#nuHosp').value.trim() || null, password: m.q('#nuPass').value};
    if (!body.full_name || !body.email) return toast('Name and email are required', 'error');
    if (body.password.length < 8) return toast('Password must be at least 8 characters', 'error');
    try { await apiJSON('/api/admin/users', {method: 'POST', json: body}); m.close(); toast(`${ROLE_NAME[body.role]} account created`, 'success'); route(); }
    catch (e) { toast(e.message, 'error'); }
  });
}

/* ---------- audit ---------- */
let adLogs = [];
const actionType = a => (a || '').split(':')[0];
async function adAudit() {
  adLogs = (await apiJSON('/api/admin/audit?limit=1000')).logs;
  const types = [...new Set(adLogs.map(l => actionType(l.action)))].sort();
  return `${intro('Audit log', `The last ${adLogs.length} recorded actions: scans, referrals, reviews, outcomes and account changes.`,
      `<button class="btn secondary" onclick="adExportAudit()">${icon('download', 16)}Export CSV</button>`)}
    <div class="card"><div class="card-h row wrap">${searchBox('auSearch', 'Search user, action or case ID…', 'adFilterAudit()')}<span class="spacer"></span>
      <select class="input" id="auType" style="width:220px" onchange="adFilterAudit()"><option value="">All actions</option>${types.map(t => `<option>${esc(t)}</option>`).join('')}</select></div>
      <div id="auTable"></div></div>`;
}
function adAuditRows() {
  const q = ($('auSearch')?.value || '').toLowerCase(), t = $('auType')?.value;
  return adLogs.filter(l => (!t || actionType(l.action) === t) &&
    (!q || [l.action, l.full_name, l.email, l.prediction_id].some(v => (v || '').toLowerCase().includes(q))));
}
function adFilterAudit() {
  const rows = adAuditRows();
  $('auTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Case</th></tr></thead><tbody>
    ${rows.slice(0, 400).map(l => `<tr><td class="small" style="white-space:nowrap">${fmtDate(l.timestamp)}</td><td>${esc(l.full_name || 'System')}<div class="small muted">${esc(l.email || '')}</div></td>
      <td><span class="badge b-accent plain">${esc(actionType(l.action))}</span> <span class="small">${esc((l.action || '').split(':').slice(1).join(':'))}</span></td>
      <td class="mono">${esc(l.prediction_id || '—')}</td></tr>`).join('')}</tbody></table></div>
    ${rows.length > 400 ? `<div class="card-b small muted">Showing 400 of ${rows.length}. Export CSV for the full list.</div>` : ''}`
    : emptyState('file', 'No matching entries', '');
}
function adExportAudit() {
  downloadCSV('pneumoscan_audit_log.csv', ['timestamp', 'user', 'email', 'action', 'case_id'],
    adAuditRows().map(l => [l.timestamp, l.full_name, l.email, l.action, l.prediction_id]));
}
