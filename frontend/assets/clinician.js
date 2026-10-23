/* Clinician portal: scan, patients, history, referrals. */
PORTALS.clinician = {
  title: 'Clinician portal', icon: 'clipboard', home: 'dashboard',
  nav: [{section: 'Workspace'},
    {id: 'dashboard', label: 'Dashboard', icon: 'grid'},
    {id: 'scan', label: 'New scan', icon: 'scan'},
    {id: 'patients', label: 'Patients', icon: 'users'},
    {id: 'history', label: 'Scan history', icon: 'clock'},
    {id: 'referrals', label: 'Referrals', icon: 'send'}],
  pages: {
    dashboard: {title: 'Dashboard', render: clDashboard},
    scan: {title: 'New scan', crumb: 'Upload a radiograph for triage', render: clScan, after: clScanAfter},
    patients: {title: 'Patients', render: clPatients, after: () => clFilterPatients()},
    patient: {title: 'Patient record', nav: 'patients', render: clPatient},
    history: {title: 'Scan history', render: clHistory, after: () => clFilterHistory()},
    referrals: {title: 'Referrals', render: clReferrals, after: () => clFilterReferrals()},
    case: {title: 'Case details', nav: 'history', render: clCase},
  },
};

/* ---------- dashboard ---------- */
async function clDashboard() {
  const [c, e] = await Promise.all([apiJSON('/api/cases?mine=true&limit=500'), apiJSON('/api/escalations?images=false')]);
  const cases = c.cases, escs = e.escalations;
  const today = new Date().toDateString();
  const n = s => cases.filter(x => x.triage_state === s).length;
  const open = escs.filter(x => x.status !== 'resolved');
  const resolved = escs.filter(x => x.status === 'resolved').sort((a, b) => (b.resolved_at || '').localeCompare(a.resolved_at || ''));
  const decided = n('CLEARED') + n('FLAGGED_URGENT');
  setCount('referrals', open.length);
  const greeting = new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening';
  return `${intro(`${greeting}, ${esc((ME.full_name || '').split(' ').slice(0, 2).join(' '))}`, 'Here is your triage activity at a glance.',
      `<div class="row"><a class="btn secondary" href="#/patients">${icon('users')}Patients</a><a class="btn" href="#/scan">${icon('scan')}New scan</a></div>`)}
    <div class="grid g4" style="margin-bottom:18px">
      ${statCard('scan', cases.filter(x => new Date(x.created_at).toDateString() === today).length, 'Scans today', '', `${cases.length} in total`)}
      ${statCard('alert', n('FLAGGED_URGENT'), 'Flagged urgent', 'danger', 'Pneumonia with high confidence')}
      ${statCard('send', open.length, 'Awaiting specialist', 'warn', `${resolved.length} reviewed`)}
      ${statCard('zap', cases.length ? pct(decided / cases.length, 0) : '—', 'Decided autonomously', 'ok', 'No referral needed')}
    </div>
    <div class="grid g-2-1">
      ${card('Recent scans', cases.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Patient</th><th>Result</th><th>P(pneumonia)</th><th>When</th></tr></thead><tbody>
        ${cases.slice(0, 8).map(x => `<tr class="click" onclick="go('#/case/${esc(x.prediction_id)}')"><td><b>${esc(x.patient_name || 'Unknown')}</b><div class="small muted">${esc(x.filename || '')}</div></td>
          <td>${stateBadge(x.triage_state)}</td><td class="mono">${x.calibrated_prob != null ? x.calibrated_prob.toFixed(3) : '—'}</td><td class="muted small">${ago(x.created_at)}</td></tr>`).join('')}
        </tbody></table></div>` : emptyState('scan', 'No scans yet', 'Upload your first radiograph to get a triage decision.', `<a class="btn" href="#/scan">${icon('scan')}Start a scan</a>`),
        {flush: true, action: `<a class="btn sm ghost" href="#/history">View all ${icon('arrow', 14)}</a>`})}
      <div class="stack">
        ${card('Specialist responses', resolved.length ? resolved.slice(0, 5).map(r => `
          <div class="row" style="padding:8px 0;cursor:pointer" onclick="go('#/case/${esc(r.prediction_id)}')"><div class="avatar">${initials(r.specialist_name)}</div>
            <div style="flex:1;min-width:0"><b>${esc(r.patient_name || 'Unknown')}</b><div class="small muted">${esc(r.specialist_name || '')} · ${ago(r.resolved_at)}</div></div>${outcomeBadge(r.confirmed_outcome)}</div>`).join('')
          : emptyState('inbox', 'No responses yet', 'Reviewed referrals will appear here.'))}
        ${card('Your triage mix', `<div class="bar-list">${Object.keys(STATE).map(s => `<div><span>${STATE[s].label}</span>
          <div class="bar"><i style="width:${cases.length ? n(s) / cases.length * 100 : 0}%;background:var(--${{ok: 'ok', danger: 'danger', warn: 'warn', gray: 'muted'}[STATE[s].tone]})"></i></div><b>${n(s)}</b></div>`).join('')}</div>`)}
      </div>
    </div>`;
}

/* ---------- new scan ---------- */
let clFile = null, clPatientsCache = [];
async function clScan(preselect) {
  clPatientsCache = (await apiJSON('/api/patients')).patients;
  clFile = null;
  return `${intro('New scan', 'Select the patient, upload a frontal paediatric chest radiograph and run the triage model.')}
  <div class="grid g-1-1">
    <div class="card"><div class="card-h"><span class="step-n">1</span><h3>Patient</h3><span class="spacer"></span>
        <button class="btn sm secondary" onclick="clNewPatient(p => { clPatientsCache.unshift(p); go('#/scan/' + p.patient_id); })">${icon('plus', 15)}New patient</button></div>
      <div class="card-b"><select class="input" id="scanPatient"><option value="">Select a patient…</option>
        ${clPatientsCache.map(p => `<option value="${esc(p.patient_id)}" ${p.patient_id === preselect ? 'selected' : ''}>${esc(p.full_name)} · ${esc(p.patient_id)}${p.age != null ? ` · ${esc(p.age)} y` : ''}</option>`).join('')}</select>
        ${clPatientsCache.length ? '' : '<p class="small muted" style="margin-top:8px">No patients yet. Create one to continue.</p>'}</div>
      <div class="card-h" style="border-top:1px solid var(--border)"><span class="step-n">2</span><h3>Radiograph</h3></div>
      <div class="card-b"><label class="drop" id="drop"><input type="file" id="scanFile" accept="image/png,image/jpeg" hidden>
          <div id="dropInner"><div class="ico">${icon('upload', 24)}</div><b>Drop an X-ray here or click to browse</b><div class="small muted">PNG or JPEG, up to 10 MB</div></div></label>
        <div class="row" style="margin-top:16px"><button class="btn secondary hidden" id="scanClear" onclick="go('#/scan/' + ($('scanPatient').value || ''))">Clear</button><span class="spacer"></span>
          <button class="btn lg" id="scanRun" disabled>${icon('activity')}Analyse radiograph</button></div>
        <div class="disclaimer" style="margin-top:16px">${icon('shield', 16)}<span>Cases below the confidence gate or unlike the training data are never auto-decided; you will be asked to refer them.</span></div></div>
    </div>
    <div class="card"><div class="card-h"><span class="step-n">3</span><h3>Triage result</h3></div>
      <div class="card-b" id="scanResult">${emptyState('image', 'Awaiting radiograph', 'The decision, heatmap and confidence will appear here.')}</div></div>
  </div>`;
}
function clScanAfter() {
  const drop = $('drop'), input = $('scanFile');
  const pick = f => {
    if (!/^image\/(png|jpeg)$/.test(f.type)) return toast('Please choose a PNG or JPEG image', 'error');
    clFile = f;
    $('dropInner').innerHTML = `<img src="${URL.createObjectURL(f)}" alt=""><div class="small muted" style="margin-top:8px">${esc(f.name)} · ${(f.size / 1024).toFixed(0)} KB</div>`;
    $('scanRun').disabled = false; $('scanClear').classList.remove('hidden');
  };
  ['dragover', 'dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.toggle('over', ev === 'dragover');
    if (ev === 'drop' && e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);
  }));
  input.onchange = () => input.files[0] && pick(input.files[0]);
  $('scanRun').onclick = async () => {
    const pid = $('scanPatient').value;
    if (!pid) return toast('Select or create a patient first', 'error');
    if (!clFile) return;
    const body = new FormData(); body.append('file', clFile);
    $('scanResult').innerHTML = `<div class="loading"><span class="spin" style="width:26px;height:26px"></span><p class="muted" style="margin-top:10px">Running model and safety gate…</p></div>`;
    await withBusy($('scanRun'), 'Analysing…', async () => {
      try {
        const d = await apiJSON(`/api/predict?patient_id=${encodeURIComponent(pid)}`, {method: 'POST', body});
        $('scanResult').innerHTML = clResult({prediction_id: d.prediction_id, triage_state: d.state, calibrated_prob: d.probability,
          ood_score: d.ood_score, reason: d.reason, heatmap_png: d.heatmap_png, model_version: d.model_version, gate: d.thresholds.confidence,
          demo: d.demo_mode}, URL.createObjectURL(clFile));
      } catch (e) {
        $('scanResult').innerHTML = emptyState('alert', 'Analysis failed', esc(e.message));
      }
    });
  };
}
const GATE = () => clGate;
let clGate = null;
function clResult(d, original, escalations = []) {
  const s = STATE[d.triage_state] || STATE.REJECTED;
  if (d.gate) clGate = d.gate;
  const needsReferral = d.triage_state === 'ESCALATED' || d.triage_state === 'REJECTED';
  const referred = escalations.length > 0;
  return `<div class="stack">
    <div class="verdict v-${esc(d.triage_state)}"><div class="ico">${icon(s.icon, 22)}</div><div><h3>${s.label}${d.triage_state === 'FLAGGED_URGENT' ? ' · pneumonia' : d.triage_state === 'CLEARED' ? ' · normal' : ''}</h3>
      <p>${esc(d.reason || s.desc)}</p></div></div>
    ${d.demo ? '<div class="form-error">Demo mode: model weights not loaded. Do not use this result.</div>' : ''}
    ${viewer(original, d.heatmap_png, 'vw-' + d.prediction_id)}
    <div class="kv"><div><div class="k">P(pneumonia)</div><div class="v">${d.calibrated_prob != null ? d.calibrated_prob.toFixed(3) : '—'}</div></div>
      <div><div class="k">Model leaning</div><div class="v">${d.calibrated_prob == null ? '—' : d.calibrated_prob >= .5 ? 'Pneumonia' : 'Normal'}</div></div>
      <div><div class="k">OOD distance</div><div class="v">${d.ood_score != null ? Number(d.ood_score).toFixed(0) : '—'}</div></div>
      <div><div class="k">Model</div><div class="v" style="font-size:13px">${esc(d.model_version || '—')}</div></div></div>
    ${d.calibrated_prob != null && clGate ? confidenceMeter(confOf(d.calibrated_prob), clGate) : ''}
    <div><div class="label" style="margin-bottom:8px">Confirmed clinical outcome ${d.confirmed_outcome ? outcomeBadge(d.confirmed_outcome) : ''}</div>
      <div class="row wrap">${['pneumonia', 'normal', 'unknown'].map(o => `<button class="btn sm secondary" onclick="clFeedback('${esc(d.prediction_id)}','${o}',this)">${{pneumonia: 'Pneumonia', normal: 'Normal', unknown: 'Not yet known'}[o]}</button>`).join('')}</div></div>
    <div class="row wrap" style="padding-top:6px;border-top:1px solid var(--border)">
      ${referred ? `<span class="badge b-info">Referred to ${esc(escalations[0].specialist_name || 'specialist')}</span>` : ''}
      <span class="spacer"></span>
      <button class="btn secondary" onclick="downloadReport('${esc(d.prediction_id)}')">${icon('download', 16)}PDF report</button>
      ${referred ? '' : `<button class="btn ${needsReferral ? 'danger' : 'secondary'}" onclick="clRefer('${esc(d.prediction_id)}', ${d.triage_state === 'FLAGGED_URGENT' || d.triage_state === 'REJECTED'})">${icon('send', 16)}${needsReferral ? 'Refer to specialist' : 'Request second opinion'}</button>`}
    </div></div>`;
}
async function clFeedback(pid, outcome, btn) {
  try {
    await apiJSON('/api/feedback', {method: 'POST', json: {prediction_id: pid, confirmed_outcome: outcome}});
    btn.parentElement.querySelectorAll('button').forEach(b => b.classList.toggle('secondary', b !== btn));
    toast('Outcome recorded', 'success');
  } catch (e) { toast(e.message, 'error'); }
}
async function clRefer(pid, urgent) {
  let specs = [];
  try { specs = (await apiJSON('/api/specialists')).specialists; } catch (e) { return toast(e.message, 'error'); }
  const avail = specs.filter(s => s.available);
  const m = openModal({title: 'Refer to a specialist', body: avail.length ? `
      <div class="field"><label>Specialist</label><select class="input" id="rfSpec">${avail.map(s => `<option value="${esc(s.user_id)}">${esc(s.full_name)}${s.hospital ? ' · ' + esc(s.hospital) : ''}</option>`).join('')}</select>
        ${specs.length > avail.length ? `<div class="small muted">${specs.length - avail.length} specialist(s) currently unavailable.</div>` : ''}</div>
      <div class="field"><label>Priority</label><div class="choice" style="grid-template-columns:1fr 1fr">
        <input type="radio" name="rfPri" id="rfR" value="routine" ${urgent ? '' : 'checked'}><label for="rfR">Routine</label>
        <input type="radio" name="rfPri" id="rfU" value="urgent" ${urgent ? 'checked' : ''}><label for="rfU">Urgent</label></div></div>
      <div class="field"><label>Clinical note for the specialist</label><textarea class="input" id="rfNote" placeholder="Symptoms, vitals, history, what you need answered…"></textarea></div>`
      : emptyState('users', 'No specialist available', 'All specialists are currently unavailable. Contact your administrator.'),
    footer: avail.length ? `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="rfSend">${icon('send', 16)}Send referral</button>` : ''});
  if (!avail.length) return;
  m.q('#rfSend').onclick = () => withBusy(m.q('#rfSend'), 'Sending…', async () => {
    try {
      await apiJSON('/api/escalations', {method: 'POST', json: {prediction_id: pid, specialist_id: m.q('#rfSpec').value,
        note: m.q('#rfNote').value.trim(), priority: m.q('input[name=rfPri]:checked').value}});
      m.close(); toast('Referral sent', 'success'); go('#/case/' + pid);
    } catch (e) { toast(e.message, 'error'); }
  });
}

/* ---------- patients ---------- */
function clPatientForm(p = {}) {
  return `<div class="field"><label>Full name *</label><input class="input" id="ptName" value="${esc(p.full_name || '')}" required></div>
    <div class="grid g2" style="gap:12px"><div class="field"><label>Age (years)</label><input class="input" id="ptAge" type="number" min="0" max="18" value="${esc(p.age ?? '')}"></div>
      <div class="field"><label>Sex</label><select class="input" id="ptSex"><option value="">—</option>${['F', 'M'].map(g => `<option ${p.gender === g ? 'selected' : ''} value="${g}">${g === 'F' ? 'Female' : 'Male'}</option>`).join('')}</select></div></div>
    <div class="grid g2" style="gap:12px"><div class="field"><label>Parent / guardian</label><input class="input" id="ptGuard" value="${esc(p.guardian_name || '')}"></div>
      <div class="field"><label>Contact phone</label><input class="input" id="ptContact" value="${esc(p.contact || '')}"></div></div>
    <div class="field"><label>Clinical notes</label><textarea class="input" id="ptNotes" placeholder="Presenting complaint, vitals…">${esc(p.notes || '')}</textarea></div>`;
}
function clReadPatient(m) {
  const age = m.q('#ptAge').value;
  return {full_name: m.q('#ptName').value.trim(), age: age === '' ? null : Number(age), gender: m.q('#ptSex').value || null,
          guardian_name: m.q('#ptGuard').value.trim() || null, contact: m.q('#ptContact').value.trim() || null, notes: m.q('#ptNotes').value.trim()};
}
function clNewPatient(done) {
  const m = openModal({title: 'Register patient', body: clPatientForm(),
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="ptSave">Register patient</button>`});
  m.q('#ptSave').onclick = () => withBusy(m.q('#ptSave'), 'Saving…', async () => {
    const body = clReadPatient(m);
    if (!body.full_name) return toast('Patient name is required', 'error');
    try {
      const r = await apiJSON('/api/patients', {method: 'POST', json: body});
      m.close(); toast(`Patient ${r.patient_id} registered`, 'success');
      done({...body, patient_id: r.patient_id, scan_count: 0});
    } catch (e) { toast(e.message, 'error'); }
  });
}
async function clPatients() {
  clPatientsCache = (await apiJSON('/api/patients')).patients;
  return `${intro('Patients', `${clPatientsCache.length} registered patient${clPatientsCache.length === 1 ? '' : 's'}.`,
      `<button class="btn" onclick="clNewPatient(p => go('#/patient/' + p.patient_id))">${icon('plus')}Register patient</button>`)}
    <div class="card"><div class="card-h">${searchBox('ptSearch', 'Search by name or ID…', 'clFilterPatients()')}</div><div class="card-b flush" id="ptTable"></div></div>`;
}
function clFilterPatients() {
  const q = ($('ptSearch')?.value || '').toLowerCase();
  const rows = clPatientsCache.filter(p => !q || (p.full_name || '').toLowerCase().includes(q) || (p.patient_id || '').toLowerCase().includes(q));
  $('ptTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Patient</th><th>ID</th><th>Age / sex</th><th>Scans</th><th>Last scan</th><th>Registered</th></tr></thead><tbody>
    ${rows.map(p => `<tr class="click" onclick="go('#/patient/${esc(p.patient_id)}')"><td><div class="row"><div class="avatar">${initials(p.full_name)}</div><b>${esc(p.full_name)}</b></div></td>
      <td class="mono">${esc(p.patient_id)}</td><td>${p.age != null ? esc(p.age) + ' y' : '—'} ${p.gender ? '· ' + esc(p.gender) : ''}</td><td>${p.scan_count || 0}</td>
      <td class="small muted">${p.last_scan ? ago(p.last_scan) : '—'}</td><td class="small muted">${fmtDate(p.created_at, false)}</td></tr>`).join('')}</tbody></table></div>`
    : emptyState('users', q ? 'No matching patients' : 'No patients yet', q ? 'Try a different search.' : 'Register a patient to start scanning.');
}
async function clPatient(id) {
  const d = await apiJSON(`/api/patients/${encodeURIComponent(id)}`);
  const p = d.patient;
  return `<div class="row" style="margin-bottom:16px"><a class="btn sm ghost" href="#/patients">${icon('back', 15)}All patients</a></div>
  <div class="grid g-2-1">
    <div class="stack">
      <div class="card"><div class="card-b row wrap" style="gap:16px"><div class="avatar" style="width:56px;height:56px;font-size:20px">${initials(p.full_name)}</div>
        <div style="flex:1;min-width:200px"><h2 style="font-size:22px">${esc(p.full_name)}</h2><div class="muted">${esc(p.patient_id)} · ${p.age != null ? esc(p.age) + ' years' : 'age not recorded'}${p.gender ? ' · ' + (p.gender === 'F' ? 'Female' : p.gender === 'M' ? 'Male' : esc(p.gender)) : ''}</div></div>
        <button class="btn secondary" onclick='clEditPatient(${JSON.stringify(p).replace(/'/g, "&#39;")})'>${icon('edit', 16)}Edit</button>
        <a class="btn" href="#/scan/${esc(p.patient_id)}">${icon('scan', 16)}New scan</a></div></div>
      ${card('Scan timeline', d.predictions.length ? `<div class="timeline">${d.predictions.map(x => `
        <div class="tl-item ${esc(x.triage_state)}"><div class="row wrap" style="cursor:pointer" onclick="go('#/case/${esc(x.prediction_id)}')">
          ${x.image_png ? `<img class="thumb" src="${x.image_png}" alt="">` : ''}
          <div style="flex:1"><div class="row">${stateBadge(x.triage_state)}${x.confirmed_outcome ? outcomeBadge(x.confirmed_outcome) : ''}</div>
            <div class="small muted" style="margin-top:4px">${fmtDate(x.created_at)} · P(pneumonia) ${x.calibrated_prob != null ? x.calibrated_prob.toFixed(3) : '—'}</div></div>
          ${icon('arrow', 16)}</div></div>`).join('')}</div>` : emptyState('scan', 'No scans yet', '', `<a class="btn" href="#/scan/${esc(p.patient_id)}">Start a scan</a>`),
        {sub: `${d.predictions.length} scan(s)`})}
    </div>
    <div class="stack">
      ${card('Details', `<div class="stack small">
        <div><div class="label">Parent / guardian</div>${esc(p.guardian_name || '—')}</div>
        <div><div class="label">Contact</div>${esc(p.contact || '—')}</div>
        <div><div class="label">Clinical notes</div>${esc(p.notes || '—')}</div>
        <div><div class="label">Registered</div>${fmtDate(p.created_at)}</div></div>`)}
      ${card('Specialist referrals', d.escalations.length ? d.escalations.map(e => `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div class="row">${escBadge(e.status)}${priorityBadge(e.priority)}<span class="spacer"></span>${outcomeBadge(e.confirmed_outcome)}</div>
        <div class="small muted" style="margin-top:4px">${esc(e.specialist_name || '')} · sent ${ago(e.created_at)}</div>
        ${e.specialist_comment ? `<div class="small" style="margin-top:4px">"${esc(e.specialist_comment)}"</div>` : ''}</div>`).join('') : '<p class="muted small">No referrals for this patient.</p>')}
    </div></div>`;
}
function clEditPatient(p) {
  const m = openModal({title: 'Edit patient', body: clPatientForm(p),
    footer: `<button class="btn secondary" data-close>Cancel</button><button class="btn" id="ptSave">Save changes</button>`});
  m.q('#ptSave').onclick = () => withBusy(m.q('#ptSave'), 'Saving…', async () => {
    const body = clReadPatient(m);
    if (!body.full_name) return toast('Patient name is required', 'error');
    try { await apiJSON(`/api/patients/${encodeURIComponent(p.patient_id)}`, {method: 'PATCH', json: body}); m.close(); toast('Patient updated', 'success'); route(); }
    catch (e) { toast(e.message, 'error'); }
  });
}

/* ---------- history ---------- */
let clHist = [], clHistState = '';
async function clHistory() {
  clHist = (await apiJSON('/api/cases?mine=true&limit=500')).cases;
  const segBtn = (v, l) => `<button class="${clHistState === v ? 'on' : ''}" onclick="clHistState='${v}';this.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('on'));this.classList.add('on');clFilterHistory()">${l}</button>`;
  return `${intro('Scan history', 'Every radiograph you have analysed, newest first.',
      `<button class="btn secondary" onclick="clExportHistory()">${icon('download', 16)}Export CSV</button>`)}
    <div class="card"><div class="card-h row wrap">${searchBox('hiSearch', 'Search patient or file…', 'clFilterHistory()')}<span class="spacer"></span>
      <div class="seg">${segBtn('', 'All')}${Object.keys(STATE).map(s => segBtn(s, STATE[s].label)).join('')}</div></div>
      <div class="card-b flush" id="hiTable"></div></div>`;
}
function clHistRows() {
  const q = ($('hiSearch')?.value || '').toLowerCase();
  return clHist.filter(x => (!clHistState || x.triage_state === clHistState) &&
    (!q || (x.patient_name || '').toLowerCase().includes(q) || (x.filename || '').toLowerCase().includes(q)));
}
function clFilterHistory() {
  const rows = clHistRows();
  $('hiTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Date</th><th>Patient</th><th>Result</th><th>P(pneumonia)</th><th>Confirmed</th><th></th></tr></thead><tbody>
    ${rows.map(x => `<tr class="click" onclick="go('#/case/${esc(x.prediction_id)}')"><td class="small">${fmtDate(x.created_at)}</td>
      <td><b>${esc(x.patient_name || 'Unknown')}</b><div class="small muted">${esc(x.filename || '')}</div></td><td>${stateBadge(x.triage_state)}</td>
      <td class="mono">${x.calibrated_prob != null ? x.calibrated_prob.toFixed(3) : '—'}</td><td>${outcomeBadge(x.confirmed_outcome)}</td>
      <td><button class="btn sm ghost" onclick="event.stopPropagation();downloadReport('${esc(x.prediction_id)}')" title="PDF report">${icon('download', 15)}</button></td></tr>`).join('')}</tbody></table></div>`
    : emptyState('clock', 'No scans found', clHist.length ? 'Try a different filter.' : 'Your analysed radiographs will appear here.');
}
function clExportHistory() {
  downloadCSV('pneumoscan_scan_history.csv', ['date', 'patient', 'patient_id', 'file', 'triage_state', 'p_pneumonia', 'ood_score', 'confirmed_outcome'],
    clHistRows().map(x => [x.created_at, x.patient_name, x.patient_id, x.filename, x.triage_state, x.calibrated_prob, x.ood_score, x.confirmed_outcome]));
}

/* ---------- referrals ---------- */
let clRefs = [], clRefTab = 'open';
async function clReferrals() {
  clRefs = (await apiJSON('/api/escalations?images=false')).escalations;
  const open = clRefs.filter(r => r.status !== 'resolved').length;
  setCount('referrals', open);
  const tab = (v, l) => `<button class="${clRefTab === v ? 'on' : ''}" onclick="clRefTab='${v}';this.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('on'));this.classList.add('on');clFilterReferrals()">${l}</button>`;
  return `${intro('Referrals', 'Cases you have sent to specialists and their responses.')}
    <div class="tabs">${tab('open', `Open (${open})`)}${tab('resolved', `Resolved (${clRefs.length - open})`)}${tab('all', 'All')}</div>
    <div class="card" id="rfTable"></div>`;
}
function clFilterReferrals() {
  const rows = clRefs.filter(r => clRefTab === 'all' || (clRefTab === 'open' ? r.status !== 'resolved' : r.status === 'resolved'));
  $('rfTable').innerHTML = rows.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Patient</th><th>Specialist</th><th>Priority</th><th>Sent</th><th>Status</th><th>Diagnosis</th><th>Waiting / turnaround</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="click" onclick="go('#/case/${esc(r.prediction_id)}')"><td><b>${esc(r.patient_name || 'Unknown')}</b><div class="small muted">${stateBadge(r.triage_state)}</div></td>
      <td>${esc(r.specialist_name || '—')}</td><td>${priorityBadge(r.priority)}</td><td class="small">${fmtDate(r.created_at)}</td><td>${escBadge(r.status)}</td>
      <td>${outcomeBadge(r.confirmed_outcome)}</td><td>${r.status === 'resolved' ? duration((new Date(r.resolved_at) - new Date(r.created_at)) / 36e5) : waitLabel(r.created_at)}</td></tr>`).join('')}</tbody></table></div>`
    : emptyState('send', 'Nothing here', clRefTab === 'open' ? 'No referrals are waiting for a specialist.' : 'No referrals yet.');
}

/* ---------- case details ---------- */
async function clCase(id) {
  const d = await apiJSON(`/api/cases/${encodeURIComponent(id)}`);
  const c = d.case, p = d.patient;
  if (!clGate) clGate = (await apiJSON('/api/model-info').catch(() => ({gate: {}}))).gate.confidence_threshold;
  return `<div class="row" style="margin-bottom:16px"><button class="btn sm ghost" onclick="history.back()">${icon('back', 15)}Back</button></div>
  <div class="grid g-1-1">
    <div class="card"><div class="card-h"><h3>${esc(p ? p.full_name : 'Unknown patient')}</h3><span class="sub">${fmtDate(c.created_at)}</span><span class="spacer"></span>
        ${p ? `<a class="btn sm secondary" href="#/patient/${esc(p.patient_id)}">${icon('user', 15)}Patient record</a>` : ''}</div>
      <div class="card-b">${clResult({...c, reason: STATE[c.triage_state]?.desc}, c.image_png, d.escalations)}</div></div>
    <div class="stack">
      ${card('Specialist review', d.escalations.length ? d.escalations.map(e => `<div class="stack">
          <div class="row"><div class="avatar">${initials(e.specialist_name)}</div><div style="flex:1"><b>${esc(e.specialist_name || 'Specialist')}</b>
            <div class="small muted">Sent ${fmtDate(e.created_at)}</div></div>${priorityBadge(e.priority)}${escBadge(e.status)}</div>
          ${e.note ? `<div><div class="label">Your note</div><p>${esc(e.note)}</p></div>` : ''}
          ${e.status === 'resolved' ? `<div class="kv"><div><div class="k">Diagnosis</div><div class="v">${esc(e.confirmed_outcome || '—')}</div></div>
              <div><div class="k">Turnaround</div><div class="v">${duration((new Date(e.resolved_at) - new Date(e.created_at)) / 36e5)}</div></div>
              <div><div class="k">AI agreement</div><div class="v">${e.ai_agrees == null ? '—' : e.ai_agrees ? 'Agrees' : 'Disagrees'}</div></div></div>
            ${e.findings ? `<div><div class="label">Findings</div><p>${esc(e.findings)}</p></div>` : ''}
            ${e.recommendation ? `<div><div class="label">Recommendation</div><p>${esc(e.recommendation)}</p></div>` : ''}
            ${e.specialist_comment ? `<div><div class="label">Comment</div><p>${esc(e.specialist_comment)}</p></div>` : ''}`
          : `<div class="form-note">Waiting for the specialist (${duration(hoursSince(e.created_at))} so far). You will be notified when they respond.</div>`}</div>`).join('<hr style="border:0;border-top:1px solid var(--border);margin:16px 0">')
        : `<p class="muted">This case has not been referred.</p>`)}
      ${p ? card('Patient', `<div class="stack small"><div class="row"><span class="muted">ID</span><span class="spacer"></span><span class="mono">${esc(p.patient_id)}</span></div>
        <div class="row"><span class="muted">Age / sex</span><span class="spacer"></span>${p.age != null ? esc(p.age) + ' y' : '—'} ${p.gender ? '· ' + esc(p.gender) : ''}</div>
        ${p.notes ? `<div><div class="label">Notes</div>${esc(p.notes)}</div>` : ''}</div>`) : ''}
    </div></div>`;
}
