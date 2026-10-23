/* Specialist portal: prioritised queue, review workspace, completed reviews. */
PORTALS.specialist = {
  title: 'Specialist portal', icon: 'steth', home: 'overview',
  nav: [{section: 'Reviews'},
    {id: 'overview', label: 'Overview', icon: 'grid'},
    {id: 'queue', label: 'Review queue', icon: 'inbox'},
    {id: 'completed', label: 'Completed', icon: 'check'}],
  pages: {
    overview: {title: 'Overview', render: spOverview},
    queue: {title: 'Review queue', render: spQueue, after: () => spFilterQueue()},
    review: {title: 'Review case', nav: 'queue', crumb: 'Review workspace', render: spReview},
    completed: {title: 'Completed reviews', render: spCompleted},
  },
  onNotify: () => spLoad().catch(() => {}),
};
let spEscs = [];
const spOpen = () => spEscs.filter(e => e.status !== 'resolved')
  .sort((a, b) => (a.priority === 'urgent' ? 0 : 1) - (b.priority === 'urgent' ? 0 : 1) || (a.created_at || '').localeCompare(b.created_at || ''));
async function spLoad() {
  spEscs = (await apiJSON('/api/escalations?images=false')).escalations;
  setCount('queue', spOpen().length);
  return spEscs;
}
const spTurn = e => (new Date(e.resolved_at) - new Date(e.created_at)) / 36e5;

async function spOverview() {
  await spLoad();
  const open = spOpen(), done = spEscs.filter(e => e.status === 'resolved');
  const turns = done.map(spTurn).filter(x => !isNaN(x)).sort((a, b) => a - b);
  const judged = done.filter(e => e.ai_agrees != null);
  const agree = judged.filter(e => e.ai_agrees).length;
  const next = open[0];
  return `${intro(`Welcome, ${esc(ME.full_name || '')}`, 'Your referral workload and review performance.')}
    <div class="grid g4" style="margin-bottom:18px">
      ${statCard('inbox', open.length, 'Open referrals', 'warn', `${open.filter(e => e.status === 'pending').length} in review`)}
      ${statCard('alert', open.filter(e => e.priority === 'urgent').length, 'Urgent waiting', 'danger', open.length ? `Oldest waiting ${duration(hoursSince(open.reduce((a, e) => (e.created_at < a ? e.created_at : a), open[0].created_at)))}` : 'Queue is clear')}
      ${statCard('check', done.length, 'Completed reviews', 'ok', turns.length ? `Median turnaround ${duration(turns[Math.floor(turns.length / 2)])}` : '')}
      ${statCard('target', judged.length ? pct(agree / judged.length, 0) : '—', 'Agreement with AI', 'info', `${judged.length} decided case(s)`)}
    </div>
    <div class="grid g-2-1">
      ${card('Next case', next ? `<div class="row wrap" style="gap:16px"><div class="avatar" style="width:52px;height:52px;font-size:18px">${initials(next.patient_name)}</div>
          <div style="flex:1;min-width:200px"><h3 style="font-size:18px">${esc(next.patient_name || 'Unknown patient')}</h3>
            <div class="muted small">${next.age != null ? esc(next.age) + ' y · ' : ''}${esc(next.gender || '')} · from ${esc(next.clinician_name || 'clinician')}</div>
            <div class="row" style="margin-top:8px">${priorityBadge(next.priority)}${stateBadge(next.triage_state)}<span class="small muted">waiting ${waitLabel(next.created_at)}</span></div></div>
          <a class="btn lg" href="#/review/${esc(next.escalation_id)}">${icon('eye')}${next.status === 'pending' ? 'Continue review' : 'Start review'}</a></div>
          ${next.note ? `<div class="form-note" style="margin-top:16px"><b>Clinician note:</b> ${esc(next.note)}</div>` : ''}`
        : emptyState('check', 'Queue is clear', 'No referrals are waiting for you.'),
        {action: `<a class="btn sm ghost" href="#/queue">Full queue ${icon('arrow', 14)}</a>`})}
      <div class="stack">
        ${card('Availability', `<div class="row"><div style="flex:1"><b>Accepting new referrals</b><div class="small muted">Turn off when you are away.</div></div>
          <label class="switch"><input type="checkbox" data-avail ${ME.available !== false ? 'checked' : ''} onchange="setAvailability(this.checked)"><span></span></label></div>`)}
        ${card('Recent decisions', done.length ? done.sort((a, b) => (b.resolved_at || '').localeCompare(a.resolved_at || '')).slice(0, 5).map(e => `
          <div class="row" style="padding:7px 0"><div style="flex:1;min-width:0"><b>${esc(e.patient_name || 'Unknown')}</b><div class="small muted">${ago(e.resolved_at)}</div></div>
          ${outcomeBadge(e.confirmed_outcome)}${e.ai_agrees == null ? '' : e.ai_agrees ? '<span class="badge b-ok plain">AI ✓</span>' : '<span class="badge b-danger plain">AI ✗</span>'}</div>`).join('')
          : '<p class="muted small">No completed reviews yet.</p>')}
      </div></div>`;
}

let spPri = '';
async function spQueue() {
  await spLoad();
  const seg = (v, l) => `<button class="${spPri === v ? 'on' : ''}" onclick="spPri='${v}';this.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('on'));this.classList.add('on');spFilterQueue()">${l}</button>`;
  return `${intro('Review queue', 'Urgent referrals first, then oldest first. Waiting time turns amber after 4 h and red after 24 h.',
      `<button class="btn secondary" onclick="route()">${icon('refresh', 16)}Refresh</button>`)}
    <div class="card"><div class="card-h"><div class="seg">${seg('', 'All')}${seg('urgent', 'Urgent')}${seg('routine', 'Routine')}</div><span class="spacer"></span>
      <span class="small muted">${spOpen().length} open</span></div><div id="spList"></div></div>`;
}
function spFilterQueue() {
  const rows = spOpen().filter(e => !spPri || e.priority === spPri);
  $('spList').innerHTML = rows.length ? rows.map(e => `<div class="queue-item ${e.priority === 'urgent' ? 'urgent' : ''}" onclick="go('#/review/${esc(e.escalation_id)}')">
      <div class="avatar">${initials(e.patient_name)}</div>
      <div style="flex:1;min-width:0"><div class="row"><b>${esc(e.patient_name || 'Unknown patient')}</b><span class="small muted">${e.age != null ? esc(e.age) + ' y' : ''} ${esc(e.gender || '')}</span></div>
        <div class="small muted">From ${esc(e.clinician_name || 'clinician')}${e.note ? ' · “' + esc(e.note.slice(0, 80)) + (e.note.length > 80 ? '…' : '') + '”' : ''}</div></div>
      <div class="row" style="gap:8px">${priorityBadge(e.priority)}${stateBadge(e.triage_state)}${escBadge(e.status)}</div>
      <div style="width:90px;text-align:right">${waitLabel(e.created_at)}<div class="small muted">waiting</div></div></div>`).join('')
    : emptyState('check', 'Nothing waiting', 'New referrals will appear here and in your notifications.');
}

async function spReview(id) {
  let esc0 = (await spLoad()).find(e => e.escalation_id === id);
  if (!esc0) throw new Error('This referral was not found or is not assigned to you.');
  if (esc0.status === 'sent') {
    await apiJSON(`/api/escalations/${encodeURIComponent(id)}/start`, {method: 'POST'});
    esc0.status = 'pending'; setCount('queue', spOpen().length);
  }
  const d = await apiJSON(`/api/cases/${encodeURIComponent(esc0.prediction_id)}`);
  const c = d.case, p = d.patient || {};
  const gate = (await apiJSON('/api/model-info').catch(() => ({gate: {}}))).gate.confidence_threshold;
  const done = esc0.status === 'resolved';
  setTimeout(() => { const f = $('rvForm'); if (f) f.onsubmit = ev => spSubmit(ev, id); });
  return `<div class="row" style="margin-bottom:16px"><a class="btn sm ghost" href="#/queue">${icon('back', 15)}Queue</a><span class="spacer"></span>
      ${priorityBadge(esc0.priority)}${escBadge(esc0.status)}<span class="small muted">received ${fmtDate(esc0.created_at)}</span></div>
  <div class="grid g-1-1">
    <div class="stack">
      ${card('Radiograph', viewer(c.image_png, c.heatmap_png, 'rv'), {action: `<button class="btn sm secondary" onclick="downloadReport('${esc(c.prediction_id)}')">${icon('download', 15)}PDF</button>`})}
      ${card('AI assessment', `<div class="stack"><div class="row">${stateBadge(c.triage_state)}<span class="muted small">${esc(STATE[c.triage_state]?.desc || '')}</span></div>
        <div class="kv"><div><div class="k">P(pneumonia)</div><div class="v">${c.calibrated_prob != null ? c.calibrated_prob.toFixed(3) : '—'}</div></div>
          <div><div class="k">Model leaning</div><div class="v">${c.ai_label ? c.ai_label[0].toUpperCase() + c.ai_label.slice(1) : '—'}</div></div>
          <div><div class="k">OOD distance</div><div class="v">${c.ood_score != null ? Number(c.ood_score).toFixed(0) : '—'}</div></div></div>
        ${c.calibrated_prob != null && gate ? confidenceMeter(confOf(c.calibrated_prob), gate) : ''}
        <p class="small muted">Record your own reading first; the model's leaning is shown for the agreement audit, not as a diagnosis.</p></div>`)}
    </div>
    <div class="stack">
      ${card('Patient & referral', `<div class="row" style="margin-bottom:14px"><div class="avatar" style="width:46px;height:46px">${initials(p.full_name)}</div>
          <div><h3 style="font-size:17px">${esc(p.full_name || 'Unknown patient')}</h3><div class="small muted">${esc(p.patient_id || '')} · ${p.age != null ? esc(p.age) + ' y' : 'age —'} · ${esc(p.gender || '—')}</div></div></div>
        <div class="stack small"><div><div class="label">Referred by</div>${esc(esc0.clinician_name || '—')}</div>
          <div><div class="label">Clinician note</div>${esc(esc0.note || 'No note provided.')}</div>
          ${p.notes ? `<div><div class="label">Patient notes</div>${esc(p.notes)}</div>` : ''}</div>`)}
      ${done ? card('Your decision', `<div class="stack small"><div class="row"><span class="label">Diagnosis</span><span class="spacer"></span>${outcomeBadge(esc0.confirmed_outcome)}</div>
          ${['findings', 'recommendation', 'specialist_comment'].map(k => d.escalations[0]?.[k] ? `<div><div class="label">${{findings: 'Findings', recommendation: 'Recommendation', specialist_comment: 'Comment'}[k]}</div>${esc(d.escalations[0][k])}</div>` : '').join('')}
          <div class="muted">Resolved ${fmtDate(esc0.resolved_at)}</div></div>`)
      : card('Your review', `<form id="rvForm" class="stack">
          <div class="field"><label>Diagnosis *</label><div class="choice">
            <input type="radio" name="rvOut" id="o1" value="pneumonia"><label for="o1">Pneumonia</label>
            <input type="radio" name="rvOut" id="o2" value="normal"><label for="o2">Normal</label>
            <input type="radio" name="rvOut" id="o3" value="unknown"><label for="o3">Inconclusive</label></div></div>
          <div class="field"><label>Radiological findings</label><textarea class="input" id="rvFind" placeholder="e.g. Right lower lobe consolidation with air bronchograms"></textarea></div>
          <div class="field"><label>Recommendation</label><select class="input" id="rvRec"><option value="">Select…</option>
            ${['Start antibiotics per national guideline', 'Admit for inpatient management', 'Treat as outpatient and review in 48 h', 'No pneumonia: manage symptomatically', 'Repeat radiograph (poor quality / positioning)', 'Further investigation required']
              .map(r => `<option>${r}</option>`).join('')}</select></div>
          <div class="field"><label>Comment to clinician *</label><textarea class="input" id="rvComment" required placeholder="Your message to the referring clinician"></textarea></div>
          <button class="btn lg block" id="rvBtn">${icon('check')}Submit review</button></form>`)}
    </div></div>`;
}
async function spSubmit(ev, id) {
  ev.preventDefault();
  const out = document.querySelector('input[name=rvOut]:checked');
  const comment = $('rvComment').value.trim();
  if (!out) return toast('Select a diagnosis', 'error');
  if (!comment) return toast('Add a comment for the clinician', 'error');
  await withBusy($('rvBtn'), 'Submitting…', async () => {
    try {
      const r = await apiJSON(`/api/escalations/${encodeURIComponent(id)}/resolve`, {method: 'POST', json: {
        confirmed_outcome: out.value, specialist_comment: comment, findings: $('rvFind').value.trim() || null, recommendation: $('rvRec').value || null}});
      toast(r.ai_agrees == null ? 'Review submitted' : r.ai_agrees ? 'Review submitted · agrees with AI' : 'Review submitted · disagrees with AI', 'success');
      go('#/queue');
    } catch (e) { toast(e.message, 'error'); }
  });
}

async function spCompleted() {
  await spLoad();
  const done = spEscs.filter(e => e.status === 'resolved').sort((a, b) => (b.resolved_at || '').localeCompare(a.resolved_at || ''));
  return `${intro('Completed reviews', 'Your decisions and how they compare with the model.')}
    ${card(`${done.length} review(s)`, done.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Patient</th><th>Received</th><th>Turnaround</th><th>Your diagnosis</th><th>AI leaning</th><th>Agreement</th><th></th></tr></thead><tbody>
      ${done.map(e => `<tr class="click" onclick="go('#/review/${esc(e.escalation_id)}')"><td><b>${esc(e.patient_name || 'Unknown')}</b><div class="small muted">from ${esc(e.clinician_name || '')}</div></td>
        <td class="small">${fmtDate(e.created_at)}</td><td>${duration(spTurn(e))}</td><td>${outcomeBadge(e.confirmed_outcome)}</td><td>${outcomeBadge(e.ai_label)}</td>
        <td>${e.ai_agrees == null ? '<span class="muted">n/a</span>' : e.ai_agrees ? '<span class="badge b-ok">Agrees</span>' : '<span class="badge b-danger">Disagrees</span>'}</td>
        <td><button class="btn sm ghost" onclick="event.stopPropagation();downloadReport('${esc(e.prediction_id)}')">${icon('download', 15)}</button></td></tr>`).join('')}</tbody></table></div>`
      : emptyState('check', 'No completed reviews', 'Reviews you submit will be listed here.'), {flush: true})}`;
}
