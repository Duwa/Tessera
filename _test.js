
// ─── API ───────────────────────────────────────────────────────────────
const BASE = '/api/v1';
async function api(path, opts={}) {
  try {
    const r = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    return r.json();
  } catch(e) {
    console.error('API error:', path, e.message);
    return null;
  }
}
const GET  = path => api(path);
const POST = (path, data) => api(path, { method:'POST', body: JSON.stringify(data) });

// ─── TOAST ─────────────────────────────────────────────────────────────
function toast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${type}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast', 3000);
}

// ─── NAVIGATION ────────────────────────────────────────────────────────
function nav(view) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.sb-nav-item a').forEach(a => a.classList.remove('active'));
  document.getElementById('view-' + view).classList.add('active');
  const link = document.getElementById('nav-' + view);
  if (link) link.classList.add('active');
  loadView(view);
  return false;
}

// ─── HELPERS ───────────────────────────────────────────────────────────
function havColor(hav) {
  if (hav >= 0.80) return 'exceptional';
  if (hav >= 0.65) return 'strong';
  if (hav >= 0.50) return 'meets';
  if (hav >= 0.35) return 'developing';
  return 'below';
}
function havLabel(hav) {
  if (hav >= 0.80) return 'Exceptional';
  if (hav >= 0.65) return 'Strong';
  if (hav >= 0.50) return 'Meets';
  if (hav >= 0.35) return 'Developing';
  return 'Below';
}
function fmt$(n) { return '$' + Math.round(n).toLocaleString(); }
function fmtPct(n) { return (n*100).toFixed(1) + '%'; }
function fmtHav(n) { return n.toFixed(3); }

function havBarHTML(npf, srq, oc, hav) {
  const npfW = Math.round(npf * 50);
  const srqW = Math.round(srq * 30);
  const ocW  = Math.round(oc  * 20);
  return `
    <div style="display:flex;align-items:center;gap:8px">
      <span class="hav-number ${havColor(hav)}">${fmtHav(hav)}</span>
      <div style="flex:1;min-width:80px">
        <div class="hav-bar">
          <div class="hav-seg npf" style="width:${npfW}%"></div>
          <div class="hav-seg srq" style="width:${srqW}%"></div>
          <div class="hav-seg oc"  style="width:${ocW}%"></div>
        </div>
        <div style="display:flex;gap:6px;margin-top:2px;font-size:9px;color:var(--faint)">
          <span style="color:var(--blue)">NPF ${npf.toFixed(2)}</span>
          <span style="color:var(--amber)">SRQ ${srq.toFixed(2)}</span>
          <span style="color:var(--red)">OC ${oc.toFixed(2)}</span>
        </div>
      </div>
    </div>`;
}

// ─── DEMO DATA ─────────────────────────────────────────────────────────
const DEMO = [
  { id:'emp-001', name:'Maya Chen',     dept:'Engineering', title:'VP Engineering',     salary:180000, npf:0.89, srq:0.85, oc:0.82, trend:'improving' },
  { id:'emp-002', name:'James Okonkwo', dept:'Engineering', title:'Senior Engineer',    salary:145000, npf:0.78, srq:0.72, oc:0.75, trend:'stable'    },
  { id:'emp-003', name:'Sofia Reyes',   dept:'Product',     title:'Product Manager',    salary:130000, npf:0.71, srq:0.68, oc:0.65, trend:'improving' },
  { id:'emp-004', name:'Alex Mercer',   dept:'Engineering', title:'Software Engineer',  salary:115000, npf:0.62, srq:0.58, oc:0.61, trend:'stable'    },
  { id:'emp-005', name:'Jordan Park',   dept:'Design',      title:'Product Designer',   salary:105000, npf:0.55, srq:0.52, oc:0.54, trend:'stable'    },
  { id:'emp-006', name:'Sam Williams',  dept:'Platform',    title:'DevOps Engineer',    salary:108000, npf:0.48, srq:0.45, oc:0.46, trend:'declining' },
  { id:'emp-007', name:'Priya Sharma',  dept:'Data',        title:'Data Scientist',     salary:135000, npf:0.76, srq:0.73, oc:0.71, trend:'improving' },
  { id:'emp-008', name:'Marcus Lee',    dept:'Engineering', title:'Frontend Engineer',  salary:95000,  npf:0.38, srq:0.35, oc:0.40, trend:'stable'    },
  { id:'emp-009', name:'Emma Wilson',   dept:'People',      title:'HR Manager',         salary:110000, npf:0.68, srq:0.64, oc:0.66, trend:'stable'    },
  { id:'emp-010', name:'David Kim',     dept:'Sales',       title:'Sales Lead',         salary:120000, npf:0.31, srq:0.28, oc:0.35, trend:'declining' },
].map(e => ({
  ...e,
  hav: parseFloat((0.50*e.npf + 0.30*e.srq + 0.20*e.oc).toFixed(4))
}));

// State
let STATE = { cycleId: null, meritCycleId: null, seeded: false, reviews: [], twinSimId: null };

// ─── SEEDER ────────────────────────────────────────────────────────────
async function seedDemo() {
  const btn = document.getElementById('seed-btn');
  btn.disabled = true;
  btn.textContent = 'Seeding...';
  const log = document.getElementById('seed-log');
  log.style.display = 'block';
  log.innerHTML = '<div class="seed-step active"><span>○</span> Seeding demo data via gateway...</div>';

  const result = await (await fetch('/demo/seed?org_id=demo-org')).json();

  log.innerHTML = '';
  if (!result || result.error) {
    log.innerHTML = `<div class="seed-step done"><span>✗</span> Seed failed: ${result?.error || 'gateway unreachable'}</div>`;
    btn.disabled = false; btn.textContent = 'Retry'; return;
  }

  const lines = [
    `✓ Performance cycle created (${result.cycle_id?.slice(0,8)}...)`,
    `✓ ${result.reviews_created} HAV reviews submitted`,
    `✓ Compensation records + alignment premiums computed`,
    `✓ ${result.requisitions} recruiting requisitions opened`,
    `✓ ${result.candidates} candidates scored with HAV-potential`,
    `✓ ${result.vc_alerts} Values Custodians flagged for retention`,
  ];
  log.innerHTML = lines.map(l => `<div class="seed-step done"><span>✓</span> ${l}</div>`).join('');

  STATE.seeded = true;
  STATE.cycleId = result.cycle_id;
  STATE.meritCycleId = result.merit_cycle_id;
  if (result.twin_sim_id) STATE.twinSimId = result.twin_sim_id;

  btn.textContent = 'Re-seed';
  btn.disabled = false;
  toast(`Seeded · org φ = ${result.org_phi} · twin φ = ${result.twin_phi ?? '—'}`);
  setTimeout(() => loadView('dashboard'), 400);
}

// ─── LOAD VIEW ─────────────────────────────────────────────────────────
async function loadView(view) {
  switch(view) {
    case 'signals':        loadSignals(); break;
    case 'dashboard':      loadDashboard(); break;
    case 'people':         loadPeople(); break;
    case 'compensation':   loadCompensation(); break;
    case 'recruiting':     loadRecruiting(); break;
    case 'performance':    loadPerformance(); break;
    case 'catalog':        loadCatalog(); break;
    case 'knowledge':      loadKnowledge(); break;
    case 'twin':           loadTwin(); break;
    case 'timeattendance': loadTimeAttendance(); break;
    case 'payroll':        loadPayroll(); break;
    case 'onboarding':     loadOnboarding(); break;
    case 'absence':        loadAbsence(); break;
    case 'learning':       loadLearning(); break;
    case 'import':         loadImport(); break;
    case 'itsm':           loadITSM(); break;
    case 'workforce':      loadWorkforce(); break;
    case 'agents':         loadAgents(); break;
    case 'goals':          loadGoals(); break;
  }
}

// ─── SIGNAL FEED ───────────────────────────────────────────────────────
let _allSignals = [];
let _sigFilter  = 'all';
let _dismissed  = new Set();
let _sigRefreshTimer = null;

async function loadSignals(force = false) {
  const container = document.getElementById('signal-feed-container');
  if (!_allSignals.length || force) {
    container.innerHTML = '<div class="empty"><h3>Gathering signals...</h3></div>';
    try {
      const r = await fetch('/signals?org_id=demo-org');
      const d = await r.json();
      _allSignals = d.signals || [];
      if (d.twin_sim_id && !STATE.twinSimId) STATE.twinSimId = d.twin_sim_id;

      // Update summary counts
      document.getElementById('sig-count-critical').textContent = d.critical ?? 0;
      document.getElementById('sig-count-warning').textContent  = d.warning  ?? 0;
      document.getElementById('sig-count-info').textContent     = d.info     ?? 0;
      document.getElementById('sig-last-refresh').textContent   =
        new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});

      // Sidebar badge
      const badge = document.getElementById('sb-signal-badge');
      const critN = d.critical ?? 0;
      if (critN > 0) {
        badge.textContent = critN;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
      }
    } catch(e) {
      container.innerHTML = '<div class="empty"><h3>Signal feed unavailable</h3><p>Seed demo data first, then refresh.</p></div>';
      return;
    }
  }
  renderSignals();
  // Auto-refresh
  clearTimeout(_sigRefreshTimer);
  _sigRefreshTimer = setTimeout(() => {
    if (document.getElementById('view-signals').classList.contains('active')) {
      loadSignals(true);
    }
  }, 30000);
}

function filterSignals(f) {
  _sigFilter = f;
  document.querySelectorAll('.sig-filter').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === f);
  });
  renderSignals();
}

function dismissSignal(type) {
  _dismissed.add(type);
  renderSignals();
}

function renderSignals() {
  const container = document.getElementById('signal-feed-container');
  const visible = _allSignals.filter(s =>
    (_sigFilter === 'all' || s.severity === _sigFilter)
  );

  if (!visible.length) {
    container.innerHTML = '<div class="empty"><h3>No signals</h3><p>All governance indicators nominal for this filter.</p></div>';
    return;
  }

  const SEV_ICON = { critical: '●', warning: '◆', info: '○' };

  container.innerHTML = visible.map(s => {
    const dismissed = _dismissed.has(s.type);
    const timeAgo = s.timestamp
      ? (() => { const d = (Date.now() - new Date(s.timestamp)) / 1000; return d < 60 ? 'just now' : Math.round(d/60) + 'm ago'; })()
      : '';
    return `<div class="sig-card ${s.severity} ${dismissed ? 'dismissed' : ''}" id="sigcard-${s.type}">
      <div class="sig-top">
        <span class="sig-sev ${s.severity}">${SEV_ICON[s.severity]||''} ${s.severity.toUpperCase()}</span>
        <span style="font-size:10px;background:var(--panel2);color:var(--faint);padding:2px 8px;border-radius:99px;border:1px solid var(--line)">${s.source}</span>
        <span class="sig-source">${timeAgo}</span>
      </div>
      <div class="sig-title">${s.title}</div>
      <div class="sig-body">${s.body}</div>
      ${s.detail ? `<div class="sig-detail">${s.detail}</div>` : ''}
      <div class="sig-actions">
        ${s.action_label && s.action_nav
          ? `<button class="btn btn-green" style="font-size:11px;padding:5px 12px" onclick="nav('${s.action_nav}')">${s.action_label}</button>`
          : ''}
        <button class="btn btn-outline" style="font-size:11px;padding:5px 12px" onclick="dismissSignal('${s.type}')">
          ${dismissed ? 'Dismissed' : 'Dismiss'}
        </button>
      </div>
    </div>`;
  }).join('');
}

// ─── DASHBOARD ─────────────────────────────────────────────────────────
async function loadDashboard() {
  const reviews = await GET(`/performance/reviews?cycle_id=${STATE.cycleId || ''}`) || { reviews: [] };
  const alerts  = await GET('/benefits/retention-alerts?org_id=demo-org') || { alerts: [] };

  const revs = reviews.reviews || [];
  const vcs  = (alerts.alerts || []).filter(a => a.mean_hav >= 0.70 && a.mean_npf >= 0.65);

  // Compute φ from reviews
  let phi = 0, totalAP = 0, payroll = 0;
  if (revs.length) {
    phi = revs.reduce((s,r) => s + r.mean_hav, 0) / revs.length;
    // Compute AP from demo data
    const r_ap = phi < 0.25 ? 0.05 : phi > 0.75 ? 0.25 : 0.05 + (phi-0.25)*0.40;
    DEMO.forEach(e => {
      const ap = r_ap * e.hav * e.salary;
      totalAP += ap;
      payroll += e.salary;
    });
  }

  // Update sidebar phi
  document.getElementById('sb-phi').textContent = phi ? phi.toFixed(3) : '—';

  // KPIs
  document.getElementById('kpi-employees').textContent  = revs.length || '—';
  document.getElementById('kpi-phi').textContent        = phi ? phi.toFixed(3) : '—';
  document.getElementById('kpi-vc').textContent         = vcs.length || '—';
  document.getElementById('kpi-ap').textContent         = totalAP ? fmt$(totalAP) : '—';

  // φ distribution chart
  if (revs.length) {
    renderPhiChart(revs);
    renderVCTable(vcs);
  }
}

function renderPhiChart(reviews) {
  const buckets = [0,0,0,0,0,0,0,0,0,0]; // 0.0-0.1, 0.1-0.2, ... 0.9-1.0
  reviews.forEach(r => {
    const b = Math.min(9, Math.floor(r.mean_hav * 10));
    buckets[b]++;
  });
  const max = Math.max(...buckets, 1);
  const labels = ['0.0','0.1','0.2','0.3','0.4','0.5','0.6','0.7','0.8','0.9'];

  const html = `
    <div style="padding:.75rem 1rem 0">
      <div style="display:flex;align-items:flex-end;gap:4px;height:100px">
        ${buckets.map((n, i) => {
          const h = Math.max(4, Math.round((n/max)*90));
          const isVC = i >= 7;
          return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">
            <div style="font-size:9px;color:var(--faint)">${n||''}</div>
            <div style="width:100%;height:${h}px;border-radius:3px 3px 0 0;background:${isVC?'#A07BE5':i>=5?'var(--green)':'var(--panel2)'};transition:.4s"></div>
          </div>`;
        }).join('')}
      </div>
      <div style="display:flex;gap:4px;border-top:1px solid var(--line);padding-top:4px">
        ${labels.map(l => `<div style="flex:1;text-align:center;font-size:9px;color:var(--faint)">${l}</div>`).join('')}
      </div>
      <div style="display:flex;gap:1rem;margin-top:.75rem;font-size:10px">
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:#A07BE5;display:inline-block"></span>Values Custodian (≥0.7)</span>
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:var(--green);display:inline-block"></span>Above φ* (≥0.5)</span>
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:var(--panel2);border:1px solid var(--line);display:inline-block"></span>Below φ*</span>
      </div>
    </div>`;
  document.getElementById('phi-chart-container').innerHTML = html;
}

function renderVCTable(vcs) {
  if (!vcs.length) {
    document.getElementById('vc-table').innerHTML = '<div class="empty" style="padding:2rem"><p>No retention alerts found. Seed demo data first.</p></div>';
    return;
  }
  const rows = vcs.map(v => {
    const emp = DEMO.find(e => e.id === v.employee_id) || {};
    return `<tr>
      <td><strong style="color:var(--text)">${emp.name || v.employee_id}</strong><br><span style="color:var(--faint);font-size:11px">${emp.title || ''}</span></td>
      <td>${havBarHTML(v.mean_npf, v.mean_npf*0.85, v.mean_npf*0.82, v.mean_hav)}</td>
      <td><span class="badge vc">VC</span></td>
      <td><span class="badge ${v.severity === 'critical' ? 'exceptional' : 'meets'}" style="text-transform:capitalize">${v.severity}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('vc-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>HAV Score</th><th>Status</th><th>Alert</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ─── PEOPLE ────────────────────────────────────────────────────────────
async function loadPeople() {
  const data = await GET(`/performance/reviews?cycle_id=${STATE.cycleId || ''}`) || { reviews: [] };
  const revs = data.reviews || [];
  if (!revs.length) return;

  document.getElementById('people-count').textContent = `${revs.length} employees`;

  const sorted = [...revs].sort((a,b) => b.mean_hav - a.mean_hav);
  const rows = sorted.map(r => {
    const emp = DEMO.find(e => e.id === r.employee_id) || { name: r.employee_id, dept:'—', title:'—' };
    const isVC = r.mean_hav >= 0.70 && r.mean_npf >= 0.65;
    return `<tr>
      <td>
        <strong style="color:var(--text)">${emp.name}</strong>
        <div style="font-size:11px;color:var(--faint)">${emp.dept} · ${emp.title}</div>
      </td>
      <td>${havBarHTML(r.mean_npf, r.mean_srq, r.mean_oc, r.mean_hav)}</td>
      <td><span class="badge ${r.rating}">${r.rating ? r.rating.charAt(0).toUpperCase()+r.rating.slice(1) : '—'}</span></td>
      <td style="color:var(--faint);font-size:12px">${r.hav_trend || '—'}</td>
      <td>${isVC ? '<span class="badge vc">VC</span>' : ''}</td>
      <td style="color:var(--green);font-size:12px">${r.merit_recommendation ? '+'+fmtPct(r.merit_recommendation) : '—'}</td>
    </tr>`;
  }).join('');

  document.getElementById('people-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>HAV · NPF · SRQ · OC</th><th>Rating</th><th>Trend</th><th>Status</th><th>Merit</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ─── COMPENSATION ───────────────────────────────────────────────────────
async function loadCompensation() {
  const reviews = await GET(`/performance/reviews?cycle_id=${STATE.cycleId || ''}`) || { reviews: [] };
  const revs = reviews.reviews || [];
  if (!revs.length) {
    document.getElementById('comp-table').innerHTML = '<div class="empty"><h3>No Data</h3><p>Seed demo data to see compensation records</p></div>';
    return;
  }

  const phi = revs.reduce((s,r) => s+r.mean_hav, 0) / revs.length;
  const r_ap = phi < 0.25 ? 0.05 : phi > 0.75 ? 0.25 : 0.05 + (phi-0.25)*0.40;

  let totalPayroll = 0, totalAP = 0;
  const rows = DEMO.map(emp => {
    const ap = r_ap * emp.hav * emp.salary;
    const tokenBudget = emp.hav >= 0.65 ? 12000 : 6000;
    const totalComp = emp.salary + tokenBudget + ap;
    totalPayroll += emp.salary;
    totalAP += ap;
    return { emp, ap, tokenBudget, totalComp };
  }).sort((a,b) => b.ap - a.ap);

  document.getElementById('comp-rap').textContent       = fmtPct(r_ap);
  document.getElementById('comp-payroll').textContent   = fmt$(totalPayroll);
  document.getElementById('comp-ap-total').textContent  = fmt$(totalAP);
  document.getElementById('comp-ap-pct').textContent    = fmtPct(totalAP/totalPayroll);

  const tableRows = rows.map(({emp, ap, tokenBudget, totalComp}) => `<tr>
    <td><strong style="color:var(--text)">${emp.name}</strong><div style="font-size:11px;color:var(--faint)">${emp.title}</div></td>
    <td><span class="hav-number ${havColor(emp.hav)}">${fmtHav(emp.hav)}</span></td>
    <td style="color:var(--muted)">${fmt$(emp.salary)}</td>
    <td style="color:var(--blue)">${fmt$(tokenBudget)}</td>
    <td style="color:var(--green);font-weight:600">${fmt$(ap)}</td>
    <td style="color:var(--text);font-weight:600">${fmt$(totalComp)}</td>
  </tr>`).join('');

  document.getElementById('comp-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>HAV</th><th>Base Salary</th><th>Token Budget</th><th>Alignment Premium</th><th>Total Comp</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>`;
}

// ─── RECRUITING ─────────────────────────────────────────────────────────
async function loadRecruiting() {
  // Twin predictions
  const pred = await GET('/twin/orgs/demo-org/role-predictions');
  const predBody = document.getElementById('rec-pred-body');
  if (pred) {
    const traj = { above_crossover:'● Above φ* — HAV regime', approaching:'◆ Approaching φ*', sub_crossover:'○ Sub-crossover' }[pred.trajectory] || pred.trajectory;
    const urgColor = { critical:'var(--red)', high:'var(--amber)', medium:'var(--blue)' };
    const riskColor = { critical:'var(--red)', high:'var(--amber)' };
    document.getElementById('rec-pred-sub').textContent = `φ=${pred.phi} · φ*=${pred.phi_star} · ${traj}`;

    const emergHTML = (pred.emerging_roles || []).map(r => `
      <div style="padding:.625rem 0;border-bottom:1px solid var(--line2);display:flex;align-items:flex-start;gap:.875rem">
        <div style="flex:1">
          <div style="font-size:13px;font-weight:600;color:var(--text)">${r.title}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px;line-height:1.5">${r.reason}</div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:${urgColor[r.urgency]||'var(--faint)'}">${r.urgency}</div>
          ${r.target_hav_min != null ? `<div style="font-size:10px;color:var(--faint);margin-top:2px">HAV min ${r.target_hav_min}</div>` : ''}
        </div>
      </div>`).join('');

    const riskHTML = (pred.at_risk_roles || []).map(r => `
      <div style="padding:.5rem 0;border-bottom:1px solid var(--line2);display:flex;align-items:center;gap:.875rem">
        <div style="flex:1;font-size:12px;color:var(--muted)">${r.title}</div>
        <span style="font-size:10px;font-weight:700;text-transform:uppercase;color:${riskColor[r.risk]||'var(--faint)'}">${r.risk} risk</span>
      </div>`).join('');

    predBody.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.5rem">
        <div>
          <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--green);margin-bottom:.75rem">Emerging Roles</div>
          ${emergHTML || '<div style="color:var(--faint);font-size:12px">None predicted at current φ</div>'}
        </div>
        <div>
          <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--red);margin-bottom:.75rem">At-Risk Roles</div>
          ${riskHTML || '<div style="color:var(--faint);font-size:12px">No roles at risk</div>'}
        </div>
      </div>
      <div style="margin-top:.875rem;padding-top:.875rem;border-top:1px solid var(--line);font-size:10px;color:var(--faint);font-family:\'DM Mono\',monospace">${pred.prediction_basis}</div>`;
  } else {
    predBody.innerHTML = '<div class="empty" style="padding:1rem"><p>Twin not calibrated — seed demo data to get predictions.</p></div>';
  }

  const reqs  = await GET('/recruiting/requisitions?org_id=demo-org') || { requisitions: [] };
  const rList = reqs.requisitions || [];
  document.getElementById('req-count').textContent = `${rList.length} open`;

  if (!rList.length) {
    document.getElementById('req-table').innerHTML = '<div class="empty" style="padding:2rem"><p>No requisitions found</p></div>';
  } else {
    const rows = rList.map(r => `<tr>
      <td>
        <strong style="color:var(--text)">${r.title}</strong>
        <div style="font-size:11px;color:var(--faint)">${r.department} · HC: ${r.headcount}</div>
      </td>
      <td>
        ${r.phi_role === 'phi_guardian' ? '<span class="badge phi-guardian">φ-Guardian</span>' : '<span class="badge open">Standard</span>'}
      </td>
      <td style="color:var(--faint);font-size:11px">HAV min: ${r.target_hav_min || '—'}</td>
    </tr>`).join('');
    document.getElementById('req-table').innerHTML = `
      <table class="tbl">
        <thead><tr><th>Role</th><th>Type</th><th>Threshold</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // Candidates — fetch all for the org in one call
  const cdData = await GET('/recruiting/candidates?org_id=demo-org') || { candidates: [] };
  const candsAll = cdData.candidates || [];

  document.getElementById('cand-count').textContent = `${candsAll.length} candidates`;
  if (!candsAll.length) {
    document.getElementById('cand-table').innerHTML = '<div class="empty" style="padding:2rem"><p>No candidates found</p></div>';
  } else {
    const sorted = candsAll.sort((a,b) => (b.hav_potential||0) - (a.hav_potential||0));
    const rows2 = sorted.map(c => `<tr>
      <td>
        <strong style="color:var(--text)">${c.name}</strong>
        <div style="font-size:11px;color:var(--faint)">${c.req_title||''}</div>
      </td>
      <td>
        ${c.hav_potential != null
          ? `<span class="hav-number ${havColor(c.hav_potential)}">${fmtHav(c.hav_potential)}</span>`
          : '<span style="color:var(--faint)">Unscored</span>'}
      </td>
      <td><span class="badge open" style="text-transform:capitalize">${c.stage||c.status||'applied'}</span></td>
    </tr>`).join('');
    document.getElementById('cand-table').innerHTML = `
      <table class="tbl">
        <thead><tr><th>Candidate</th><th>HAV Potential</th><th>Stage</th></tr></thead>
        <tbody>${rows2}</tbody>
      </table>`;
  }
}

// ─── PERFORMANCE ────────────────────────────────────────────────────────
async function aggregateHav() {
  const btn = document.getElementById('sync-hav-btn');
  btn.disabled = true; btn.textContent = 'Syncing...';
  const res  = document.getElementById('sync-hav-result');
  try {
    const r = await (await fetch('/aggregate-hav?org_id=demo-org', { method:'POST' })).json();
    res.style.display = 'block';
    if (r.error) {
      res.innerHTML = `<div style="background:var(--red-d);border:1px solid rgba(229,80,74,.25);border-radius:8px;padding:.875rem;font-size:12px;color:var(--red)">✗ ${r.error}</div>`;
    } else {
      const twinLine = r.twin_recalibrated
        ? `<div style="margin-top:.5rem;font-size:11px;color:var(--muted)">Twin recalibrated → φ=${r.twin_phi?.toFixed(3)} · φ*=${r.twin_phi_star?.toFixed(3)} · ${r.twin_crossover ? '<span style="color:var(--amber)">above crossover</span>' : '<span style="color:var(--green)">sub-crossover</span>'}</div>`
        : `<div style="margin-top:.5rem;font-size:11px;color:var(--faint)">Twin not recalibrated (no new reviews)</div>`;
      res.innerHTML = `<div style="background:var(--green-d);border:1px solid var(--green-line);border-radius:8px;padding:.875rem;font-size:12px;color:var(--green)">
        ✓ ${r.employees_updated} employees updated · ${r.employees_no_sessions} with no sessions
        <span style="color:var(--faint);margin-left:8px">cycle ${r.cycle_id?.slice(0,8)}…</span>
        ${twinLine}
      </div>`;
      toast(`HAV synced · ${r.employees_updated} updated · twin ${r.twin_recalibrated ? 'recalibrated' : 'unchanged'}`);
      setTimeout(() => loadPerformance(), 300);
    }
  } catch(e) {
    res.style.display = 'block';
    res.innerHTML = `<div style="background:var(--red-d);border:1px solid rgba(229,80,74,.25);border-radius:8px;padding:.875rem;font-size:12px;color:var(--red)">✗ Aggregate failed — check services are running</div>`;
  }
  btn.disabled = false; btn.textContent = 'Sync from T&A';
}

async function loadPerformance() {
  const data = await GET(`/performance/reviews?cycle_id=${STATE.cycleId || ''}`) || { reviews: [] };
  const revs = (data.reviews || []).sort((a,b) => b.mean_hav - a.mean_hav);
  const title = STATE.cycleId ? `HAV Reviews — FY2026 Q1` : 'HAV Reviews (all cycles)';
  document.getElementById('perf-cycle-title').textContent = title;
  document.getElementById('perf-count').textContent = `${revs.length} reviews`;

  if (!revs.length) return;

  const rows = revs.map(r => {
    const emp = DEMO.find(e => e.id === r.employee_id) || { name: r.employee_id };
    return `<tr>
      <td><strong style="color:var(--text)">${emp.name}</strong></td>
      <td>${havBarHTML(r.mean_npf, r.mean_srq, r.mean_oc, r.mean_hav)}</td>
      <td style="font-size:11px;color:var(--faint)">${fmtPct(r.above_crossover_pct||0)} above φ*</td>
      <td><span class="badge ${r.rating}">${r.rating ? r.rating.charAt(0).toUpperCase()+r.rating.slice(1) : '—'}</span></td>
      <td style="color:var(--green)">${r.merit_recommendation ? '+'+fmtPct(r.merit_recommendation) : '—'}</td>
      <td style="color:var(--faint);font-size:11px">${r.phi_guardian_sessions||0} sessions</td>
    </tr>`;
  }).join('');

  document.getElementById('perf-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>HAV Breakdown</th><th>φ* Coverage</th><th>Rating</th><th>Merit</th><th>φ-Guardian</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ─── CATALOG ────────────────────────────────────────────────────────────
async function loadCatalog() {
  const items   = await GET('/service-catalog/items?org_id=demo-org&per_page=20') || { items: [] };
  const summary = await GET('/service-catalog/reports/summary?org_id=demo-org')   || {};

  const iList = items.items || [];
  if (!iList.length) {
    document.getElementById('catalog-items').innerHTML = '<div class="empty" style="padding:2rem"><p>Catalog items loading or not seeded</p></div>';
  } else {
    const rows = iList.map(i => `<tr>
      <td>
        <strong style="color:var(--text)">${i.name}</strong>
        <div style="font-size:11px;color:var(--faint)">${i.description||''}</div>
      </td>
      <td>${i.requires_phi_guardian ? '<span class="badge phi-guardian">φ-Guardian Required</span>' : ''}</td>
      <td style="font-size:11px;color:var(--faint)">HAV min: ${i.min_fulfiller_hav || '—'}</td>
    </tr>`).join('');
    document.getElementById('catalog-items').innerHTML = `
      <table class="tbl">
        <thead><tr><th>Service</th><th>Gate</th><th>Threshold</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  if (summary.phi_guardian_items_in_flight != null) {
    document.getElementById('catalog-summary').innerHTML = `
      <div style="display:flex;flex-direction:column;gap:.875rem">
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Total Requests</span><strong>${summary.total_requests||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Open</span><strong style="color:var(--amber)">${summary.open_requests||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Fulfilled</span><strong style="color:var(--green)">${summary.fulfilled_requests||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">φ-Guardian In-Flight</span><strong style="color:#A07BE5">${summary.phi_guardian_items_in_flight||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Avg Fulfillment Quality</span><strong style="color:var(--green)">${summary.avg_fulfillment_quality ? (summary.avg_fulfillment_quality*100).toFixed(1)+'%' : '—'}</strong></div>
      </div>`;
  } else {
    document.getElementById('catalog-summary').innerHTML = '<div class="empty" style="padding:1rem"><p>No summary data yet</p></div>';
  }
}

// ─── KNOWLEDGE ──────────────────────────────────────────────────────────
async function loadKnowledge() {
  const defl = await GET('/knowledge/reports/deflection?org_id=demo-org') || {};
  const vc   = await GET('/knowledge/reports/vc-knowledge?org_id=demo-org') || {};

  if (defl.total_deflections != null) {
    document.getElementById('kb-deflection').innerHTML = `
      <div style="display:flex;flex-direction:column;gap:.875rem">
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Total Deflections</span><strong>${defl.total_deflections||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Value Saved</span><strong style="color:var(--green)">${fmt$(defl.total_value_saved||0)}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Published Articles</span><strong>${defl.published_article_count||0}</strong></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Value per Deflection</span><strong>$22.00</strong></div>
      </div>`;
  } else {
    document.getElementById('kb-deflection').innerHTML = '<div class="empty" style="padding:1rem"><p>No deflection data yet. Create and publish KB articles to track value.</p></div>';
  }

  const vcList = vc.articles || [];
  if (!vcList.length) {
    document.getElementById('kb-vc').innerHTML = '<div class="empty" style="padding:2rem"><p>No VC-authored articles found</p></div>';
  } else {
    const rows = vcList.slice(0,6).map(a => `<tr>
      <td>
        <strong style="color:var(--text)">${a.title}</strong>
        <div style="font-size:11px;color:var(--faint)">${a.category_name||''}</div>
      </td>
      <td><span class="hav-number ${havColor(a.author_hav||0)}">${fmtHav(a.author_hav||0)}</span></td>
      <td style="color:var(--green);font-size:11px">${fmt$(a.deflection_value_at_risk||0)}</td>
    </tr>`).join('');
    document.getElementById('kb-vc').innerHTML = `
      <table class="tbl">
        <thead><tr><th>Article</th><th>Author HAV</th><th>Value at Risk</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }
}

// ─── DIGITAL TWIN ────────────────────────────────────────────────────────
async function resolveTwinSimId() {
  if (STATE.twinSimId) return STATE.twinSimId;
  const d = await GET('/twin/orgs/demo-org/sim');
  if (d && d.sim_id) { STATE.twinSimId = d.sim_id; return d.sim_id; }
  return null;
}

async function loadTwin() {
  const simId = await resolveTwinSimId();
  if (!simId) {
    document.getElementById('twin-phi-chart').innerHTML = '<div class="empty" style="padding:2rem"><p>No twin calibrated. Seed demo data first (HAV Overview → Seed Demo Data).</p></div>';
    return;
  }

  const [warn, hist, comp] = await Promise.all([
    GET(`/twin/sim/${simId}/early-warning`),
    GET(`/twin/orgs/demo-org/phi-history?last_n=20`),
    GET('/people/composition'),
  ]);

  // KPIs
  const ev = warn?.evidence || {};
  const phi = ev.phi ?? '—';
  const pstar = ev.phi_star ?? '—';
  const mhav = ev.mean_hav;
  const stage = warn?.stage ?? '—';

  document.getElementById('twin-phi').textContent = typeof phi === 'number' ? phi.toFixed(3) : phi;
  document.getElementById('twin-phi').className = `kpi-val ${(typeof phi === 'number' && typeof pstar === 'number' && phi > pstar) ? 'kpi-val' : 'kpi-val green'}`;
  document.getElementById('twin-phi-sub').textContent = typeof pstar === 'number' ? `φ* = ${pstar.toFixed(3)}` : 'crossover threshold';
  document.getElementById('twin-phistar').textContent = typeof pstar === 'number' ? pstar.toFixed(3) : '—';
  document.getElementById('twin-hav').textContent = mhav != null ? mhav.toFixed(3) : '—';

  const stageColors = ['green','amber','red'];
  const stageLabels = ['Stage 0 — OK','Stage 1 — Monitor','Stage 2 — Intervene'];
  const stageEl = document.getElementById('twin-stage');
  stageEl.textContent = typeof stage === 'number' ? `Stage ${stage}` : '—';
  stageEl.style.color = typeof stage === 'number' ? `var(--${stageColors[stage]||'red'})` : 'var(--faint)';
  document.getElementById('twin-stage-sub').textContent = typeof stage === 'number' ? stageLabels[stage] : 'governance health';

  // φ history sparkline
  const records = hist?.history || [];
  document.getElementById('twin-epochs-label').textContent = `${records.length} epochs`;
  if (records.length) {
    const phis = records.map(r => r.phi || 0);
    const maxP = Math.max(...phis, 0.001);
    const pstarLine = records[0]?.phi_star || 0.32;
    const bars = phis.map((p, i) => {
      const h = Math.max(4, Math.round((p / maxP) * 80));
      const above = p > pstarLine;
      return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px">
        <div style="font-size:8px;color:var(--faint)">${p.toFixed(2)}</div>
        <div style="width:100%;height:${h}px;border-radius:2px 2px 0 0;background:${above?'var(--red)':'var(--green)'};transition:.3s"></div>
      </div>`;
    }).join('');
    const pstarPct = Math.round((pstarLine / maxP) * 80);
    document.getElementById('twin-phi-chart').innerHTML = `
      <div style="position:relative">
        <div style="display:flex;align-items:flex-end;gap:3px;height:100px;padding:1rem 1rem 0">${bars}</div>
        <div style="padding:.5rem 1rem;display:flex;gap:1rem;font-size:10px;color:var(--faint)">
          <span style="color:var(--green)">■ Below φ* (safe)</span>
          <span style="color:var(--red)">■ Above φ* (HAV regime)</span>
          <span style="margin-left:auto">φ* = ${pstarLine.toFixed(3)}</span>
        </div>
      </div>`;
  }

  // Early warning alerts
  const alerts = warn?.alerts || [];
  if (!alerts.length) {
    document.getElementById('twin-alerts').innerHTML = '<div style="color:var(--green);font-size:13px;padding:.5rem 0">✓ All governance signals nominal</div>';
  } else {
    document.getElementById('twin-alerts').innerHTML = alerts.map(a =>
      `<div style="padding:.625rem 0;border-bottom:1px solid var(--line2);font-size:12px;color:var(--amber);line-height:1.5">${a}</div>`
    ).join('') + `<div style="margin-top:.875rem;font-size:11px;color:var(--faint)">Sim: <code style="color:var(--muted)">${simId.slice(0,12)}...</code></div>`;
  }

  // Capital composition
  if (comp) {
    const total = (comp.n_human||0) + (comp.n_ai_agent||0) + (comp.n_autonomous||0);
    const bar = (n, color, label) => n > 0 ? `
      <div style="margin-bottom:.75rem">
        <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px">
          <span style="color:var(--muted)">${label}</span>
          <span style="color:var(--text);font-weight:600">${n}</span>
        </div>
        <div style="height:6px;background:var(--panel2);border-radius:3px;overflow:hidden">
          <div style="height:100%;width:${Math.round(n/Math.max(1,total)*100)}%;background:${color};border-radius:3px;transition:.4s"></div>
        </div>
      </div>` : '';
    document.getElementById('twin-composition').innerHTML = `
      ${bar(comp.n_human||0, 'var(--green)', '◎ Human')}
      ${bar(comp.n_ai_agent||0, 'var(--blue)', '⬡ AI Agent')}
      ${bar(comp.n_autonomous||0, 'var(--purple)', '◑ Autonomous')}
      <div style="margin-top:.875rem;padding-top:.875rem;border-top:1px solid var(--line);display:flex;justify-content:space-between;font-size:11px">
        <span style="color:var(--faint)">φ effective</span>
        <span style="color:var(--text);font-weight:600">${(comp.phi_effective||0).toFixed(4)}</span>
      </div>`;
  }

  // φ history table
  if (records.length) {
    const rows = [...records].reverse().slice(0, 10).map(r => `<tr>
      <td style="font-size:11px;color:var(--faint)">${r.recorded_at ? r.recorded_at.slice(0,16).replace('T',' ') : '—'}</td>
      <td style="font-weight:600;color:${r.phi > (r.phi_star||0.32) ? 'var(--red)' : 'var(--green)'}">${(r.phi||0).toFixed(4)}</td>
      <td style="color:var(--faint)">${(r.phi_star||0).toFixed(3)}</td>
      <td style="color:var(--green)">${r.mean_hav != null ? r.mean_hav.toFixed(3) : '—'}</td>
      <td style="color:var(--amber)">${r.alignment_gap != null ? r.alignment_gap.toFixed(3) : '—'}</td>
      <td><span class="badge ${r.track2_nudge ? 'meets' : 'filled'}">${r.track2_nudge ? 'ACTIVE' : 'Off'}</span></td>
    </tr>`).join('');
    document.getElementById('twin-history-table').innerHTML = `
      <table class="tbl">
        <thead><tr><th>Time</th><th>φ</th><th>φ*</th><th>Mean HAV</th><th>Align Gap</th><th>Track 2</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }
}

async function runTwinEpochs() {
  const simId = await resolveTwinSimId();
  if (!simId) { toast('No twin — seed demo data first', 'error'); return; }
  const r = await POST(`/twin/sim/${simId}/run`, { epochs: 5 });
  if (r) { toast(`Ran 5 epochs · total ${r.epochs_run}`); loadTwin(); }
}

async function recalibrateTwin() {
  const r = await fetch('/api/v1/twin/orgs/demo-org/calibrate?epochs=5', { method: 'POST' });
  const d = await r.json().catch(() => null);
  if (d?.sim_id) {
    STATE.twinSimId = d.sim_id;
    toast(`Recalibrated · φ = ${d.phi?.toFixed(3)}`);
    loadTwin();
  } else {
    toast('Calibration failed', 'error');
  }
}

async function runScenario() {
  const simId = await resolveTwinSimId();
  if (!simId) { toast('No twin — seed demo data first', 'error'); return; }
  const nAi   = parseInt(document.getElementById('scenario-ai').value) || 0;
  const nAuto = parseInt(document.getElementById('scenario-auto').value) || 0;
  const label = `+${nAi} AI agents, +${nAuto} autonomous`;
  const r = await POST(`/twin/sim/${simId}/scenario`, {
    label, n_ai: nAi, n_autonomous: nAuto, epochs: 10
  });
  const el = document.getElementById('scenario-result');
  if (!r) { el.style.display = 'none'; return; }
  const base = r.baseline?.summary || {};
  const scen = r.scenario_result?.summary || {};
  const phiDelta = ((scen.phi_final||0) - (base.phi_final||0)).toFixed(3);
  const havDelta = ((scen.mean_hav_final||0) - (base.mean_hav_final||0)).toFixed(3);
  const color = phiDelta > 0 ? 'var(--amber)' : 'var(--green)';
  el.style.display = 'block';
  el.innerHTML = `
    <div style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:.875rem;font-size:12px">
      <div style="font-weight:600;color:var(--text);margin-bottom:.625rem">${label}</div>
      <div style="display:flex;gap:1.5rem">
        <div><div style="color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.08em">φ shift</div>
          <div style="font-size:18px;font-family:var(--fh);color:${color}">${phiDelta > 0 ? '+' : ''}${phiDelta}</div></div>
        <div><div style="color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.08em">HAV shift</div>
          <div style="font-size:18px;font-family:var(--fh);color:${havDelta >= 0 ? 'var(--green)' : 'var(--red)'}">${havDelta > 0 ? '+' : ''}${havDelta}</div></div>
        <div><div style="color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.08em">Final φ</div>
          <div style="font-size:18px;font-family:var(--fh)">${(scen.phi_final||0).toFixed(3)}</div></div>
      </div>
      ${scen.phi_final > (r.scenario_result?.params?.phi_star||0.32)
        ? `<div style="margin-top:.75rem;padding:.5rem .75rem;background:var(--amber-d);border:1px solid rgba(229,168,58,.25);border-radius:6px;color:var(--amber);font-size:11px">⚠ Scenario pushes φ above φ*. Switch to HAV regime before deploying.</div>` : ''}
    </div>`;
}

// ─── TIME & ATTENDANCE ────────────────────────────────────────────────────
async function loadTimeAttendance() {
  // Populate employee dropdown
  const sel = document.getElementById('ta-emp-select');
  if (sel.options.length <= 1) {
    DEMO.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.id; opt.textContent = `${e.name} — ${e.title}`;
      sel.appendChild(opt);
    });
  }

  // Org HAV summary
  const summary = await GET('/time-attendance/org-hav-summary?org_id=demo-org&last_days=90');
  document.getElementById('ta-hav').textContent = summary?.mean_hav != null ? summary.mean_hav.toFixed(3) : '—';
  document.getElementById('ta-phi-guard').textContent = summary?.phi_guardian_sessions ?? '—';

  // Sessions
  const data = await GET('/time-attendance/sessions?org_id=demo-org&limit=50');
  const sessions = data?.sessions || [];
  const active = sessions.filter(s => s.state === 'active');
  const completed = sessions.filter(s => s.state === 'completed');

  document.getElementById('ta-sessions').textContent = completed.length;
  document.getElementById('ta-active').textContent = active.length;
  document.getElementById('ta-sessions-label').textContent = `${sessions.length} sessions`;

  if (!sessions.length) {
    document.getElementById('ta-sessions-table').innerHTML = '<div class="empty"><h3>No Sessions</h3><p>Check in an employee above to start tracking HAV</p></div>';
    return;
  }

  const rows = sessions.map(s => {
    const emp = DEMO.find(e => e.id === s.employee_id) || { name: s.employee_id };
    const hav = s.hav_score;
    const isActive = s.state === 'active';
    const checkin = s.checkin_at ? s.checkin_at.slice(11,16) : '—';
    const checkout = s.checkout_at ? s.checkout_at.slice(11,16) : '—';
    const date = s.checkin_at ? s.checkin_at.slice(0,10) : '—';
    return `<tr>
      <td>
        <strong style="color:var(--text)">${emp.name}</strong>
        <div style="font-size:11px;color:var(--faint)">${date} · ${checkin}–${isActive ? 'active' : checkout}</div>
      </td>
      <td>${s.shift_type === 'phi_guardian' ? '<span class="badge phi-guardian">φ-Guardian</span>' : s.shift_type === 'values_custodian' ? '<span class="badge vc">VC</span>' : '<span class="badge open">Standard</span>'}</td>
      <td style="color:var(--blue);font-size:12px">${s.actual_npf != null ? s.actual_npf.toFixed(2) : (s.declared_npf != null ? `~${s.declared_npf.toFixed(2)}` : '—')}</td>
      <td style="color:var(--amber);font-size:12px">${s.srq_score != null ? s.srq_score.toFixed(2) : '—'}</td>
      <td style="color:var(--red);font-size:12px">${s.oc_score != null ? s.oc_score.toFixed(2) : '—'}</td>
      <td>${hav != null ? `<span class="hav-number ${havColor(hav)}">${hav.toFixed(3)}</span>` : isActive ? '<span style="color:var(--faint);font-size:11px">in session</span>' : '—'}</td>
      <td><span class="badge ${isActive ? 'open' : 'filled'}" style="text-transform:capitalize">${s.state}</span></td>
    </tr>`;
  }).join('');

  document.getElementById('ta-sessions-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>Shift</th><th style="color:var(--blue)">NPF</th><th style="color:var(--amber)">SRQ</th><th style="color:var(--red)">OC</th><th>HAV</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function checkin() {
  const empId = document.getElementById('ta-emp-select').value;
  if (!empId) { toast('Select an employee', 'error'); return; }
  const taskType = document.getElementById('ta-task-type').value;
  const npf = parseFloat(document.getElementById('ta-npf').value);
  const body = {
    employee_id: empId, org_id: 'demo-org',
    task_type: taskType, declared_npf: npf,
  };
  if (STATE.twinSimId) body.sim_id = STATE.twinSimId;
  const r = await POST('/time-attendance/checkin', body);
  const el = document.getElementById('ta-checkin-result');
  if (r?.session_id) {
    el.style.display = 'block';
    el.innerHTML = `<div style="color:var(--green);font-weight:600;margin-bottom:.5rem">✓ Checked in</div>
      <div>Session ID: <code style="color:var(--text)">${r.session_id}</code></div>
      <div style="margin-top:.375rem">Shift: <strong>${r.shift_type}</strong> · φ = ${r.phi_at_checkin?.toFixed(3) ?? '—'}</div>
      <div style="margin-top:.375rem;color:var(--faint)">${r.guidance}</div>`;
    document.getElementById('ta-session-id').value = r.session_id;
    toast(`Checked in · ${r.shift_type} shift`);
    setTimeout(() => loadTimeAttendance(), 500);
  } else {
    toast('Check-in failed', 'error');
  }
}

async function checkout() {
  const sessionId = document.getElementById('ta-session-id').value.trim();
  if (!sessionId) { toast('Paste a session ID', 'error'); return; }
  const npf = parseFloat(document.getElementById('ta-actual-npf').value);
  const r = await POST(`/time-attendance/sessions/${sessionId}/checkout`, { actual_npf: npf });
  const el = document.getElementById('ta-checkout-result');
  if (r?.hav_breakdown) {
    const hb = r.hav_breakdown;
    el.style.display = 'block';
    el.innerHTML = `<div style="color:var(--green);font-weight:600;margin-bottom:.5rem">✓ HAV computed</div>
      <div style="display:flex;gap:1.5rem;margin-bottom:.5rem">
        <span><span style="color:var(--blue)">NPF</span> ${hb.npf.toFixed(3)}</span>
        <span><span style="color:var(--amber)">SRQ</span> ${hb.srq.toFixed(3)}</span>
        <span><span style="color:var(--red)">OC</span> ${hb.oc.toFixed(3)}</span>
        <span style="color:var(--green);font-weight:600">HAV ${hb.hav.toFixed(3)}</span>
      </div>
      <div style="color:var(--faint);font-size:11px">${r.total_minutes?.toFixed(0)} min · ${r.phi_context?.note || ''}</div>`;
    toast(`HAV = ${hb.hav.toFixed(3)}`);
    setTimeout(() => loadTimeAttendance(), 500);
  } else {
    toast('Checkout failed — check session ID', 'error');
  }
}

// ─── PAYROLL ─────────────────────────────────────────────────────────────
async function runPayroll() {
  const btn = document.querySelector('#view-payroll .btn-green');
  btn.disabled = true; btn.textContent = 'Running...';

  // Fetch twin's calibrated φ first — r_AP must reflect real org composition
  let realPhi = null;
  try {
    const td = await GET('/twin/orgs/demo-org/role-predictions');
    if (td?.phi != null) realPhi = td.phi;
  } catch(_) {}
  const phi = realPhi ?? (DEMO.reduce((s,e) => s + e.hav, 0) / DEMO.length);
  const r_ap = phi < 0.25 ? 0.05 : phi > 0.75 ? 0.25 : 0.05 + (phi - 0.25) * 0.40;

  const today = '2026-06-23';
  const monthStart = '2026-06-01';

  const contracts = DEMO.map(e => ({
    employee_id: e.id, employee_name: e.name,
    unit_type: 'human', contract_type: 'full_time',
    start_date: '2026-01-01',
    annual_salary: e.salary,
    monthly_token_allocation: e.hav >= 0.65 ? 12000 : 6000,
    token_cost_per_unit: 0.001,
    department: e.dept,
  }));

  const r = await POST('/payroll/run', {
    run_id: 'demo-run-' + Date.now(),
    name: 'June 2026 Payroll',
    date_from: monthStart,
    date_to: today,
    contracts,
    token_consumption: Object.fromEntries(DEMO.map(e => [e.id, e.hav >= 0.65 ? 8400 : 3000])),
  });

  btn.disabled = false; btn.textContent = 'Run Payroll';
  if (!r) { toast('Payroll run failed', 'error'); return; }

  document.getElementById('payroll-headcount').textContent = r.headcount_human || '—';
  document.getElementById('payroll-gross').textContent     = fmt$(r.total_gross || 0);
  document.getElementById('payroll-tokens').textContent    = (r.total_token_allocated || 0).toLocaleString();
  document.getElementById('payroll-huang').textContent     = r.huang_ratio != null ? r.huang_ratio.toFixed(3) : '—';
  document.getElementById('payroll-period').textContent    = `${r.date_from} → ${r.date_to}`;

  const slips = (r.payslips || []).sort((a,b) => (b.gross_pay||0) - (a.gross_pay||0));
  const rows = slips.map(s => {
    const emp  = DEMO.find(e => e.id === s.employee_id) || { name: s.employee_id, title: '', hav: 0, salary: 0 };
    const ap   = r_ap * emp.hav * (emp.salary / 12);   // monthly alignment premium
    const bstar = s.b_star_status;
    return `<tr>
      <td><strong style="color:var(--text)">${emp.name}</strong><div style="font-size:11px;color:var(--faint)">${emp.title}</div></td>
      <td style="color:var(--muted)">${fmt$(s.base_pay || 0)}</td>
      <td style="color:var(--amber);font-size:11px">${fmt$(ap)}<div style="font-size:10px;color:var(--faint)">r_AP=${(r_ap*100).toFixed(0)}%·φ=${phi.toFixed(3)}</div></td>
      <td style="color:var(--blue)">${(s.token_allocation || 0).toLocaleString()}</td>
      <td style="color:var(--green);font-weight:600">${fmt$(s.gross_pay || 0)}</td>
      <td style="color:var(--text)">${fmt$(s.net_pay || 0)}</td>
      <td><span class="badge ${bstar === 'above' ? 'filled' : 'meets'}">${bstar || '—'}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('payroll-table').innerHTML = `
    <div style="margin-bottom:.75rem;padding:.625rem 1rem;background:var(--panel2);border:1px solid var(--line);border-radius:8px;font-size:12px;display:flex;gap:2rem">
      <div><span style="color:var(--faint)">Org φ (twin-calibrated): </span><strong style="color:var(--text)">${phi.toFixed(4)}</strong></div>
      <div><span style="color:var(--faint)">Alignment Premium Rate: </span><strong style="color:var(--amber)">${(r_ap*100).toFixed(1)}%</strong></div>
      <div><span style="color:var(--faint)">Source: </span><span style="color:var(--faint)">${realPhi != null ? 'twin calibration' : 'DEMO average (twin not calibrated)'}</span></div>
    </div>
    <table class="tbl">
      <thead><tr><th>Employee</th><th>Base Pay</th><th>Alignment Premium</th><th>Token Alloc</th><th>Gross</th><th>Net</th><th>B*</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  toast(`Payroll run · ${slips.length} payslips · r_AP=${(r_ap*100).toFixed(1)}% · φ=${phi.toFixed(3)}`);
}

// ─── ONBOARDING ───────────────────────────────────────────────────────────
async function loadOnboarding() {
  // Populate select
  const sel = document.getElementById('ob-emp-select');
  if (sel.options.length <= 1) {
    DEMO.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.id; opt.textContent = `${e.name} — ${e.title}`;
      sel.appendChild(opt);
    });
  }

  const [journeys, summary] = await Promise.all([
    GET('/onboarding/journeys'),
    GET('/onboarding/summary'),
  ]);

  const list = journeys?.journeys || [];
  const sum  = summary || {};

  document.getElementById('ob-total').textContent    = sum.total_journeys ?? list.length;
  document.getElementById('ob-active').textContent   = sum.in_progress ?? list.filter(j => j.state === 'in_progress').length;
  document.getElementById('ob-complete').textContent  = sum.complete ?? list.filter(j => j.state === 'complete').length;
  const h = sum.human_journeys || 0, a = sum.ai_agent_journeys || 0;
  document.getElementById('ob-ratio').textContent    = (h || a) ? `${h}H / ${a}AI` : '—';
  document.getElementById('ob-journeys-count').textContent = `${list.length} journeys`;

  if (!list.length) {
    document.getElementById('ob-table').innerHTML = '<div class="empty" style="padding:2rem"><p>No journeys yet. Seed demo data or start one.</p></div>';
    return;
  }

  const rows = list.slice(0, 20).map(j => {
    const emp = DEMO.find(e => e.id === j.unit_id) || { name: j.unit_name || j.unit_id };
    const pct = j.progress?.required_pct ?? 0;
    const stateColor = { in_progress:'var(--amber)', complete:'var(--green)', planned:'var(--faint)', cancelled:'var(--red)' }[j.state] || 'var(--faint)';
    return `<tr>
      <td><strong style="color:var(--text)">${emp.name}</strong><div style="font-size:11px;color:var(--faint)">${j.journey_type} · ${j.unit_type}</div></td>
      <td>
        <div style="display:flex;align-items:center;gap:6px">
          <div style="flex:1;height:4px;background:var(--panel2);border-radius:2px;overflow:hidden">
            <div style="height:100%;width:${pct}%;background:var(--green);border-radius:2px"></div>
          </div>
          <span style="font-size:11px;color:var(--muted);min-width:28px">${pct.toFixed(0)}%</span>
        </div>
      </td>
      <td style="color:${stateColor};font-size:11px;text-transform:capitalize">${j.state}</td>
      ${j.mutation_trigger ? `<td><span class="badge phi-guardian" style="font-size:9px">${j.mutation_trigger}</span></td>` : '<td style="color:var(--faint);font-size:11px">—</td>'}
    </tr>`;
  }).join('');
  document.getElementById('ob-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>Progress</th><th>State</th><th>Mutation</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function startJourney() {
  const empId = document.getElementById('ob-emp-select').value;
  if (!empId) { toast('Select an employee', 'error'); return; }
  const emp = DEMO.find(e => e.id === empId);
  const journeyType = document.getElementById('ob-journey-type').value;
  const mutation    = document.getElementById('ob-mutation').value;

  const body = {
    unit_id: empId, unit_name: emp?.name || empId,
    unit_type: 'human', journey_type: journeyType,
    start_date: new Date().toISOString().slice(0,10),
    manager_id: 'emp-001',
    role: emp?.title || '', department: emp?.dept || '',
    belief_alignment_at_entry: emp?.hav || 0.5,
  };
  if (mutation) body.mutation_trigger = mutation;

  const r = await POST('/onboarding/journeys', body);
  const el = document.getElementById('ob-start-result');
  if (r?.journey_id) {
    el.style.display = 'block';

    // Compute φ-impact of mutation trigger from twin's current position
    let mutationHTML = '';
    if (mutation) {
      const twinR = await GET('/twin/orgs/demo-org/role-predictions');
      const phi      = twinR?.phi      ?? 0.087;
      const phiStar  = twinR?.phi_star ?? 0.32;
      const n        = 10; // total capital units in demo org
      const impacts = {
        T_probe_entry:   { delta: +(1/n).toFixed(3), dir: 'φ* shifts up', color: 'var(--blue)',  note: 'Outsider injection raises crossover threshold — org must earn its AI fraction.' },
        T_replace_h2a:   { delta: +(1/n).toFixed(3), dir: 'φ increases', color: 'var(--amber)', note: `AI fraction rises to ~${(phi + 1/n).toFixed(3)} — approaching φ*=${phiStar}.` },
        T_replace_a2h:   { delta: -(1/n).toFixed(3), dir: 'φ decreases', color: 'var(--green)', note: `Human capital restored — φ drops to ~${Math.max(0, phi - 1/n).toFixed(3)}.` },
      };
      const imp = impacts[mutation];
      if (imp) {
        mutationHTML = `
          <div style="margin-top:.625rem;padding:.625rem .875rem;background:var(--panel2);border:1px solid var(--line);border-radius:7px">
            <div style="font-size:11px;font-weight:700;color:${imp.color};margin-bottom:3px">${mutation} fired → ${imp.dir} (Δ${imp.delta > 0 ? '+' : ''}${imp.delta})</div>
            <div style="font-size:11px;color:var(--muted)">${imp.note}</div>
            <div style="margin-top:.375rem;font-size:10px;color:var(--faint)">Current φ=${phi.toFixed(3)} · φ*=${phiStar.toFixed(3)} · Click "Sync from T&A" after journeys progress to recalibrate twin.</div>
          </div>`;
      } else {
        mutationHTML = `<div style="margin-top:.375rem;font-size:11px;color:var(--amber)">Mutation trigger: ${mutation}</div>`;
      }
    }

    el.innerHTML = `
      <div style="color:var(--green);font-weight:600;margin-bottom:.375rem">✓ Journey started</div>
      <div style="font-size:12px;color:var(--muted)">ID: <code style="color:var(--text)">${r.journey_id.slice(0,12)}…</code> · ${r.total_tasks} tasks · ${journeyType}</div>
      ${mutationHTML}`;
    toast(`Journey started · ${r.total_tasks} tasks`);
    setTimeout(() => loadOnboarding(), 500);
  } else {
    toast('Failed to start journey', 'error');
  }
}

// ─── ABSENCE ──────────────────────────────────────────────────────────────
async function loadAbsence() {
  const sel = document.getElementById('ab-emp-select');
  if (sel.options.length <= 1) {
    DEMO.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.id; opt.textContent = `${e.name}${e.hav >= 0.70 && e.npf >= 0.65 ? ' ★ VC' : ''}`;
      sel.appendChild(opt);
    });
  }

  const data = await GET('/absence/requests') || { requests: [] };
  const reqs = data.requests || [];

  const pending  = reqs.filter(r => r.status === 'pending');
  const approved = reqs.filter(r => r.status === 'approved');
  const vcReqs   = reqs.filter(r => r.is_vc || r.is_values_custodian);
  const totalHavImpact = reqs.reduce((s, r) => s + (r.hav_impact || 0), 0);

  document.getElementById('ab-pending').textContent    = pending.length;
  document.getElementById('ab-approved').textContent   = approved.length;
  document.getElementById('ab-vc').textContent         = vcReqs.length;
  document.getElementById('ab-hav-impact').textContent = totalHavImpact.toFixed(2);
  document.getElementById('ab-count').textContent      = `${reqs.length} requests`;

  if (!reqs.length) {
    document.getElementById('ab-table').innerHTML = '<div class="empty" style="padding:2rem"><p>No absence requests. Seed data or submit one.</p></div>';
    return;
  }

  const statusColor = { pending:'var(--amber)', approved:'var(--green)', denied:'var(--red)', cancelled:'var(--faint)' };
  const rows = reqs.slice(0, 20).map(r => {
    const emp = DEMO.find(e => e.id === r.employee_id) || { name: r.employee_id };
    return `<tr>
      <td><strong style="color:var(--text)">${emp.name}</strong>${r.is_vc || r.is_values_custodian ? ' <span class="badge vc">VC</span>' : ''}</td>
      <td style="color:var(--faint);font-size:11px;text-transform:capitalize">${r.leave_type}</td>
      <td style="color:var(--faint);font-size:11px">${r.dates || (r.start_date + ' → ' + r.end_date)}</td>
      <td style="color:var(--muted)">${r.days || r.days_requested} days</td>
      <td style="color:var(--amber)">${(r.hav_impact || 0).toFixed(2)}</td>
      <td><span class="badge ${r.status === 'approved' ? 'filled' : r.status === 'pending' ? 'meets' : 'below'}" style="text-transform:capitalize">${r.status}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('ab-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>Employee</th><th>Type</th><th>Dates</th><th>Days</th><th>HAV Impact</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function submitAbsence() {
  const empId = document.getElementById('ab-emp-select').value;
  if (!empId) { toast('Select an employee', 'error'); return; }
  const emp = DEMO.find(e => e.id === empId);
  const r = await POST('/absence/requests', {
    employee_id: empId,
    leave_type:  document.getElementById('ab-leave-type').value,
    start_date:  document.getElementById('ab-start').value,
    end_date:    document.getElementById('ab-end').value,
    days_requested: parseFloat(document.getElementById('ab-days').value),
    mean_hav: emp?.hav || 0.5,
    mean_npf: emp?.npf || 0.5,
    org_k: 4,
  });
  const el = document.getElementById('ab-submit-result');
  if (r?.request_id) {
    el.style.display = 'block';
    const isVC = r.is_values_custodian;
    el.innerHTML = `<div style="color:${isVC?'var(--amber)':'var(--green)'};font-weight:600">${isVC ? '⚠ Values Custodian absence' : '✓ Request submitted'}</div>
      <div>HAV impact: <strong>${(r.hav_impact||0).toFixed(3)}</strong> · φ-coverage impact: <strong>${(r.phi_coverage_impact||0).toFixed(3)}</strong></div>
      ${(r.alerts||[]).map(a => `<div style="margin-top:.375rem;color:var(--amber);font-size:11px">⚠ ${a.message}</div>`).join('')}`;
    toast(`Absence submitted · ${r.days} days`);
    setTimeout(() => loadAbsence(), 500);
  } else {
    toast('Submission failed', 'error');
  }
}

// ─── LEARNING ─────────────────────────────────────────────────────────────
async function loadLearning() {
  const sel = document.getElementById('learn-emp-select');
  if (sel.options.length <= 1) {
    DEMO.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.id; opt.textContent = `${e.name} — HAV ${e.hav.toFixed(3)}`;
      sel.appendChild(opt);
    });
  }
}

async function generateLearningPlan() {
  const empId = document.getElementById('learn-emp-select').value;
  if (!empId) { toast('Select an employee', 'error'); return; }
  const emp    = DEMO.find(e => e.id === empId);
  const regime = document.getElementById('learn-regime').value;

  const btn = document.querySelector('#view-learning .btn-green');
  btn.disabled = true; btn.textContent = 'Generating...';

  // Build human state from DEMO data
  const belief = Array.from({ length: 12 }, (_, i) => {
    if (i < 6) return emp.npf;   // NPF-influenced beliefs
    return emp.oc;               // OC-influenced beliefs
  });

  const r = await POST('/learning/plan', {
    human: {
      employee_id:   emp.id,
      employee_name: emp.name,
      role:          emp.title,
      department:    emp.dept,
      fitness_score: emp.hav,
      activation_score: emp.hav * 0.95,
      belief_alignment: emp.hav * 0.9,
      belief_vector: belief,
      primary_regime: regime,
      hcm_level: emp.hav,
      self_directed_rate: emp.npf > 0.70 ? 0.04 : 0.01,
      token_utilization_pct: emp.hav >= 0.65 ? 70.0 : 40.0,
      agents_worked_with: ['claude-agent-01'],
      completed_learning_this_epoch: [],
    },
    agents: [{
      agent_id: 'claude-agent-01', agent_name: 'Platform AI',
      model_version: 'claude-opus-4-8',
      fitness_score: 0.82, mandate_coherence: 0.91,
      rag_depth: 3, rag_activations_per_epoch: 14,
      failure_flag: false, failure_count_this_epoch: 0,
      decisions_participated: 20, decisions_resolved: 17, resolution_rate: 0.85,
      task_domains: ['engineering', 'analysis'],
      avg_tokens_per_task: 420,
    }],
    org_template_vector: Array(12).fill(0.6),
    epoch: 1,
  });

  btn.disabled = false; btn.textContent = 'Generate Plan →';

  if (!r) { toast('Learning plan failed', 'error'); return; }

  document.getElementById('learn-emp-label').textContent = `${emp.name} · Regime ${regime} · HAV ${emp.hav.toFixed(3)}`;

  const opportunities = r.opportunities || r.learning_opportunities || [];
  const rec = r.recommended_regime || r.regime_recommendation || '';
  const budget = r.token_budget_recommendation;
  const summary = r.one_line_summary || '';
  const theme = r.primary_theme || '';
  const replRisk = r.replacement_risk_score != null ? r.replacement_risk_score : null;
  const huangGap = r.huang_learning_gap != null ? r.huang_learning_gap : null;
  const immCount = r.immediate_count ?? 0;
  const weekCount = r.this_week_count ?? 0;
  const totalHrs = r.total_hours ?? 0;

  const oppHTML = opportunities.slice(0, 8).map(o => {
    const impColor = { high:'var(--green)', medium:'var(--amber)', low:'var(--faint)' }[o.expected_impact || o.impact || 'low'] || 'var(--faint)';
    return `<div style="padding:.625rem 0;border-bottom:1px solid var(--line2)">
      <div style="display:flex;align-items:flex-start;gap:.75rem">
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600;color:var(--text)">${o.title || o.description || o.name || 'Learning opportunity'}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px;line-height:1.4">${o.rationale || o.why || ''}</div>
        </div>
        <span style="font-size:10px;font-weight:700;text-transform:uppercase;color:${impColor};white-space:nowrap">${o.expected_impact || o.impact || ''}</span>
      </div>
    </div>`;
  }).join('');

  document.getElementById('learn-plan-body').innerHTML = `
    ${summary ? `<div style="margin-bottom:.75rem;font-size:13px;font-weight:600;color:var(--text)">${summary}</div>` : ''}
    ${theme ? `<div style="margin-bottom:1rem;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">${theme}</div>` : ''}
    <div style="margin-bottom:1rem;display:flex;gap:1.5rem;flex-wrap:wrap;font-size:12px">
      ${replRisk != null ? `<div><span style="color:var(--faint)">Replacement risk: </span><strong style="color:${replRisk>0.6?'var(--red)':replRisk>0.35?'var(--amber)':'var(--green)'}">${(replRisk*100).toFixed(0)}%</strong></div>` : ''}
      ${huangGap != null ? `<div><span style="color:var(--faint)">Huang gap: </span><strong style="color:var(--blue)">${huangGap.toFixed(3)}</strong></div>` : ''}
      ${immCount ? `<div><span style="color:var(--faint)">Immediate: </span><strong>${immCount}</strong></div>` : ''}
      ${weekCount ? `<div><span style="color:var(--faint)">This week: </span><strong>${weekCount}</strong></div>` : ''}
      ${totalHrs ? `<div><span style="color:var(--faint)">Total: </span><strong>${totalHrs}h</strong></div>` : ''}
      ${budget ? `<div><span style="color:var(--faint)">Token budget: </span><strong style="color:var(--green)">${budget.toLocaleString()}</strong></div>` : ''}
    </div>
    ${rec ? `<div style="margin-bottom:1rem;padding:.75rem 1rem;background:var(--panel2);border:1px solid var(--line);border-radius:8px;font-size:12px;color:var(--muted)">${rec}</div>` : ''}
    <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin-bottom:.5rem">Learning Opportunities</div>
    ${oppHTML || '<div style="color:var(--faint);font-size:12px;padding:.5rem 0">No specific opportunities generated — employee may already be at target for this regime.</div>'}`;
  toast(`Plan generated · ${opportunities.length} opportunities`);
}

// ─── IMPORT ──────────────────────────────────────────────────────────────

let _impPeriodCount = 0;

function loadImport() { /* view is static, nothing to prefetch */ }

function addImportPeriod() {
  const list = document.getElementById('imp-periods-list');
  const idx  = _impPeriodCount++;
  const row  = document.createElement('div');
  row.id = `imp-period-${idx}`;
  row.style.cssText = 'display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr auto;gap:.5rem;margin-bottom:.5rem;align-items:center';
  row.innerHTML = `
    <input placeholder="Label (e.g. 2025-Q4)" id="ipl-${idx}" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--fb)">
    <input type="number" step="0.01" min="0" max="1" placeholder="NPF" id="ipn-${idx}" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--fb)">
    <input type="number" step="0.01" min="0" max="1" placeholder="SRQ" id="ips-${idx}" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--fb)">
    <input type="number" step="0.01" min="0" max="1" placeholder="OC"  id="ipo-${idx}" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--fb)">
    <input type="number" placeholder="Hrs" id="iph-${idx}" value="160" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--fb)">
    <button onclick="document.getElementById('imp-period-${idx}').remove()" style="background:none;border:none;color:var(--faint);cursor:pointer;font-size:14px;padding:0 4px">✕</button>`;
  list.appendChild(row);
}

async function runQuickImport() {
  const eid  = document.getElementById('imp-eid').value.trim();
  const name = document.getElementById('imp-name').value.trim();
  if (!eid || !name) { toast('Employee ID and name required', 'error'); return; }

  const periods = [];
  let i = 0;
  while (document.getElementById(`imp-period-${i}`) || i < _impPeriodCount) {
    const row = document.getElementById(`imp-period-${i}`);
    if (row) {
      const npf = parseFloat(document.getElementById(`ipn-${i}`).value);
      const srq = parseFloat(document.getElementById(`ips-${i}`).value);
      const oc  = parseFloat(document.getElementById(`ipo-${i}`).value);
      if (!isNaN(npf)) {
        periods.push({
          label: document.getElementById(`ipl-${i}`).value || `period-${i}`,
          npf, srq: isNaN(srq) ? npf * 0.9 : srq,
          oc:  isNaN(oc)  ? npf * 0.8 : oc,
          hours: parseFloat(document.getElementById(`iph-${i}`).value) || 160,
        });
      }
    }
    i++;
    if (i > 50) break;
  }

  if (!periods.length) { toast('Add at least one period', 'error'); return; }

  const btn = document.querySelector('#view-import .two-col .btn-green');
  btn.disabled = true; btn.textContent = 'Importing...';

  const payload = {
    source: document.getElementById('imp-source').value,
    employees: [{
      employee_id:   eid,
      employee_name: name,
      role:          document.getElementById('imp-role').value || 'Unknown',
      department:    document.getElementById('imp-dept').value || 'Unknown',
      periods,
    }],
  };

  const r = await _bootFetch(payload);
  btn.disabled = false; btn.textContent = 'Import → Bootstrap HAV →';
  _showImportResult(r);
}

async function runJsonImport() {
  const raw = document.getElementById('imp-json').value.trim();
  if (!raw) { toast('Paste a JSON payload first', 'error'); return; }
  let payload;
  try { payload = JSON.parse(raw); }
  catch(e) { toast('Invalid JSON: ' + e.message, 'error'); return; }

  const btns = document.querySelectorAll('#view-import .btn-green');
  btns.forEach(b => { b.disabled = true; });
  const r = await _bootFetch(payload);
  btns.forEach(b => { b.disabled = false; });
  _showImportResult(r);
}

async function _bootFetch(payload) {
  try {
    const res = await fetch('/import/hav-bootstrap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch(e) {
    return { error: e.message };
  }
}

function _showImportResult(r) {
  const el = document.getElementById('imp-result');
  if (!r || r.error) {
    el.innerHTML = `<div style="margin-top:.75rem;padding:.75rem 1rem;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;font-size:12px;color:#ef4444">Import failed: ${r?.error || 'unknown error'}</div>`;
    toast('Import failed', 'error');
    return;
  }
  const agg = r.aggregate_result || {};
  el.innerHTML = `
    <div style="margin-top:.75rem;padding:.875rem 1rem;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);border-radius:8px;font-size:12px">
      <div style="font-weight:700;color:var(--green);margin-bottom:.5rem">Bootstrap complete</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:.375rem;color:var(--muted)">
        <div>Employees registered: <strong style="color:var(--text)">${r.employees_registered ?? '—'}</strong></div>
        <div>Sessions imported: <strong style="color:var(--text)">${r.imported_sessions?.inserted ?? '—'}</strong></div>
        <div>Reviews created: <strong style="color:var(--text)">${agg.employees_updated ?? '—'}</strong></div>
        <div>Cycle: <strong style="color:var(--text)">${agg.cycle_id ? agg.cycle_id.slice(0,8) + '…' : '—'}</strong></div>
        <div>Source: <strong style="color:var(--text)">${r.source ?? '—'}</strong></div>
        <div>Org: <strong style="color:var(--text)">${r.org_id ?? '—'}</strong></div>
      </div>
      <div style="margin-top:.75rem;font-size:11px;color:var(--faint)">Performance reviews are live — click Performance → Sync from T&A to see them.</div>
    </div>`;
  toast(`Bootstrapped · ${r.employees_registered} employees · ${agg.employees_updated} reviews`);
}

// ─── WORKFORCE PLANNING ──────────────────────────────────────────────────

async function loadWorkforce() {
  const main = document.getElementById('view-workforce');
  main.innerHTML = `<div style="padding:2rem;color:var(--faint);font-size:13px">Loading Workforce Planning…</div>`;

  let twin = {}, plans = [], scenarios = [], decisions = [];
  try {
    const [tR, scR] = await Promise.all([
      GET('/twin/orgs/demo-org/role-predictions'),
      GET('/workforce-planning/phi-scenarios?org_id=demo-org'),
    ]);
    twin      = tR || {};
    scenarios = scR?.scenarios || [];

    // Get latest plan and its decisions
    const pR  = await GET('/workforce-planning/plans?org_id=demo-org');
    plans     = pR?.plans || [];
    if (plans.length) {
      const latest = plans[0];
      const dR = await GET(`/workforce-planning/plans/${latest.id}/decisions`);
      decisions = dR?.decisions || [];
    }
  } catch(e) {}

  const phi      = twin.phi ?? 0.087;
  const phiStar  = twin.phi_star ?? 0.32;
  const aboveStar = phi > phiStar;
  const stage    = twin.stage ?? 0;

  const hiCount  = decisions.filter(d => d.recommended === 'hire_human').length;
  const aiCount  = decisions.filter(d => d.recommended === 'deploy_ai').length;
  const hybCount = decisions.filter(d => d.recommended === 'hybrid').length;

  const decColor = r => r === 'hire_human' ? 'var(--green)' : r === 'deploy_ai' ? 'var(--amber)' : 'var(--blue)';
  const decLabel = r => r === 'hire_human' ? 'Hire Human' : r === 'deploy_ai' ? 'Deploy AI' : 'Hybrid';

  const renderDecRow = d => {
    const havNeeded = d.hav_required >= 0.65;
    const vDiff     = (d.v_net_human || 0) - (d.v_net_ai || 0);
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:.6rem .75rem">
        <div style="font-size:12px;font-weight:600;color:var(--text)">${d.role_title}</div>
        <div style="font-size:10px;color:var(--faint)">${d.department || '—'} · ×${d.headcount || 1}</div>
      </td>
      <td style="padding:.6rem .75rem;font-size:11px;color:var(--muted)">
        HAV≥${(d.hav_required||0).toFixed(2)} / NPF≥${(d.npf_required||0).toFixed(2)}
        ${havNeeded ? '<div style="font-size:10px;color:var(--green)">→ HAV required: human only</div>' : ''}
      </td>
      <td style="padding:.6rem .75rem;font-size:11px;text-align:right">
        <div style="color:var(--text)">${fmt$(d.v_net_human||0)}</div>
        <div style="font-size:10px;color:var(--faint)">V_net(human)</div>
      </td>
      <td style="padding:.6rem .75rem;font-size:11px;text-align:right">
        <div style="color:var(--text)">${fmt$(d.v_net_ai||0)}</div>
        <div style="font-size:10px;color:var(--faint)">V_net(AI)</div>
      </td>
      <td style="padding:.6rem .75rem;font-size:11px;text-align:right;color:${vDiff >= 0 ? 'var(--green)' : 'var(--amber)'}">
        ${vDiff >= 0 ? '+' : ''}${fmt$(vDiff)}
      </td>
      <td style="padding:.6rem .75rem;text-align:center">
        <span style="padding:3px 9px;border-radius:5px;font-size:10px;font-weight:700;
          background:${d.recommended==='hire_human'?'rgba(16,185,129,.15)':d.recommended==='deploy_ai'?'rgba(251,191,36,.12)':'rgba(59,130,246,.12)'};
          color:${decColor(d.recommended)}">${decLabel(d.recommended)}</span>
      </td>
    </tr>`;
  };

  const renderScRow = s => {
    const delta    = s.delta_phi > 0 ? `+${s.delta_phi.toFixed(3)}` : s.delta_phi.toFixed(3);
    const projected = (s.projected_phi || 0).toFixed(3);
    const abv       = s.stays_above_crossover;
    const nudge     = (s.nudge_acceleration || 0).toFixed(4);
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:.55rem .75rem;font-size:12px;color:var(--text)">${s.scenario_name}</td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted);text-align:center">${delta}</td>
      <td style="padding:.55rem .75rem;font-size:11px;font-weight:700;text-align:center;color:${abv?'var(--amber)':'var(--green)'}">
        ${projected}
      </td>
      <td style="padding:.55rem .75rem;font-size:10px;text-align:center">
        <span style="padding:2px 7px;border-radius:4px;font-weight:600;
          background:${abv?'rgba(251,191,36,.12)':'rgba(16,185,129,.12)'};
          color:${abv?'var(--amber)':'var(--green)'}">
          ${abv ? 'Above φ*' : 'Below φ*'}
        </span>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted);text-align:right">${nudge}</td>
    </tr>`;
  };

  main.innerHTML = `
<div style="padding:1.5rem 2rem 2rem">
  <div style="display:flex;align-items:baseline;gap:1rem;margin-bottom:1.5rem">
    <h2 style="margin:0;font-size:17px;font-weight:600">Workforce Planning</h2>
    <span style="font-size:11px;color:var(--faint)">Hire Human vs Deploy AI — V_net cost model + φ trajectory</span>
  </div>

  <!-- KPI strip -->
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:1rem;margin-bottom:1.5rem">
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Current φ</div>
      <div style="font-size:22px;font-weight:700;color:${aboveStar?'var(--amber)':'var(--green)'}">${phi.toFixed(3)}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">AI fraction of org</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">φ* Crossover</div>
      <div style="font-size:22px;font-weight:700;color:var(--text)">${phiStar.toFixed(3)}</div>
      <div style="font-size:10px;color:${aboveStar?'var(--amber)':'var(--faint)'};margin-top:.2rem">${aboveStar?'⚠ Above crossover':'Below crossover'}</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Hire Human</div>
      <div style="font-size:22px;font-weight:700;color:var(--green)">${hiCount}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">of ${decisions.length} roles</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Deploy AI</div>
      <div style="font-size:22px;font-weight:700;color:var(--amber)">${aiCount}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">of ${decisions.length} roles</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Hybrid</div>
      <div style="font-size:22px;font-weight:700;color:var(--blue)">${hybCount}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">split deployment</div>
    </div>
  </div>

  <!-- Differentiator callout -->
  <div style="margin-bottom:1.5rem;padding:.85rem 1.1rem;background:rgba(160,123,229,.1);border:1px solid rgba(160,123,229,.25);border-radius:8px;font-size:12px;color:var(--muted)">
    <strong style="color:#A07BE5">HCAM differentiator:</strong>
    Workday headcount planning is based on FTEs and cost. Tessera computes <strong>V_net(human)</strong> and <strong>V_net(AI)</strong> per role using the φ-crossover theorem —
    when φ &gt; φ*, the Alignment Premium shifts, HAV-governance costs rise, and deploying more AI without high-NPF humans causes belief drift.
    <strong>This is the only platform that mathematically models when to hire vs deploy.</strong>
  </div>

  <!-- Role decision matrix -->
  <div class="card" style="padding:0;overflow:hidden;margin-bottom:1.5rem">
    <div style="padding:1rem 1.25rem;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:13px;font-weight:600">Role Decision Matrix — ${plans[0]?.name || 'FY2026 H2'}</span>
      <button onclick="runRoleDecision()" style="font-size:11px;padding:4px 12px;border-radius:5px;border:1px solid var(--green);color:var(--green);background:var(--green-d);cursor:pointer;font-family:var(--fb)">+ Evaluate Role</button>
    </div>
    ${decisions.length === 0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No role decisions yet — seed demo data first.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">ROLE</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">HAV / NPF NEEDED</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">V_NET HUMAN</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">V_NET AI</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">DELTA</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">RECOMMENDATION</th>
          </tr></thead>
          <tbody>${decisions.map(renderDecRow).join('')}</tbody>
        </table></div>`}
  </div>

  <!-- φ Scenario Modeler -->
  <div class="card" style="padding:0;overflow:hidden;margin-bottom:1.5rem">
    <div style="padding:1rem 1.25rem;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:13px;font-weight:600">φ Scenario Modeler</span>
      <button onclick="runPhiScenario()" style="font-size:11px;padding:4px 12px;border-radius:5px;border:1px solid var(--blue);color:var(--blue);background:rgba(59,130,246,.1);cursor:pointer;font-family:var(--fb)">+ Run Scenario</button>
    </div>
    ${scenarios.length === 0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No scenarios yet.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">SCENARIO</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">Δφ</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">PROJECTED φ</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">VS φ*</th>
            <th style="padding:.5rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">NUDGE ACCEL</th>
          </tr></thead>
          <tbody>${scenarios.map(renderScRow).join('')}</tbody>
        </table></div>`}
    <div style="padding:.75rem 1.25rem;border-top:1px solid var(--line);font-size:10px;color:var(--faint)">
      Nudge acceleration = (1 − mean NPF) × φ. Higher values = AI beliefs dominate faster without human correction.
    </div>
  </div>

  <!-- Quick evaluate form -->
  <div class="card" style="padding:1.25rem" id="wf-eval-panel" style="display:none">
    <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:1rem">Quick Role Evaluation</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:.75rem;margin-bottom:.75rem">
      <input id="wf-role" placeholder="Role title" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-dept" placeholder="Department" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-hav" type="number" step="0.01" min="0" max="1" placeholder="HAV required (0–1)" value="0.60" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-npf" type="number" step="0.01" min="0" max="1" placeholder="NPF required (0–1)" value="0.55" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:.75rem;margin-bottom:.75rem">
      <input id="wf-hcomp" type="number" placeholder="Human salary ($)" value="95000" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-hval"  type="number" placeholder="Human value delivery ($)" value="120000" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-acost" type="number" placeholder="AI deployment cost ($)" value="20000" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="wf-aval"  type="number" placeholder="AI value delivery ($)" value="90000" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
    </div>
    <div style="display:flex;gap:.75rem;align-items:center">
      <button onclick="submitRoleDecision()" style="font-size:12px;padding:7px 20px;border-radius:6px;border:none;background:var(--green);color:#000;cursor:pointer;font-family:var(--fb);font-weight:600">Evaluate</button>
      <span id="wf-eval-status" style="font-size:11px;color:var(--faint)"></span>
    </div>
    <div id="wf-eval-result" style="margin-top:.75rem"></div>
  </div>
</div>`;
}

function runRoleDecision() {
  const panel = document.getElementById('wf-eval-panel');
  if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

async function runPhiScenario() {
  const name  = prompt('Scenario name (e.g. "Add 2 AI agents"):');
  if (!name) return;
  const delta = parseFloat(prompt('Δφ — how much does AI fraction change? (e.g. +0.04 or -0.03):', '0.04'));
  if (isNaN(delta)) return;

  try {
    const twin = await GET('/twin/orgs/demo-org/role-predictions') || {};
    const phi  = twin.phi ?? 0.087;
    const r = await fetch('/api/v1/workforce-planning/phi-scenarios', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_id: 'demo-org', scenario_name: name, base_phi: phi, delta_phi: delta, org_k: 4, mean_npf: 0.60 })
    });
    if (!r.ok) throw new Error(await r.text());
    toast(`Scenario saved · φ ${phi.toFixed(3)} → ${(phi + delta).toFixed(3)}`);
    loadWorkforce();
  } catch(e) { toast(`Error: ${e.message}`); }
}

async function submitRoleDecision() {
  const role  = document.getElementById('wf-role').value.trim();
  const dept  = document.getElementById('wf-dept').value.trim();
  const hav   = parseFloat(document.getElementById('wf-hav').value) || 0;
  const npf   = parseFloat(document.getElementById('wf-npf').value) || 0;
  const hcomp = parseFloat(document.getElementById('wf-hcomp').value) || 95000;
  const hval  = parseFloat(document.getElementById('wf-hval').value) || 120000;
  const acost = parseFloat(document.getElementById('wf-acost').value) || 20000;
  const aval  = parseFloat(document.getElementById('wf-aval').value) || 90000;
  const status = document.getElementById('wf-eval-status');
  const res    = document.getElementById('wf-eval-result');

  if (!role) { status.textContent = 'Role title required.'; return; }
  status.textContent = 'Evaluating…';

  try {
    // Get or create plan
    const pR = await GET('/workforce-planning/plans?org_id=demo-org');
    const plans = pR?.plans || [];
    let planId = plans[0]?.id;
    if (!planId) {
      const twin = await GET('/twin/orgs/demo-org/role-predictions') || {};
      const pr   = await fetch('/api/v1/workforce-planning/plans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ org_id: 'demo-org', name: 'Ad-hoc Plan', period: '2026-H2', current_phi: twin.phi ?? 0.087, org_k: 4 })
      });
      planId = (await pr.json()).plan_id;
    }

    const r = await fetch('/api/v1/workforce-planning/role-decisions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan_id: planId, role_title: role, department: dept,
        hav_required: hav, npf_required: npf,
        human_fitness: 0.80, human_value_delivery: hval, human_comp_cost: hcomp,
        human_gov_cost: 5000, ai_deployment_value: aval, ai_deployment_cost: acost,
        ai_oversight_cost: Math.round(acost * 0.6), probe_cost: 500, edge_count: 5, headcount: 1
      })
    });
    const d = await r.json();
    const vDiff = (d.v_net_human - d.v_net_ai);
    const recColor = d.recommendation === 'hire_human' ? 'var(--green)' : d.recommendation === 'deploy_ai' ? 'var(--amber)' : 'var(--blue)';
    res.innerHTML = `<div style="padding:.75rem 1rem;background:var(--panel2);border:1px solid var(--line);border-radius:6px;font-size:12px">
      <strong style="color:${recColor}">${d.recommendation === 'hire_human' ? 'Hire Human' : d.recommendation === 'deploy_ai' ? 'Deploy AI' : 'Hybrid'}</strong>
      — ${d.rationale}<br>
      <span style="color:var(--faint);font-size:11px">V_net(human)=${fmt$(d.v_net_human)} · V_net(AI)=${fmt$(d.v_net_ai)} · Δ=${fmt$(vDiff)}</span>
    </div>`;
    status.textContent = '✓ Saved';
    setTimeout(() => loadWorkforce(), 1000);
  } catch(e) {
    status.style.color = 'var(--red)';
    status.textContent = `Error: ${e.message}`;
  }
}

// ─── ITSM ────────────────────────────────────────────────────────────────

const ITSM_ORG_ID = '00000000-0000-0000-0000-000000000001';
const ITSM_ORG = ITSM_ORG_ID; // alias kept for modal code

const PRIO_COLOR = { P1: 'var(--red)', P2: 'var(--amber)', P3: 'var(--blue)', P4: 'var(--faint)' };
const PRIO_BG    = { P1: 'rgba(239,68,68,.12)', P2: 'rgba(251,191,36,.10)', P3: 'rgba(59,130,246,.10)', P4: 'rgba(255,255,255,.04)' };

let _itsmActiveTab = 'incidents';

async function loadITSM(tab) {
  if (tab) _itsmActiveTab = tab;
  const main = document.getElementById('view-itsm');

  // Shell with tab bar always renders first; tab content fills #itsm-tab-body
  main.innerHTML = _itsmShell();
  _itsmSetActiveTab(_itsmActiveTab);
  await _itsmLoadTab(_itsmActiveTab);
}

function _itsmShell() {
  const tabs = [
    { id: 'incidents', label: 'Incidents' },
    { id: 'changes',   label: 'Changes' },
    { id: 'problems',  label: 'Problems' },
    { id: 'cmdb',      label: 'CMDB' },
    { id: 'reports',   label: 'Reports' },
  ];
  const tabBar = tabs.map(t =>
    `<button id="itsm-tab-${t.id}" onclick="loadITSM('${t.id}')"
      style="padding:.5rem 1.1rem;font-size:12px;font-family:var(--fb);border:none;cursor:pointer;
        border-bottom:2px solid transparent;background:none;color:var(--muted);transition:.12s"
    >${t.label}</button>`
  ).join('');
  return `
<div style="padding:1.5rem 2rem 0">
  <div style="display:flex;align-items:baseline;gap:1rem;margin-bottom:1.25rem">
    <h2 style="margin:0;font-size:17px;font-weight:600">ITSM</h2>
    <span style="font-size:11px;color:var(--faint)">ServiceNow-class · HAV-connected</span>
  </div>
  <div style="border-bottom:1px solid var(--line);margin-bottom:1.5rem">${tabBar}</div>
</div>
<div id="itsm-tab-body" style="padding:0 2rem 2rem">
  <div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading…</div>
</div>
<!-- Resolve Modal -->
<div id="itsm-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;align-items:center;justify-content:center">
  <div style="background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1.75rem;width:480px;max-width:95vw">
    <div style="font-size:14px;font-weight:600;margin-bottom:.25rem" id="modal-title">Resolve Ticket</div>
    <div style="font-size:11px;color:var(--faint);margin-bottom:1.25rem" id="modal-subtitle"></div>
    <div style="margin-bottom:1rem">
      <div class="field-label">Resolution Notes</div>
      <textarea id="modal-resolution" rows="3" placeholder="Describe how the incident was resolved…"
        style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:8px 10px;color:var(--text);font-size:12px;font-family:var(--fb);resize:vertical;box-sizing:border-box"></textarea>
    </div>
    <div style="margin-bottom:1rem">
      <div class="field-label">Resolved By (email)</div>
      <input id="modal-resolver" placeholder="resolver@demo.com"
        style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:8px 10px;color:var(--text);font-size:12px;font-family:var(--fb);box-sizing:border-box">
    </div>
    <div id="modal-srq-preview" style="margin-bottom:1rem;padding:.75rem 1rem;background:var(--panel2);border:1px solid var(--line);border-radius:6px;font-size:12px;color:var(--muted)"></div>
    <div style="display:flex;gap:.75rem;justify-content:flex-end">
      <button onclick="document.getElementById('itsm-modal').style.display='none'"
        style="font-size:12px;padding:7px 18px;border-radius:6px;border:1px solid var(--line);background:none;color:var(--muted);cursor:pointer;font-family:var(--fb)">Cancel</button>
      <button id="modal-submit-btn" onclick="submitResolve()"
        style="font-size:12px;padding:7px 18px;border-radius:6px;border:none;background:var(--green);color:#000;cursor:pointer;font-family:var(--fb);font-weight:600">Resolve + Log SRQ</button>
    </div>
    <div id="modal-result" style="margin-top:.75rem;font-size:11px;color:var(--faint)"></div>
  </div>
</div>`;
}

function _itsmSetActiveTab(tab) {
  ['incidents','changes','problems','cmdb','reports'].forEach(t => {
    const el = document.getElementById(`itsm-tab-${t}`);
    if (!el) return;
    if (t === tab) {
      el.style.color = 'var(--green)';
      el.style.borderBottomColor = 'var(--green)';
    } else {
      el.style.color = 'var(--muted)';
      el.style.borderBottomColor = 'transparent';
    }
  });
}

async function _itsmLoadTab(tab) {
  const body = document.getElementById('itsm-tab-body');
  if (!body) return;
  if (tab === 'incidents')  await _itsmIncidents(body);
  else if (tab === 'changes')   await _itsmChanges(body);
  else if (tab === 'problems')  await _itsmProblems(body);
  else if (tab === 'cmdb')      await _itsmCMDB(body);
  else if (tab === 'reports')   await _itsmReports(body);
}

// ── Tab: Incidents ────────────────────────────────────────────────────────
async function _itsmIncidents(body) {
  body.innerHTML = '<div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading incidents…</div>';

  const ITSM_ORG = ITSM_ORG_ID;

  let tickets = [], atRisk = [], breaches = [], summary = {};
  try {
    const [tR, arR, brR, smR] = await Promise.all([
      GET(`/itsm/tickets?org_id=${ITSM_ORG}&status=open&limit=50`),
      GET(`/itsm/sla/at-risk?org_id=${ITSM_ORG}`),
      GET(`/itsm/sla/breaches?org_id=${ITSM_ORG}`),
      GET(`/itsm/reports/summary?org_id=${ITSM_ORG}`),
    ]);
    tickets  = tR?.tickets  || [];
    atRisk   = arR?.at_risk || [];
    breaches = brR?.breaches || [];
    summary  = smR || {};
  } catch(e) {}

  const openCount    = summary.open_tickets ?? tickets.length;
  const p1p2Count    = tickets.filter(t => t.priority === 'P1' || t.priority === 'P2').length;
  const atRiskCount  = atRisk.length;
  const breachCount  = breaches.length;

  const kpiColor = (v, warn, crit) => v >= crit ? 'var(--red)' : v >= warn ? 'var(--amber)' : 'var(--green)';

  const renderTicketRow = (t) => {
    const prio  = t.priority || 'P3';
    const pc    = PRIO_COLOR[prio] || 'var(--faint)';
    const pb    = PRIO_BG[prio]   || 'rgba(255,255,255,.04)';
    const isAR  = atRisk.some(r => r.ticket_id === t.id);
    const isBR  = breaches.some(r => r.ticket_id === t.id);
    const slaTag = isBR
      ? `<span style="font-size:9px;background:rgba(239,68,68,.18);color:var(--red);border-radius:4px;padding:1px 6px;font-weight:700">BREACHED</span>`
      : isAR
        ? `<span style="font-size:9px;background:rgba(251,191,36,.18);color:var(--amber);border-radius:4px;padding:1px 6px;font-weight:700">AT RISK</span>`
        : '';
    const id = t.id || '';
    const slaResolveAt = t.sla_resolve_by || '';
    return `<tr style="border-bottom:1px solid var(--line)" id="row-${id}">
      <td style="padding:.55rem .75rem;font-size:11px">
        <span style="display:inline-block;padding:2px 7px;border-radius:4px;font-weight:700;font-size:10px;background:${pb};color:${pc}">${prio}</span>
      </td>
      <td style="padding:.55rem .75rem;font-size:12px;color:var(--text);max-width:260px">
        <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.title || '—'}</div>
        <div style="font-size:10px;color:var(--faint);margin-top:2px">${t.ticket_type?.toUpperCase() || 'INC'} · ${t.category || '—'} ${slaTag}</div>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.assignee_email || '—'}</td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.team || '—'}</td>
      <td style="padding:.55rem .75rem;font-size:11px;text-align:center">
        <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;background:rgba(255,255,255,.06);color:var(--muted)">${t.status || 'open'}</span>
      </td>
      <td style="padding:.55rem .75rem;text-align:right">
        <button onclick="openResolveModal('${id}','${prio}','${slaResolveAt}','${(t.title||'').replace(/'/g,"\\'")}','${t.assignee_email||''}')"
          style="font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid var(--green);color:var(--green);background:var(--green-d);cursor:pointer;font-family:var(--fb)">
          Resolve + Log SRQ
        </button>
      </td>
    </tr>`;
  };

  const byPrio = (a, b) => {
    const o = {P1:0,P2:1,P3:2,P4:3};
    return (o[a.priority]||9) - (o[b.priority]||9);
  };
  const sorted = [...tickets].sort(byPrio);

  const incOnly = tickets.filter(t => t.ticket_type === 'incident' || !t.ticket_type);
  const sortedInc = [...incOnly].sort((a,b) => ({P1:0,P2:1,P3:2,P4:3}[a.priority]||9) - ({P1:0,P2:1,P3:2,P4:3}[b.priority]||9));

  const renderRow = t => {
    const prio = t.priority||'P3', pc = PRIO_COLOR[prio]||'var(--faint)', pb = PRIO_BG[prio]||'rgba(255,255,255,.04)';
    const isAR = atRisk.some(r=>r.id===t.id), isBR = breaches.some(r=>r.id===t.id);
    const slaTag = isBR ? `<span style="font-size:9px;background:rgba(239,68,68,.18);color:var(--red);border-radius:4px;padding:1px 6px;font-weight:700">BREACHED</span>`
      : isAR ? `<span style="font-size:9px;background:rgba(251,191,36,.18);color:var(--amber);border-radius:4px;padding:1px 6px;font-weight:700">AT RISK</span>` : '';
    const id = t.id||'';
    return `<tr style="border-bottom:1px solid var(--line)" id="row-${id}">
      <td style="padding:.55rem .75rem"><span style="padding:2px 7px;border-radius:4px;font-weight:700;font-size:10px;background:${pb};color:${pc}">${prio}</span></td>
      <td style="padding:.55rem .75rem;font-size:12px;color:var(--text);max-width:260px">
        <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.title||'—'}</div>
        <div style="font-size:10px;color:var(--faint);margin-top:2px">${t.category||'—'} ${slaTag}</div>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.assignee_email||'—'}</td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.team||'—'}</td>
      <td style="padding:.55rem .75rem;text-align:right">
        <button onclick="openResolveModal('${id}','${prio}','${t.sla_resolve_at||''}','${(t.title||'').replace(/'/g,"\\'")}','${t.assignee_email||''}')"
          style="font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid var(--green);color:var(--green);background:var(--green-d);cursor:pointer;font-family:var(--fb)">
          Resolve + Log SRQ
        </button>
      </td>
    </tr>`;
  };

  const p12 = tickets.filter(t=>t.priority==='P1'||t.priority==='P2').length;

  body.innerHTML = `
  <!-- KPI strip -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.25rem">
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Open Tickets</div>
      <div style="font-size:26px;font-weight:700;color:${kpiColor(openCount,3,6)}">${openCount}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">all types</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">P1/P2 Critical</div>
      <div style="font-size:26px;font-weight:700;color:${kpiColor(p12,1,2)}">${p12}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">high-urgency</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">SLA At-Risk</div>
      <div style="font-size:26px;font-weight:700;color:${kpiColor(atRisk.length,1,3)}">${atRisk.length}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">breach window</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">SLA Breached</div>
      <div style="font-size:26px;font-weight:700;color:${breaches.length>0?'var(--red)':'var(--green)'}">${breaches.length}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">${breaches.length>0?'SRQ penalty':'all clear'}</div>
    </div>
  </div>
  <!-- HCAM callout -->
  <div style="margin-bottom:1.25rem;padding:.8rem 1rem;background:var(--green-d);border:1px solid rgba(16,185,129,.25);border-radius:8px;font-size:12px;color:var(--muted)">
    <strong style="color:var(--green)">HCAM differentiator:</strong>
    Resolving a ticket logs an <strong>SRQ event</strong> — resolution speed vs SLA becomes a quality score fed into the resolver's HAV measurement.
    ServiceNow tracks tickets closed; Tessera tracks <em>how well</em> the human solved it.
  </div>
  <!-- Create form -->
  <div class="card" style="padding:1.25rem;margin-bottom:1.25rem">
    <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">Create Incident</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.6rem;margin-bottom:.6rem">
      <input id="itsm-title" placeholder="Title" style="grid-column:1/3;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <select id="itsm-prio" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
        <option value="P1">P1 Critical</option><option value="P2">P2 High</option>
        <option value="P3" selected>P3 Medium</option><option value="P4">P4 Low</option>
      </select>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.6rem;margin-bottom:.6rem">
      <input id="itsm-cat" placeholder="Category" value="IT Operations" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="itsm-reporter" placeholder="Reporter email" value="maya.chen@demo.com" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="itsm-assignee" placeholder="Assignee email" value="priya.sharma@demo.com" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
    </div>
    <textarea id="itsm-desc" rows="2" placeholder="Description…" style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb);resize:vertical;box-sizing:border-box;margin-bottom:.6rem"></textarea>
    <button onclick="createITSMTicket()" style="font-size:12px;padding:6px 18px;border-radius:5px;border:none;background:var(--blue);color:#fff;cursor:pointer;font-family:var(--fb)">Create</button>
    <span id="itsm-create-status" style="font-size:11px;color:var(--faint);margin-left:.75rem"></span>
  </div>
  <!-- Incidents table -->
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:.85rem 1.25rem;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:13px;font-weight:600">Open Incidents</span>
      <button onclick="loadITSM('incidents')" style="font-size:11px;padding:3px 10px;border-radius:5px;border:1px solid var(--line);background:none;color:var(--muted);cursor:pointer;font-family:var(--fb)">↻</button>
    </div>
    ${sorted.length===0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No open incidents.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">PRI</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">TITLE</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">ASSIGNEE</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">TEAM</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">ACTION</th>
          </tr></thead>
          <tbody>${sorted.map(renderRow).join('')}</tbody>
        </table></div>`}
  </div>`;
}

// ── Tab: Changes (CAB) ────────────────────────────────────────────────────
async function _itsmChanges(body) {
  body.innerHTML = '<div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading changes…</div>';
  const O = ITSM_ORG_ID;
  let changes = [];
  try {
    const r = await GET(`/itsm/tickets?org_id=${O}&ticket_type=change&limit=30`);
    changes = r?.tickets || [];
  } catch(e) {}

  const renderChange = t => {
    const prio = t.priority||'P3', pc = PRIO_COLOR[prio]||'var(--faint)', pb = PRIO_BG[prio]||'rgba(255,255,255,.04)';
    const id = t.id||'';
    return `<tr style="border-bottom:1px solid var(--line)" id="chg-row-${id}">
      <td style="padding:.55rem .75rem"><span style="padding:2px 7px;border-radius:4px;font-weight:700;font-size:10px;background:${pb};color:${pc}">${prio}</span></td>
      <td style="padding:.55rem .75rem;font-size:12px;color:var(--text)">
        <div>${t.title||'—'}</div>
        <div style="font-size:10px;color:var(--faint);margin-top:2px">${t.number||''} · ${t.category||'—'} · ${t.team||'—'}</div>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.reporter_email||'—'}</td>
      <td style="padding:.55rem .75rem;text-align:center">
        <span style="padding:2px 8px;border-radius:4px;font-size:10px;background:rgba(255,255,255,.06);color:var(--muted)">${t.status||'open'}</span>
      </td>
      <td style="padding:.55rem .75rem;text-align:right;display:flex;gap:.4rem;justify-content:flex-end">
        <button onclick="cabApprove('${id}','maya.chen@demo.com','approved')"
          style="font-size:11px;padding:3px 9px;border-radius:4px;border:1px solid var(--green);color:var(--green);background:var(--green-d);cursor:pointer;font-family:var(--fb)">CAB Approve</button>
        <button onclick="cabApprove('${id}','maya.chen@demo.com','rejected')"
          style="font-size:11px;padding:3px 9px;border-radius:4px;border:1px solid var(--red);color:var(--red);background:rgba(239,68,68,.08);cursor:pointer;font-family:var(--fb)">Reject</button>
      </td>
    </tr>`;
  };

  body.innerHTML = `
  <div style="margin-bottom:1.25rem;padding:.8rem 1rem;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);border-radius:8px;font-size:12px;color:var(--muted)">
    <strong style="color:var(--blue)">Change Advisory Board (CAB):</strong>
    All CHG tickets require CAB approval. Rejecting a change blocks it from proceeding.
    AI agent deployments (T_replace_h2a) must pass CAB + HAV governance review.
  </div>
  <!-- Create CHG form -->
  <div class="card" style="padding:1.25rem;margin-bottom:1.25rem">
    <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">Submit Change Request</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-bottom:.6rem">
      <input id="chg-title" placeholder="Change title" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="chg-cat" placeholder="Category (e.g. AI Deployment)" value="Platform Config" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
    </div>
    <textarea id="chg-desc" rows="2" placeholder="Describe the change and business justification…" style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb);resize:vertical;box-sizing:border-box;margin-bottom:.6rem"></textarea>
    <button onclick="createChange()" style="font-size:12px;padding:6px 18px;border-radius:5px;border:none;background:var(--blue);color:#fff;cursor:pointer;font-family:var(--fb)">Submit CHG</button>
    <span id="chg-status" style="font-size:11px;color:var(--faint);margin-left:.75rem"></span>
  </div>
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:.85rem 1.25rem;border-bottom:1px solid var(--line)"><span style="font-size:13px;font-weight:600">Change Requests</span></div>
    ${changes.length===0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No change requests.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">PRI</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">CHANGE</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">REQUESTOR</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">STATUS</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">CAB</th>
          </tr></thead>
          <tbody>${changes.map(renderChange).join('')}</tbody>
        </table></div>`}
  </div>`;
}

// ── Tab: Problems ─────────────────────────────────────────────────────────
async function _itsmProblems(body) {
  body.innerHTML = '<div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading problems…</div>';
  const O = ITSM_ORG_ID;
  let problems = [], incidents = [];
  try {
    const [pR, iR] = await Promise.all([
      GET(`/itsm/tickets?org_id=${O}&ticket_type=problem&limit=20`),
      GET(`/itsm/tickets?org_id=${O}&ticket_type=incident&status=open&limit=50`),
    ]);
    problems  = pR?.tickets || [];
    incidents = iR?.tickets || [];
  } catch(e) {}

  const renderProblem = t => {
    const id = t.id||'';
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:.55rem .75rem;font-size:12px;color:var(--text)">
        <div style="font-weight:600">${t.title||'—'}</div>
        <div style="font-size:10px;color:var(--faint);margin-top:2px">${t.number||''} · ${t.category||'—'}</div>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${t.assignee_email||'—'}</td>
      <td style="padding:.55rem .75rem;text-align:center">
        <span style="padding:2px 8px;border-radius:4px;font-size:10px;background:rgba(255,255,255,.06);color:var(--muted)">${t.status||'open'}</span>
      </td>
      <td style="padding:.55rem .75rem;text-align:right">
        <button onclick="resolveTicketDirect('${id}','Root cause identified and remediated','priya.sharma@demo.com')"
          style="font-size:11px;padding:3px 9px;border-radius:4px;border:1px solid var(--blue);color:var(--blue);background:rgba(59,130,246,.1);cursor:pointer;font-family:var(--fb)">Close RCA</button>
      </td>
    </tr>`;
  };

  body.innerHTML = `
  <div style="margin-bottom:1.25rem;padding:.8rem 1rem;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:8px;font-size:12px;color:var(--muted)">
    <strong style="color:var(--amber)">Problem Management:</strong>
    PRB records root causes behind recurring incidents.
    Resolving a Problem closes the SRQ loop and prevents future AI-agent latency issues from generating repeated SRQ penalties.
  </div>
  <!-- Recurring incidents summary -->
  ${incidents.length > 0 ? `
  <div class="card" style="padding:1rem;margin-bottom:1.25rem">
    <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">Open Incidents (${incidents.length}) — Promote to Problem</div>
    <div style="display:flex;flex-direction:column;gap:.4rem">
      ${incidents.slice(0,5).map(i=>`
        <div style="display:flex;align-items:center;justify-content:space-between;padding:.4rem .6rem;background:var(--bg);border-radius:5px;border:1px solid var(--line)">
          <span style="font-size:12px;color:var(--text)">${i.title}</span>
          <button onclick="createProblem('${(i.title||'').replace(/'/g,"\\'")} — Root Cause','${i.category||'IT'}')"
            style="font-size:10px;padding:2px 8px;border-radius:4px;border:1px solid var(--amber);color:var(--amber);background:rgba(251,191,36,.08);cursor:pointer;font-family:var(--fb)">→ PRB</button>
        </div>`).join('')}
    </div>
  </div>` : ''}
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:.85rem 1.25rem;border-bottom:1px solid var(--line)"><span style="font-size:13px;font-weight:600">Problem Records</span></div>
    ${problems.length===0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No problem records. Promote recurring incidents above to create one.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">PROBLEM</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">OWNER</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">STATUS</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">ACTION</th>
          </tr></thead>
          <tbody>${problems.map(renderProblem).join('')}</tbody>
        </table></div>`}
  </div>`;
}

// ── Tab: CMDB ─────────────────────────────────────────────────────────────
async function _itsmCMDB(body) {
  body.innerHTML = '<div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading CMDB…</div>';
  const O = ITSM_ORG_ID;
  let cis = [];
  try {
    const r = await GET(`/itsm/cmdb?org_id=${O}`);
    cis = r?.items || r?.cis || [];
  } catch(e) {}

  const typeColor = { service:'var(--blue)', server:'var(--green)', network:'var(--amber)', software:'#A07BE5', other:'var(--faint)' };

  const renderCI = ci => {
    const tc = typeColor[ci.ci_type]||'var(--faint)';
    const isAI = ci.name?.toLowerCase().includes('agent') || ci.name?.toLowerCase().includes('claude');
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:.55rem .75rem">
        <div style="font-size:12px;font-weight:600;color:var(--text)">${ci.name||'—'}${isAI?'<span style="font-size:9px;margin-left:.4rem;padding:1px 5px;background:rgba(160,123,229,.15);color:#A07BE5;border-radius:3px">AI Agent</span>':''}</div>
        <div style="font-size:10px;color:var(--faint);margin-top:2px">${ci.description||''}</div>
      </td>
      <td style="padding:.55rem .75rem">
        <span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;color:${tc};background:${tc.replace(')',',0.1)').replace('var(--','rgba(').replace(')',',0.1)')||'rgba(255,255,255,.06)'}">${ci.ci_type||'other'}</span>
      </td>
      <td style="padding:.55rem .75rem;font-size:11px;color:var(--muted)">${ci.owner_email||'—'}</td>
      <td style="padding:.55rem .75rem;text-align:center">
        <span style="padding:2px 8px;border-radius:4px;font-size:10px;background:${ci.status==='active'?'rgba(16,185,129,.12)':'rgba(255,255,255,.06)'};color:${ci.status==='active'?'var(--green)':'var(--faint)'}">${ci.status||'—'}</span>
      </td>
    </tr>`;
  };

  body.innerHTML = `
  <div style="margin-bottom:1.25rem;padding:.8rem 1rem;background:rgba(160,123,229,.1);border:1px solid rgba(160,123,229,.25);border-radius:8px;font-size:12px;color:var(--muted)">
    <strong style="color:#A07BE5">HCAM differentiator:</strong>
    AI agents are registered here as capital assets alongside servers and services.
    Tessera is the <em>only</em> platform that tracks AI agents and humans in the same capital registry — enabling true cost-benefit comparison via V_net.
  </div>
  <!-- Register CI form -->
  <div class="card" style="padding:1.25rem;margin-bottom:1.25rem">
    <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">Register CI / AI Agent</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:.6rem;margin-bottom:.6rem">
      <input id="ci-name"  placeholder="Name (e.g. claude-agent-03)" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <select id="ci-type" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
        <option value="service" selected>Service / AI Agent</option>
        <option value="server">Server</option>
        <option value="network">Network</option>
        <option value="software">Software</option>
        <option value="other">Other</option>
      </select>
      <input id="ci-owner" placeholder="Owner email" value="maya.chen@demo.com" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
      <input id="ci-desc"  placeholder="Description" style="background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;color:var(--text);font-size:12px;font-family:var(--fb)">
    </div>
    <button onclick="registerCI()" style="font-size:12px;padding:6px 18px;border-radius:5px;border:none;background:var(--blue);color:#fff;cursor:pointer;font-family:var(--fb)">Register</button>
    <span id="ci-status" style="font-size:11px;color:var(--faint);margin-left:.75rem"></span>
  </div>
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:.85rem 1.25rem;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:13px;font-weight:600">Configuration Items (${cis.length})</span>
      <span style="font-size:10px;color:var(--faint)">${cis.filter(c=>c.name?.toLowerCase().includes('agent')||c.name?.toLowerCase().includes('claude')).length} AI agents · ${cis.filter(c=>c.status==='active').length} active</span>
    </div>
    ${cis.length===0
      ? `<div style="padding:2rem;text-align:center;color:var(--faint);font-size:12px">No CIs registered. Seed demo data to populate.</div>`
      : `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--line)">
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">NAME</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">TYPE</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">OWNER</th>
            <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:center">STATUS</th>
          </tr></thead>
          <tbody>${cis.map(renderCI).join('')}</tbody>
        </table></div>`}
  </div>`;
}

// ── Tab: Reports ──────────────────────────────────────────────────────────
async function _itsmReports(body) {
  body.innerHTML = '<div style="color:var(--faint);font-size:13px;padding:1rem 0">Loading reports…</div>';
  const O = ITSM_ORG_ID;
  let summary = {}, sla = {}, team = [];
  try {
    const [sR, slR, tR] = await Promise.all([
      GET(`/itsm/reports/summary?org_id=${O}`),
      GET(`/itsm/reports/sla?org_id=${O}`),
      GET(`/itsm/reports/team?org_id=${O}`),
    ]);
    summary = sR || {};
    sla     = slR || {};
    team    = tR?.teams || [];
  } catch(e) {}

  const slaColor = pct => pct >= 95 ? 'var(--green)' : pct >= 80 ? 'var(--amber)' : 'var(--red)';
  const slaPct   = sla.sla_compliance_pct ?? 0;

  body.innerHTML = `
  <!-- SLA compliance ring + stats -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.25rem">
    <div class="card" style="padding:1rem;text-align:center">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">SLA Compliance</div>
      <div style="font-size:32px;font-weight:800;color:${slaColor(slaPct)}">${slaPct.toFixed(1)}%</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">resolved within SLA</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Total Resolved</div>
      <div style="font-size:26px;font-weight:700;color:var(--green)">${sla.total_resolved??0}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">${sla.within_sla??0} within SLA</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Currently Breached</div>
      <div style="font-size:26px;font-weight:700;color:${(sla.currently_breached??0)>0?'var(--red)':'var(--green)'}">${sla.currently_breached??0}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">SRQ penalty active</div>
    </div>
    <div class="card" style="padding:1rem">
      <div style="font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.35rem">Open P1 Count</div>
      <div style="font-size:26px;font-weight:700;color:${(summary.open_p1??0)>0?'var(--red)':'var(--green)'}">${summary.open_p1??0}</div>
      <div style="font-size:10px;color:var(--faint);margin-top:.2rem">critical incidents</div>
    </div>
  </div>
  <!-- By type + by priority -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.25rem">
    <div class="card" style="padding:1.1rem">
      <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">By Type</div>
      ${Object.entries(summary.by_type||{}).map(([k,v])=>`
        <div style="display:flex;justify-content:space-between;margin-bottom:.4rem">
          <span style="font-size:12px;color:var(--muted);text-transform:capitalize">${k}</span>
          <span style="font-size:12px;font-weight:600;color:var(--text)">${v}</span>
        </div>`).join('')||'<div style="color:var(--faint);font-size:12px">No data</div>'}
    </div>
    <div class="card" style="padding:1.1rem">
      <div style="font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.08em;text-transform:uppercase;margin-bottom:.75rem">By Priority</div>
      ${Object.entries(summary.by_priority||{}).sort().map(([k,v])=>`
        <div style="display:flex;justify-content:space-between;margin-bottom:.4rem">
          <span style="font-size:12px;font-weight:600;color:${PRIO_COLOR[k]||'var(--faint)'}">${k}</span>
          <span style="font-size:12px;color:var(--text)">${v}</span>
        </div>`).join('')||'<div style="color:var(--faint);font-size:12px">No data</div>'}
    </div>
  </div>
  <!-- Team workload -->
  ${team.length > 0 ? `
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:.85rem 1.25rem;border-bottom:1px solid var(--line)"><span style="font-size:13px;font-weight:600">Team Workload</span></div>
    <div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--line)">
        <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:left">TEAM</th>
        <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">OPEN</th>
        <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">OPEN P1</th>
        <th style="padding:.45rem .75rem;font-size:10px;color:var(--faint);font-weight:600;letter-spacing:.06em;text-align:right">RESOLVED</th>
      </tr></thead>
      <tbody>
        ${team.map(t=>`<tr style="border-bottom:1px solid var(--line)">
          <td style="padding:.5rem .75rem;font-size:12px;color:var(--text)">${t.team||'—'}</td>
          <td style="padding:.5rem .75rem;font-size:12px;color:var(--muted);text-align:right">${t.open_count??0}</td>
          <td style="padding:.5rem .75rem;font-size:12px;text-align:right;color:${(t.open_p1??0)>0?'var(--red)':'var(--faint)'}">${t.open_p1??0}</td>
          <td style="padding:.5rem .75rem;font-size:12px;color:var(--green);text-align:right">${t.resolved_count??0}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>
  </div>` : ''}`;
}

// ── ITSM action helpers ───────────────────────────────────────────────────
async function cabApprove(ticketId, approverEmail, decision) {
  try {
    const r = await fetch(`/api/v1/itsm/tickets/${ticketId}/approvals`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ approver_email: approverEmail, role: 'CAB Member' })
    });
    const app = await r.json();
    if (app.approval_id) {
      await fetch(`/api/v1/itsm/approvals/${app.approval_id}`, {
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ decision, comment: `CAB ${decision} via Tessera ITSM` })
      });
    }
    toast(`CHG ${decision} by CAB`);
    const row = document.getElementById(`chg-row-${ticketId}`);
    if (row) row.style.opacity = '0.4';
  } catch(e) { toast(`Error: ${e.message}`); }
}

async function createChange() {
  const title  = document.getElementById('chg-title')?.value?.trim();
  const cat    = document.getElementById('chg-cat')?.value?.trim() || 'Platform Config';
  const desc   = document.getElementById('chg-desc')?.value?.trim();
  const status = document.getElementById('chg-status');
  if (!title) { if(status) status.textContent='Title required.'; return; }
  if(status) status.textContent='Creating…';
  try {
    const r = await fetch('/api/v1/itsm/tickets', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ org_id: ITSM_ORG_ID, ticket_type:'change', title, description:desc||title, priority:'P4', category:cat, reporter_email:'maya.chen@demo.com', team:'Platform' })
    });
    const d = await r.json();
    if(status) status.textContent = `Created ${d.number||'✓'}`;
    setTimeout(() => loadITSM('changes'), 800);
  } catch(e) { if(status) status.textContent=`Error: ${e.message}`; }
}

async function createProblem(title, category) {
  try {
    await fetch('/api/v1/itsm/tickets', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ org_id: ITSM_ORG_ID, ticket_type:'problem', title, description: 'Root cause analysis required.', priority:'P3', category: category||'IT', reporter_email:'priya.sharma@demo.com', team:'Platform' })
    });
    toast(`PRB created: ${title.slice(0,40)}`);
    setTimeout(() => loadITSM('problems'), 600);
  } catch(e) { toast(`Error: ${e.message}`); }
}

async function resolveTicketDirect(ticketId, resolution, resolvedBy) {
  try {
    await fetch(`/api/v1/itsm/tickets/${ticketId}/resolve`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ resolution, resolved_by: resolvedBy })
    });
    toast('Problem record closed');
    loadITSM('problems');
  } catch(e) { toast(`Error: ${e.message}`); }
}

async function registerCI() {
  const name  = document.getElementById('ci-name')?.value?.trim();
  const type  = document.getElementById('ci-type')?.value || 'service';
  const owner = document.getElementById('ci-owner')?.value?.trim();
  const desc  = document.getElementById('ci-desc')?.value?.trim();
  const status = document.getElementById('ci-status');
  if (!name) { if(status) status.textContent='Name required.'; return; }
  if(status) status.textContent='Registering…';
  try {
    const r = await fetch('/api/v1/itsm/cmdb', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ org_id: ITSM_ORG_ID, name, ci_type: type, owner_email: owner||'maya.chen@demo.com', description: desc||name, status:'active' })
    });
    const d = await r.json();
    if(status) status.textContent = d.id ? `Registered ✓` : 'Done';
    document.getElementById('ci-name').value='';
    document.getElementById('ci-desc').value='';
    setTimeout(() => loadITSM('cmdb'), 600);
  } catch(e) { if(status) status.textContent=`Error: ${e.message}`; }
}

let _itsmResolveCtx = {};

function openResolveModal(ticketId, priority, slaResolveAt, title, assignee) {
  _itsmResolveCtx = { ticketId, priority, slaResolveAt };
  document.getElementById('modal-title').textContent = `Resolve: ${title}`;

  // Compute expected SRQ based on current time vs SLA deadline
  let srqEstimate = 0.70;
  let srqNote = 'SRQ will be computed on submission.';
  if (slaResolveAt) {
    const now   = Date.now();
    const slaMs = new Date(slaResolveAt).getTime();
    const slaHoursTotal = { P1: 4, P2: 8, P3: 24, P4: 72 }[priority] || 24;
    const createdEst = slaMs - slaHoursTotal * 3600000;
    const elapsed = (now - createdEst) / 3600000;
    srqEstimate = Math.max(0.10, Math.min(0.95, 1 - elapsed / slaHoursTotal));
    const pct = (srqEstimate * 100).toFixed(0);
    const now_before = now < slaMs;
    srqNote = now_before
      ? `Resolving before SLA deadline → SRQ ≈ <strong style="color:var(--green)">${pct}%</strong>`
      : `SLA already breached → SRQ ≈ <strong style="color:var(--red)">${pct}%</strong> (penalised)`;
  }
  document.getElementById('modal-subtitle').textContent = `[${priority}] · SLA: ${slaResolveAt ? new Date(slaResolveAt).toLocaleString() : '—'}`;
  document.getElementById('modal-srq-preview').innerHTML =
    `<strong>Expected SRQ:</strong> ${srqNote}<br>
     <span style="color:var(--faint)">This score will feed into the resolver's HAV measurement via a T&amp;A session import.</span>`;
  document.getElementById('modal-resolver').value = assignee || '';
  document.getElementById('modal-resolution').value = '';
  document.getElementById('modal-result').textContent = '';
  document.getElementById('itsm-modal').style.display = 'flex';
}

async function submitResolve() {
  const { ticketId, priority, slaResolveAt } = _itsmResolveCtx;
  const resolution = document.getElementById('modal-resolution').value.trim();
  const resolver   = document.getElementById('modal-resolver').value.trim();
  if (!resolution) { document.getElementById('modal-result').textContent = 'Enter resolution notes.'; return; }
  if (!resolver)   { document.getElementById('modal-result').textContent = 'Enter resolver email.'; return; }

  document.getElementById('modal-submit-btn').textContent = 'Resolving…';
  document.getElementById('modal-submit-btn').disabled = true;

  try {
    // 1. Resolve the ticket
    const rr = await fetch(`/api/v1/itsm/tickets/${ticketId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution, resolved_by: resolver })
    });
    if (!rr.ok) throw new Error(`Resolve failed: ${rr.status}`);

    // 2. Compute SRQ from resolution time vs SLA
    const slaHoursTotal = { P1: 4, P2: 8, P3: 24, P4: 72 }[priority] || 24;
    let srqScore = 0.70;
    if (slaResolveAt) {
      const now = Date.now();
      const slaMs = new Date(slaResolveAt).getTime();
      const createdEst = slaMs - slaHoursTotal * 3600000;
      const elapsed = (now - createdEst) / 3600000;
      srqScore = Math.max(0.10, Math.min(0.95, 1 - elapsed / slaHoursTotal));
    }

    // 3. Derive employee_id from resolver email (demo mapping)
    const EMAIL_TO_EMP = {
      'maya.chen@demo.com':    'emp-001',
      'james.okonkwo@demo.com':'emp-002',
      'sofia.reyes@demo.com':  'emp-003',
      'alex.mercer@demo.com':  'emp-004',
      'jordan.park@demo.com':  'emp-005',
      'sam.williams@demo.com': 'emp-006',
      'priya.sharma@demo.com': 'emp-007',
      'marcus.lee@demo.com':   'emp-008',
      'emma.wilson@demo.com':  'emp-009',
      'david.kim@demo.com':    'emp-010',
    };
    const empId = EMAIL_TO_EMP[resolver.toLowerCase()] || 'emp-007';

    // 4. Import a T&A session with this SRQ score — the HCAM bridge
    const now   = new Date();
    const cin   = new Date(now.getTime() - slaHoursTotal * 3600000 * 0.5).toISOString();
    const cout  = now.toISOString();
    const npf   = Math.min(0.95, srqScore * 0.85 + 0.10); // SRQ-informed NPF

    const sr = await fetch('/api/v1/time-attendance/sessions/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sessions: [{
          employee_id: empId,
          org_id: 'demo-org',
          checkin_at: cin,
          checkout_at: cout,
          actual_npf: npf,
          srq_score: srqScore,
          oc_score: 0.50,
          task_type: 'srq_resolution',
          source: `itsm:${ticketId}`,
          notes: `[${priority}] SLA resolution · SRQ=${srqScore.toFixed(2)}`
        }],
        phi_star_default: 0.32
      })
    });

    const sd = sr.ok ? await sr.json() : null;
    const havScore = (0.50 * npf + 0.30 * srqScore + 0.20 * 0.50).toFixed(3);

    document.getElementById('modal-result').innerHTML =
      `<div style="padding:.6rem .8rem;background:var(--green-d);border:1px solid rgba(16,185,129,.3);border-radius:6px;color:var(--green)">
        ✓ Resolved · SRQ=${srqScore.toFixed(2)} · NPF=${npf.toFixed(2)} · HAV contribution=${havScore}
        ${sd ? `<br><span style="font-size:10px;color:var(--muted)">T&A session imported · Run "Sync from T&A" in Performance to update reviews.</span>` : ''}
      </div>`;
    document.getElementById('modal-submit-btn').textContent = 'Done';

    // Fade out the row
    const row = document.getElementById(`row-${ticketId}`);
    if (row) { row.style.opacity = '0.35'; row.style.pointerEvents = 'none'; }

    toast(`Resolved · SRQ=${srqScore.toFixed(2)} logged to ${resolver}`);
  } catch(err) {
    document.getElementById('modal-result').textContent = `Error: ${err.message}`;
    document.getElementById('modal-submit-btn').textContent = 'Resolve + Log SRQ';
    document.getElementById('modal-submit-btn').disabled = false;
  }
}

async function createITSMTicket() {
  const title    = document.getElementById('itsm-title').value.trim();
  const priority = document.getElementById('itsm-prio').value;
  const category = document.getElementById('itsm-cat').value.trim();
  const reporter = document.getElementById('itsm-reporter').value.trim();
  const assignee = document.getElementById('itsm-assignee').value.trim();
  const desc     = document.getElementById('itsm-desc').value.trim();
  const status   = document.getElementById('itsm-create-status');

  if (!title) { status.textContent = 'Title is required.'; return; }
  status.textContent = 'Creating…';
  try {
    const r = await fetch('/api/v1/itsm/tickets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_id: ITSM_ORG,
        ticket_type: 'incident',
        title,
        description: desc || title,
        priority,
        category: category || 'IT Operations',
        reporter_email: reporter || 'admin@demo.com',
        assignee_email: assignee || undefined,
        team: 'Operations'
      })
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    status.textContent = `Created ${d.ticket_id || d.id || '✓'}`;
    document.getElementById('itsm-title').value = '';
    document.getElementById('itsm-desc').value  = '';
    setTimeout(() => loadITSM(), 800);
  } catch(e) {
    status.style.color = 'var(--red)';
    status.textContent = `Error: ${e.message}`;
  }
}

// ─── HEALTH CHECK ────────────────────────────────────────────────────────
async function checkHealth() {
  const txt = document.getElementById('health-txt');
  try {
    const r = await fetch('/health');
    const h = await r.json();
    if (h?.status === 'healthy') {
      txt.textContent = '27 services healthy';
      txt.style.color = 'var(--faint)';
    } else if (h?.status === 'degraded') {
      const down = Object.entries(h.services||{}).filter(([,v]) => v !== 'up').length;
      txt.textContent = `${down} services degraded`;
      txt.style.color = 'var(--amber)';
    } else {
      txt.textContent = 'Checking services...';
    }
  } catch(e) {
    txt.textContent = 'Gateway unreachable';
    txt.style.color = 'var(--red)';
  }
}


// ─── AGENT REGISTRY ──────────────────────────────────────────────────────

const AGENT_ORG = 'market360';
const DEPT_META = {
  customers:    { icon: '◎', color: 'var(--blue)',   label: 'Customers' },
  sales:        { icon: '◈', color: 'var(--green)',  label: 'Sales' },
  planning:     { icon: '◐', color: 'var(--amber)',  label: 'Planning' },
  fulfillment:  { icon: '⊟', color: 'var(--purple)', label: 'Fulfillment' },
};
const STATUS_STYLE = {
  active:   { color: 'var(--green)',  bg: 'var(--green-d)',  label: 'Active' },
  retiring: { color: 'var(--amber)',  bg: 'var(--amber-d)',  label: 'Retiring' },
  retired:  { color: 'var(--faint)',  bg: 'rgba(255,255,255,.04)', label: 'Retired' },
  paused:   { color: 'var(--blue)',   bg: 'var(--blue-d)',   label: 'Paused' },
};
const FW_COLORS = {
  langgraph: 'var(--blue)', crewai: 'var(--purple)', autogen: 'var(--amber)', custom: 'var(--faint)'
};

let _agentsActiveTab = 'all';
let _retireAgentId = null;
let _retireAgentName = '';
let _retirePreview = null;

async function loadAgents(tab) {
  if (tab) _agentsActiveTab = tab;
  const main = document.getElementById('view-agents');
  main.innerHTML = `
    <div style="padding:2rem">
      <div class="ph">
        <div class="ph-left">
          <h1 style="font-family:var(--fh);font-size:26px;font-weight:400">Agent Registry</h1>
          <p style="font-size:12px;color:var(--muted);margin-top:3px">Market360 — AI agent lifecycle: onboard · monitor · retire</p>
        </div>
        <div style="display:flex;gap:.75rem">
          <button class="btn btn-outline" onclick="loadAgents()">↺ Refresh</button>
          <button class="btn btn-green" onclick="openOnboardWizard()">+ Onboard Agent</button>
        </div>
      </div>
      <div id="agents-body"><div style="color:var(--faint);font-size:13px">Loading…</div></div>
    </div>
    ${_agentOnboardModal()}
    ${_agentRetireModal()}`;
  await _renderAgents();
}

async function _renderAgents() {
  const body = document.getElementById('agents-body');
  let data;
  try {
    data = await GET(`/agent-factory/agents?org_id=${AGENT_ORG}`);
  } catch(e) {
    body.innerHTML = `<div style="color:var(--red);font-size:13px">Could not reach agent registry. Seed demo data first.</div>`;
    return;
  }

  const agents = data.agents || [];
  const phi = data.org_phi_from_agents ?? 0;
  const phiStar = data.phi_star ?? 0.32;
  const above = data.above_crossover;

  // KPI strip
  const kpis = [
    { label: 'Total Agents', val: data.total ?? 0, sub: 'registered', color: '' },
    { label: 'Active', val: data.active ?? 0, sub: 'running pipelines', color: 'green' },
    { label: 'Retiring', val: data.retiring ?? 0, sub: 'in wind-down', color: data.retiring ? 'amber' : '' },
    { label: 'φ from Agents', val: (phi * 100).toFixed(1) + '%', sub: `φ* = ${(phiStar * 100).toFixed(0)}%`, color: above ? 'red' : 'green' },
  ];

  let html = `<div class="kpi-grid" style="margin-bottom:1.5rem">
    ${kpis.map(k => `<div class="kpi-card">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-val ${k.color}">${k.val}</div>
      <div class="kpi-sub">${k.sub}</div>
    </div>`).join('')}
  </div>`;

  // φ crossover warning
  if (above) {
    html += `<div style="background:rgba(229,80,74,.08);border:1px solid rgba(229,80,74,.2);border-radius:10px;padding:.875rem 1rem;margin-bottom:1.25rem;display:flex;align-items:center;gap:.875rem">
      <span style="font-size:18px;color:var(--red)">⚠</span>
      <div>
        <div style="font-size:13px;font-weight:600;color:var(--red)">φ above crossover (${(phi*100).toFixed(1)}% > φ*=${(phiStar*100).toFixed(0)}%)</div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px">Standard management models are suboptimal. Values Custodian oversight is critical. Consider retiring low-value agents.</div>
      </div>
    </div>`;
  } else {
    const headroom = data.phi_headroom ?? (phiStar - phi);
    html += `<div style="background:var(--green-d);border:1px solid var(--green-line);border-radius:10px;padding:.875rem 1rem;margin-bottom:1.25rem;display:flex;align-items:center;gap:.875rem">
      <span style="font-size:18px;color:var(--green)">◆</span>
      <div style="font-size:13px;color:var(--muted)">φ headroom: <strong style="color:var(--green)">${(headroom*100).toFixed(1)}%</strong> below crossover. Safe to onboard ${Math.floor(headroom/0.012)} more agents before governance stage escalates.</div>
    </div>`;
  }

  // Department summary cards
  const depts = ['customers','sales','planning','fulfillment'];
  html += `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-radius:12px;overflow:hidden;margin-bottom:1.5rem">`;
  for (const dept of depts) {
    const dm = DEPT_META[dept] || { icon:'◎', color:'var(--faint)', label: dept };
    const deptAgents = agents.filter(a => a.department === dept);
    const active = deptAgents.filter(a => a.status === 'active');
    const totalRuns = active.reduce((s, a) => s + (a.daily_runs || 0), 0);
    const totalValue = active.reduce((s, a) => s + (a.daily_runs || 0) * (a.value_per_run || 0), 0);
    html += `<div style="background:var(--panel);padding:1.25rem;cursor:pointer" onclick="filterDept('${dept}')">
      <div style="font-size:18px;margin-bottom:.5rem;color:${dm.color}">${dm.icon}</div>
      <div style="font-size:13px;font-weight:600;color:var(--text)">${dm.label}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">${active.length} agent${active.length!==1?'s':''} active</div>
      <div style="font-size:11px;color:var(--faint);margin-top:2px">${totalRuns.toLocaleString()} runs/day · $${totalValue.toFixed(0)}/day</div>
    </div>`;
  }
  html += `</div>`;

  // Tab bar
  const tabs = [
    { id: 'all', label: `All (${agents.length})` },
    { id: 'active', label: `Active (${data.active ?? 0})` },
    { id: 'retiring', label: `Retiring (${data.retiring ?? 0})` },
    { id: 'retired', label: `Retired (${data.retired ?? 0})` },
  ];
  html += `<div style="display:flex;gap:0;border-bottom:1px solid var(--line);margin-bottom:1.25rem">
    ${tabs.map(t => `<button onclick="loadAgents('${t.id}')"
      style="background:none;border:none;padding:.625rem 1rem;font-size:13px;cursor:pointer;font-family:var(--fb);
      color:${_agentsActiveTab===t.id?'var(--green)':'var(--muted)'};
      border-bottom:2px solid ${_agentsActiveTab===t.id?'var(--green)':'transparent'};transition:.15s">${t.label}</button>`).join('')}
  </div>`;

  // Filter
  const filtered = _agentsActiveTab === 'all' ? agents : agents.filter(a => a.status === _agentsActiveTab);

  if (filtered.length === 0) {
    html += `<div style="text-align:center;padding:3rem;color:var(--faint);font-size:13px">No agents in this status. <button onclick="openOnboardWizard()" style="background:none;border:none;color:var(--green);cursor:pointer;font-family:var(--fb);font-size:13px">Onboard your first agent →</button></div>`;
  } else {
    html += `<div style="background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--line)">
          ${['Agent','Department','Pipeline','Framework','Runs/day','Value/mo','Status','φ','Actions'].map(h =>
            `<th style="padding:.75rem 1rem;text-align:left;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)">${h}</th>`
          ).join('')}
        </tr></thead>
        <tbody>
          ${filtered.map(a => _agentRow(a)).join('')}
        </tbody>
      </table>
    </div>`;
  }

  body.innerHTML = html;
}

function filterDept(dept) {
  _agentsActiveTab = 'active';
  loadAgents('active');
}

function _agentRow(a) {
  const dm   = DEPT_META[a.department] || { icon:'◎', color:'var(--faint)', label: a.department };
  const ss   = STATUS_STYLE[a.status]  || STATUS_STYLE.active;
  const fwc  = FW_COLORS[a.framework]  || 'var(--faint)';
  const monthly = ((a.daily_runs || 0) * (a.value_per_run || 0) * 30).toFixed(0);
  const tasks = (a.tasks_automated || []).slice(0, 2).join(', ') + ((a.tasks_automated||[]).length > 2 ? '…' : '');
  const age = a.onboarded_at ? Math.floor((Date.now() - new Date(a.onboarded_at)) / 86400000) : '?';

  return `<tr style="border-bottom:1px solid var(--line2);transition:.1s" onmouseover="this.style.background='rgba(255,255,255,.02)'" onmouseout="this.style.background=''">
    <td style="padding:.75rem 1rem">
      <div style="font-size:13px;font-weight:500;color:var(--text)">${a.name}</div>
      <div style="font-size:11px;color:var(--faint);margin-top:2px">${tasks || a.description?.slice(0,50) || '—'}</div>
    </td>
    <td style="padding:.75rem 1rem">
      <span style="color:${dm.color};font-size:13px">${dm.icon} ${dm.label}</span>
    </td>
    <td style="padding:.75rem 1rem;font-size:12px;color:var(--muted)">${a.pipeline}</td>
    <td style="padding:.75rem 1rem">
      <span style="font-size:11px;color:${fwc};font-weight:500;text-transform:uppercase;letter-spacing:.06em">${a.framework}</span>
    </td>
    <td style="padding:.75rem 1rem;font-size:13px;color:var(--text)">${(a.daily_runs||0).toLocaleString()}</td>
    <td style="padding:.75rem 1rem;font-size:13px;color:var(--green)">$${Number(monthly).toLocaleString()}</td>
    <td style="padding:.75rem 1rem">
      <span style="background:${ss.bg};color:${ss.color};font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px">${ss.label}</span>
    </td>
    <td style="padding:.75rem 1rem;font-size:12px;color:var(--muted)">${((a.phi_contribution||0)*100).toFixed(1)}%</td>
    <td style="padding:.75rem 1rem">
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <button onclick="openCodeDeploy('${a.id}','${a.name.replace(/'/g,"\\'")}')"
          style="background:none;border:1px solid var(--blue-d);color:var(--blue);font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:var(--fb)" title="Generate code + deploy to Azure">⚙ Code</button>
        ${a.status === 'active' ? `<button onclick="openRetireWizard('${a.id}','${a.name.replace(/'/g,"\\'")}','${a.department}')"
          style="background:none;border:1px solid rgba(229,80,74,.3);color:var(--red);font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:var(--fb)">Retire</button>` : ''}
        ${a.status === 'retired' ? `<span style="font-size:11px;color:var(--faint)">${age}d ago</span>` : ''}
        ${a.status === 'paused' ? `<button onclick="resumeAgent('${a.id}')"
          style="background:none;border:1px solid var(--green-line);color:var(--green);font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:var(--fb)">Resume</button>` : ''}
      </div>
    </td>
  </tr>`;
}

function _agentOnboardModal() {
  return `<div id="onboard-modal" style="display:none;position:fixed;inset:0;z-index:500;background:rgba(5,5,5,.8);backdrop-filter:blur(6px);align-items:center;justify-content:center;padding:1.5rem">
    <div style="background:var(--panel);border:1px solid var(--line);border-radius:14px;width:100%;max-width:540px;overflow:hidden">
      <div style="padding:1.5rem 1.5rem 1rem;border-bottom:1px solid var(--line2)">
        <div style="font-family:var(--fh);font-size:20px">Onboard New Agent</div>
        <div style="font-size:12px;color:var(--muted);margin-top:3px">Register an AI agent into the Market360 lifecycle registry</div>
      </div>
      <div style="padding:1.5rem;display:flex;flex-direction:column;gap:.875rem;max-height:70vh;overflow-y:auto">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem">
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Agent Name *</label>
            <input id="ob-name" placeholder="LeadScorer-02" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
          </div>
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Department *</label>
            <select id="ob-dept" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px">
              <option value="customers">Customers</option>
              <option value="sales">Sales</option>
              <option value="planning">Planning</option>
              <option value="fulfillment">Fulfillment</option>
            </select>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem">
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Pipeline *</label>
            <input id="ob-pipeline" placeholder="lead-scoring" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
          </div>
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Framework</label>
            <select id="ob-fw" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px">
              <option value="langgraph">LangGraph</option>
              <option value="crewai">CrewAI</option>
              <option value="autogen">AutoGen</option>
              <option value="custom">Custom</option>
            </select>
          </div>
        </div>
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Description</label>
          <input id="ob-desc" placeholder="What does this agent do?" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
        </div>
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Tasks Automated (comma-separated)</label>
          <input id="ob-tasks" placeholder="classify request, route to team, log activity" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.75rem">
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Daily Runs</label>
            <input id="ob-runs" type="number" placeholder="150" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
          </div>
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Value/Run ($)</label>
            <input id="ob-value" type="number" placeholder="5.00" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
          </div>
          <div>
            <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">φ Contribution</label>
            <input id="ob-phi" type="number" step="0.001" placeholder="0.012" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
          </div>
        </div>
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">VC Oversight (email)</label>
          <input id="ob-vc" placeholder="maya.chen@demo.com" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px"/>
        </div>
        <div id="ob-phi-preview" style="background:var(--panel2);border-radius:8px;padding:.875rem;border:1px solid var(--line);font-size:12px;color:var(--muted);display:none"></div>
      </div>
      <div style="padding:1rem 1.5rem 1.25rem;border-top:1px solid var(--line2);display:flex;justify-content:flex-end;gap:.75rem">
        <button onclick="closeOnboardModal()" class="btn btn-outline">Cancel</button>
        <button onclick="submitOnboard()" class="btn btn-green" id="ob-submit-btn">Onboard Agent</button>
      </div>
    </div>
  </div>`;
}

function _agentRetireModal() {
  return `<div id="retire-modal" style="display:none;position:fixed;inset:0;z-index:500;background:rgba(5,5,5,.8);backdrop-filter:blur(6px);align-items:center;justify-content:center;padding:1.5rem">
    <div style="background:var(--panel);border:1px solid var(--line);border-radius:14px;width:100%;max-width:520px;overflow:hidden">
      <div style="padding:1.5rem 1.5rem 1rem;border-bottom:1px solid var(--line2)">
        <div style="font-family:var(--fh);font-size:20px;color:var(--red)">Retire Agent</div>
        <div id="retire-agent-name" style="font-size:12px;color:var(--muted);margin-top:3px">Loading…</div>
      </div>
      <div id="retire-preview-panel" style="padding:1rem 1.5rem;border-bottom:1px solid var(--line2)">
        <div style="color:var(--faint);font-size:12px">Loading retirement preview…</div>
      </div>
      <div style="padding:1.25rem 1.5rem;display:flex;flex-direction:column;gap:.875rem">
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Retirement Reason *</label>
          <select id="ret-reason-sel" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px">
            <option value="replaced-by-upgraded-agent">Replaced by upgraded agent</option>
            <option value="pipeline-decommissioned">Pipeline decommissioned</option>
            <option value="cost-optimisation">Cost optimisation</option>
            <option value="governance-phi-reduction">Governance — φ reduction</option>
            <option value="human-takeover">Human takeover — HAV role identified</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Handoff Plan *</label>
          <textarea id="ret-handoff" rows="2" placeholder="Who or what takes over? e.g. 'LeadScorer-03 (upgraded)' or 'James Okonkwo will handle manually'" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px;resize:vertical"></textarea>
        </div>
        <div>
          <label style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.3rem">Knowledge Captured</label>
          <textarea id="ret-knowledge" rows="2" placeholder="Key learnings, edge cases, config notes for the successor…" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--text);font-family:var(--fb);font-size:13px;resize:vertical"></textarea>
        </div>
      </div>
      <div style="padding:1rem 1.5rem 1.25rem;border-top:1px solid var(--line2);display:flex;justify-content:flex-end;gap:.75rem">
        <button onclick="closeRetireModal()" class="btn btn-outline">Cancel</button>
        <button onclick="submitRetire()" style="background:var(--red);color:#fff;border:1px solid var(--red)" class="btn" id="ret-submit-btn">Confirm Retirement</button>
      </div>
    </div>
  </div>`;
}

function openOnboardModal() {
  const m = document.getElementById('onboard-modal');
  if (m) { m.style.display = 'flex'; }
}
function closeOnboardModal() {
  const m = document.getElementById('onboard-modal');
  if (m) { m.style.display = 'none'; }
}
function openOnboardWizard() { openOnboardModal(); }

async function openRetireWizard(agentId, agentName, dept) {
  _retireAgentId = agentId;
  _retireAgentName = agentName;
  const m = document.getElementById('retire-modal');
  if (!m) return;
  document.getElementById('retire-agent-name').textContent = `${agentName} · ${dept}`;
  m.style.display = 'flex';

  // Load preview
  const panel = document.getElementById('retire-preview-panel');
  panel.innerHTML = `<div style="color:var(--faint);font-size:12px">Calculating φ impact…</div>`;
  try {
    const p = await GET(`/agent-factory/agents/${agentId}/retirement-preview`);
    _retirePreview = p;
    const drop = ((p.phi_delta || 0) * 100).toFixed(1);
    const govImprove = p.governance_improvement;
    panel.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.75rem;margin-bottom:.75rem">
        <div style="background:var(--panel2);border-radius:8px;padding:.75rem;border:1px solid var(--line)">
          <div style="font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.25rem">φ Before</div>
          <div style="font-size:18px;font-family:var(--fh);color:${p.currently_above_crossover?'var(--red)':'var(--green)'}">${(p.phi_current*100).toFixed(1)}%</div>
        </div>
        <div style="background:var(--panel2);border-radius:8px;padding:.75rem;border:1px solid var(--line)">
          <div style="font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.25rem">φ After</div>
          <div style="font-size:18px;font-family:var(--fh);color:${p.will_be_above_crossover?'var(--red)':'var(--green)'}">${(p.phi_after_retirement*100).toFixed(1)}%</div>
        </div>
        <div style="background:var(--panel2);border-radius:8px;padding:.75rem;border:1px solid var(--line)">
          <div style="font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.25rem">Value Impact</div>
          <div style="font-size:18px;font-family:var(--fh);color:var(--amber)">-$${Number(p.estimated_value_impact||0).toLocaleString()}/mo</div>
        </div>
      </div>
      ${govImprove ? `<div style="background:var(--green-d);border:1px solid var(--green-line);border-radius:8px;padding:.625rem .875rem;font-size:12px;color:var(--green);margin-bottom:.5rem">◆ Retiring this agent brings φ below φ* — governance stage will improve.</div>` : ''}
      ${p.hire_human_recommended ? `<div style="background:rgba(229,168,58,.08);border:1px solid rgba(229,168,58,.2);border-radius:8px;padding:.625rem .875rem;font-size:12px;color:var(--amber)">⚠ ${p.tasks_requiring_human_judgment.length} task(s) require human judgment — hire before retiring: ${p.tasks_requiring_human_judgment.join(', ')}</div>` : `<div style="font-size:12px;color:var(--muted)">All ${p.tasks_requiring_coverage.length} task(s) are procedural — safe to hand off to another agent.</div>`}`;
  } catch(e) {
    panel.innerHTML = `<div style="font-size:12px;color:var(--faint)">Preview unavailable.</div>`;
  }
}
function closeRetireModal() {
  const m = document.getElementById('retire-modal');
  if (m) { m.style.display = 'none'; }
  _retireAgentId = null; _retirePreview = null;
}

async function submitOnboard() {
  const name     = document.getElementById('ob-name')?.value?.trim();
  const dept     = document.getElementById('ob-dept')?.value;
  const pipeline = document.getElementById('ob-pipeline')?.value?.trim();
  if (!name || !pipeline) { toast('Agent name and pipeline are required'); return; }

  const fw    = document.getElementById('ob-fw')?.value || 'custom';
  const desc  = document.getElementById('ob-desc')?.value?.trim() || '';
  const tasks = (document.getElementById('ob-tasks')?.value || '').split(',').map(t => t.trim()).filter(Boolean);
  const runs  = parseInt(document.getElementById('ob-runs')?.value) || 0;
  const val   = parseFloat(document.getElementById('ob-value')?.value) || 0;
  const phi   = parseFloat(document.getElementById('ob-phi')?.value) || 0.01;
  const vc    = document.getElementById('ob-vc')?.value?.trim() || '';

  const btn = document.getElementById('ob-submit-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Onboarding…'; }

  try {
    const r = await fetch('/api/v1/agent-factory/agents/onboard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_id: AGENT_ORG, name, description: desc, department: dept,
        pipeline, framework: fw, phi_contribution: phi,
        tasks_automated: tasks, daily_runs: runs, value_per_run: val,
        oversight_human: vc || undefined, onboarded_by: 'platform-user',
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed');
    toast(`Agent '${name}' onboarded · φ: ${(d.org_phi_before*100).toFixed(1)}% → ${(d.org_phi_after*100).toFixed(1)}%`);
    closeOnboardModal();
    await loadAgents(_agentsActiveTab);
  } catch(e) {
    toast(`Error: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = 'Onboard Agent'; }
  }
}

async function submitRetire() {
  if (!_retireAgentId) return;
  const reason   = document.getElementById('ret-reason-sel')?.value || 'other';
  const handoff  = document.getElementById('ret-handoff')?.value?.trim();
  const knowledge = document.getElementById('ret-knowledge')?.value?.trim() || '';
  if (!handoff) { toast('Handoff plan is required'); return; }

  const btn = document.getElementById('ret-submit-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Retiring…'; }

  try {
    const r = await fetch(`/api/v1/agent-factory/agents/${_retireAgentId}/retire`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ retirement_reason: reason, handoff_plan: handoff, knowledge_captured: knowledge, retired_by: 'platform-user' }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed');
    toast(`'${_retireAgentName}' retired · φ: ${(d.org_phi_before*100).toFixed(1)}% → ${(d.org_phi_after*100).toFixed(1)}%`);
    closeRetireModal();
    await loadAgents(_agentsActiveTab);
  } catch(e) {
    toast(`Error: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = 'Confirm Retirement'; }
  }
}

async function resumeAgent(agentId) {
  try {
    const r = await fetch(`/api/v1/agent-factory/agents/${agentId}/resume?actor=platform-user`, { method: 'PATCH' });
    if (!r.ok) throw new Error((await r.json()).detail);
    toast('Agent resumed');
    await loadAgents(_agentsActiveTab);
  } catch(e) { toast(`Error: ${e.message}`); }
}

// ─── CODE GENERATION + AZURE DEPLOY ──────────────────────────────────────────

let _codeModal_agentId = null;
let _codeModal_deployId = null;
let _codeModal_pollTimer = null;

async function openCodeDeploy(agentId, agentName) {
  _codeModal_agentId = agentId;
  _codeModal_deployId = null;
  if (_codeModal_pollTimer) { clearInterval(_codeModal_pollTimer); _codeModal_pollTimer = null; }

  // Inject modal if not present
  if (!document.getElementById('code-modal')) {
    document.body.insertAdjacentHTML('beforeend', _codeDeployModal());
  }
  document.getElementById('code-modal').style.display = 'flex';
  document.getElementById('cd-agent-title').textContent = agentName;
  document.getElementById('cd-tabs').style.display = 'none';
  document.getElementById('cd-loading').style.display = 'block';
  document.getElementById('cd-loading').textContent = 'Generating code…';

  try {
    const r = await fetch('/api/v1/deployment/generate-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: agentId, org_id: AGENT_ORG }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed');
    document.getElementById('cd-loading').style.display = 'none';
    document.getElementById('cd-tabs').style.display = 'block';
    document.getElementById('cd-code-agent').textContent = d.agent_py || '';
    document.getElementById('cd-code-docker').textContent = d.dockerfile || '';
    document.getElementById('cd-code-k8s').textContent = d.k8s_manifest || '# ACI deployment — no K8s manifest needed';
    document.getElementById('cd-code-sdk').textContent = d.tessera_sdk_py || '';
    document.getElementById('cd-image-tag').textContent = d.image_tag || '';
    document.getElementById('cd-acr-login').textContent = d.acr_login || '';
    document.getElementById('cd-compute').textContent = (d.compute_target || 'aci').toUpperCase();
    const cmds = (d.deploy_commands || []).join('\n');
    document.getElementById('cd-deploy-cmds').textContent = cmds;
    cdShowTab('agent');
  } catch(e) {
    document.getElementById('cd-loading').textContent = `Error: ${e.message}`;
  }
}

function _codeDeployModal() {
  return `<div id="code-modal" style="display:none;position:fixed;inset:0;z-index:600;background:rgba(5,5,5,.85);backdrop-filter:blur(8px);align-items:center;justify-content:center;padding:1rem">
    <div style="background:var(--panel);border:1px solid var(--line);border-radius:14px;width:100%;max-width:780px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden">
      <!-- Header -->
      <div style="padding:1.25rem 1.5rem 1rem;border-bottom:1px solid var(--line2);display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <div>
          <div style="font-family:var(--fh);font-size:19px" id="cd-agent-title">Agent</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">
            <span id="cd-compute" style="color:var(--blue)">ACI</span> ·
            Image: <code style="font-size:11px;color:var(--amber)" id="cd-image-tag">—</code> ·
            ACR: <code style="font-size:11px;color:var(--faint)" id="cd-acr-login">—</code>
          </div>
        </div>
        <div style="display:flex;gap:.625rem;align-items:center">
          <button onclick="triggerDeploy()" id="cd-deploy-btn" class="btn btn-green">▶ Deploy to Azure</button>
          <button onclick="closeCodeModal()" style="background:none;border:none;color:var(--faint);font-size:20px;cursor:pointer;padding:4px 8px">×</button>
        </div>
      </div>
      <!-- Deploy status bar -->
      <div id="cd-status-bar" style="display:none;padding:.625rem 1.5rem;background:var(--panel2);border-bottom:1px solid var(--line2);font-size:12px;color:var(--muted)">
        Status: <span id="cd-status-text" style="color:var(--green)">Queued</span>
        <span id="cd-status-detail" style="color:var(--faint);margin-left:.5rem"></span>
      </div>
      <!-- Loading -->
      <div id="cd-loading" style="padding:2rem;color:var(--faint);font-size:13px">Generating code…</div>
      <!-- Tab bar -->
      <div id="cd-tabs" style="display:none;flex-direction:column;flex:1;overflow:hidden">
        <div style="display:flex;border-bottom:1px solid var(--line2);flex-shrink:0;padding:0 1.5rem">
          ${[
            {id:'agent',label:'agent.py'},
            {id:'docker',label:'Dockerfile'},
            {id:'k8s',label:'k8s.yaml'},
            {id:'sdk',label:'tessera_sdk.py'},
            {id:'cmds',label:'Deploy Commands'},
            {id:'logs',label:'Logs'},
          ].map(t => `<button id="cd-tab-${t.id}" onclick="cdShowTab('${t.id}')"
            style="background:none;border:none;padding:.625rem .875rem;font-size:12px;cursor:pointer;font-family:var(--fb);color:var(--faint);border-bottom:2px solid transparent;transition:.15s">${t.label}</button>`).join('')}
        </div>
        <div style="flex:1;overflow-y:auto;padding:1rem 1.5rem">
          ${['agent','docker','k8s','sdk'].map(t => `
            <div id="cd-panel-${t}" style="display:none;position:relative">
              <button onclick="copyCode('cd-code-${t}')" style="position:absolute;top:0;right:0;background:var(--panel2);border:1px solid var(--line);border-radius:5px;color:var(--muted);font-size:11px;padding:4px 10px;cursor:pointer;font-family:var(--fb)">Copy</button>
              <button onclick="dlCode('cd-code-${t}','${t === 'agent' ? 'agent.py' : t === 'docker' ? 'Dockerfile' : t === 'k8s' ? 'k8s.yaml' : 'tessera_sdk.py'}')" style="position:absolute;top:0;right:60px;background:var(--panel2);border:1px solid var(--line);border-radius:5px;color:var(--muted);font-size:11px;padding:4px 10px;cursor:pointer;font-family:var(--fb)">↓ Download</button>
              <pre id="cd-code-${t}" style="background:var(--void);border:1px solid var(--line2);border-radius:8px;padding:1rem;font-size:11px;line-height:1.6;overflow-x:auto;color:var(--text);margin-top:2rem;white-space:pre"></pre>
            </div>`).join('')}
          <div id="cd-panel-cmds" style="display:none">
            <div style="font-size:12px;color:var(--muted);margin-bottom:.75rem">Run these commands after setting Azure credentials. Or click <strong style="color:var(--green)">Deploy to Azure</strong> to let Tessera do it.</div>
            <pre id="cd-deploy-cmds" style="background:var(--void);border:1px solid var(--line2);border-radius:8px;padding:1rem;font-size:11px;line-height:1.7;overflow-x:auto;color:var(--amber);white-space:pre"></pre>
          </div>
          <div id="cd-panel-logs" style="display:none">
            <pre id="cd-logs-content" style="background:var(--void);border:1px solid var(--line2);border-radius:8px;padding:1rem;font-size:11px;line-height:1.6;overflow-x:auto;color:var(--text);white-space:pre;min-height:200px">No deployment started yet. Click ▶ Deploy to Azure.</pre>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

function cdShowTab(tab) {
  ['agent','docker','k8s','sdk','cmds','logs'].forEach(t => {
    const p = document.getElementById(`cd-panel-${t}`);
    const b = document.getElementById(`cd-tab-${t}`);
    if (p) p.style.display = t === tab ? 'block' : 'none';
    if (b) { b.style.color = t === tab ? 'var(--green)' : 'var(--faint)'; b.style.borderBottomColor = t === tab ? 'var(--green)' : 'transparent'; }
  });
}

function copyCode(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => toast('Copied to clipboard'));
}

function dlCode(elId, filename) {
  const el = document.getElementById(elId);
  if (!el) return;
  const blob = new Blob([el.textContent], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}

async function triggerDeploy() {
  const btn = document.getElementById('cd-deploy-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deploying…'; }
  const statusBar = document.getElementById('cd-status-bar');
  if (statusBar) statusBar.style.display = 'block';

  try {
    const r = await fetch('/api/v1/deployment/deploy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: _codeModal_agentId, org_id: AGENT_ORG }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed');
    _codeModal_deployId = d.deployment_id;
    if (d.simulate) {
      const st = document.getElementById('cd-status-text');
      if (st) st.textContent += ' (simulate mode — set AZURE_* vars for real deploy)';
    }
    cdShowTab('logs');
    // Poll for status
    _codeModal_pollTimer = setInterval(() => _pollDeploy(), 2000);
  } catch(e) {
    toast(`Deploy error: ${e.message}`);
    if (btn) { btn.disabled = false; btn.textContent = '▶ Deploy to Azure'; }
  }
}

async function _pollDeploy() {
  if (!_codeModal_deployId) return;
  try {
    const d = await GET(`/deployment/deployments/${_codeModal_deployId}`);
    const st  = document.getElementById('cd-status-text');
    const det = document.getElementById('cd-status-detail');
    const logs = document.getElementById('cd-logs-content');
    const btn  = document.getElementById('cd-deploy-btn');

    const STATUS_COLORS = { queued:'var(--faint)', provisioning:'var(--blue)', building:'var(--blue)',
      pushing:'var(--blue)', deploying:'var(--amber)', running:'var(--green)', failed:'var(--red)', stopped:'var(--faint)' };

    if (st)  { st.textContent = d.status || ''; st.style.color = STATUS_COLORS[d.status] || 'var(--muted)'; }
    if (det) det.textContent = d.status_detail || '';
    if (logs) logs.textContent = (d.logs || 'Waiting for logs…').trim();

    if (d.status === 'running' || d.status === 'failed' || d.status === 'stopped') {
      clearInterval(_codeModal_pollTimer); _codeModal_pollTimer = null;
      if (btn) { btn.disabled = false; btn.textContent = d.status === 'running' ? '✓ Running' : '▶ Deploy to Azure'; }
      if (d.status === 'running') toast(`Agent deployed and running on ${d.compute_target?.toUpperCase()}`);
      if (d.status === 'failed') toast(`Deployment failed: ${d.error?.slice(0,80) || 'see logs'}`);
    }
  } catch(e) { /* ignore poll errors */ }
}

function closeCodeModal() {
  if (_codeModal_pollTimer) { clearInterval(_codeModal_pollTimer); _codeModal_pollTimer = null; }
  const m = document.getElementById('code-modal');
  if (m) m.style.display = 'none';
}

// ─── ONBOARDING WIZARD ───────────────────────────────────────────────────

const WIZARD_KEY = 'tessera_wizard_done';

const WIZARD_STEPS = [
  {
    id: 'welcome',
    icon: '◈',
    title: 'Welcome to Tessera',
    subtitle: 'Human and AI Capital Management',
    body: `<p style="color:var(--muted);line-height:1.7;font-size:14px">
      Tessera is the first platform to measure what actually matters in the AI era —
      <strong style="color:var(--text)">Human Alignment Value (HAV)</strong>.
    </p>
    <div style="margin:1.25rem 0;padding:1rem 1.25rem;background:var(--panel2);border-radius:10px;border:1px solid var(--green-line)">
      <div style="font-size:11px;color:var(--faint);letter-spacing:.1em;text-transform:uppercase;margin-bottom:.5rem">The core formula</div>
      <div style="font-family:monospace;font-size:15px;color:var(--green);letter-spacing:.02em">HAV = 0.50×NPF + 0.30×SRQ + 0.20×OC</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5rem;margin-top:.875rem">
        <div style="font-size:11px;color:var(--muted)"><strong style="color:var(--text)">NPF</strong><br>Non-procedural work fraction</div>
        <div style="font-size:11px;color:var(--muted)"><strong style="color:var(--text)">SRQ</strong><br>Service resolution quality</div>
        <div style="font-size:11px;color:var(--muted)"><strong style="color:var(--text)">OC</strong><br>Org coherence contribution</div>
      </div>
    </div>
    <p style="color:var(--muted);font-size:13px">This replaces Workday's man-hours, ServiceNow's ticket counts, and Jira's story points — all under one hood.</p>`,
    cta: 'Get started',
    skip: 'I\'ll explore on my own',
  },
  {
    id: 'import',
    icon: '⇡',
    title: 'Connect your data',
    subtitle: 'Step 1 of 4',
    body: `<p style="color:var(--muted);margin-bottom:1rem;font-size:14px">How does your org track people today?</p>
    <div id="wiz-source-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-bottom:1rem">
      ${[
        {id:'workday', icon:'▣', label:'Workday', desc:'Export Workers + Performance report'},
        {id:'bamboohr', icon:'◉', label:'BambooHR', desc:'Export Employees + Compensation'},
        {id:'csv', icon:'≡', label:'CSV / Excel', desc:'Any spreadsheet with employee data'},
        {id:'demo', icon:'◈', label:'Load demo data', desc:'10 employees · 60 sessions · instant'},
      ].map(s => `<button onclick="wizSelectSource('${s.id}')" id="wiz-src-${s.id}"
        style="background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:1rem;text-align:left;cursor:pointer;transition:.15s;color:var(--text)"
        onmouseover="this.style.borderColor='var(--green-line)'" onmouseout="this.style.borderColor=document.getElementById('wiz-selected-source')==='${s.id}'?'var(--green)':'var(--line)'">
        <div style="font-size:18px;margin-bottom:.4rem;opacity:.8">${s.icon}</div>
        <div style="font-size:13px;font-weight:600">${s.label}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">${s.desc}</div>
      </button>`).join('')}
    </div>
    <div id="wiz-import-area" style="display:none"></div>`,
    cta: 'Continue',
    skip: 'Skip for now',
  },
  {
    id: 'phi',
    icon: 'φ',
    title: 'Set your AI fraction target',
    subtitle: 'Step 2 of 4',
    body: `<p style="color:var(--muted);margin-bottom:1.25rem;font-size:14px">
      φ* is the crossover threshold — the AI fraction above which standard management breaks down.
      Based on your org's interconnectivity (K), Tessera recommends:
    </p>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.75rem;margin-bottom:1.25rem">
      ${[
        {k:'≥6',phi:'0.25',label:'High K',hint:'Many interdependencies — crossover hits early'},
        {k:'4',phi:'0.32',label:'Medium K',hint:'Most orgs — good balance of autonomy and coherence'},
        {k:'≤2',phi:'0.44',label:'Low K',hint:'Few interdependencies — more AI is safe'},
      ].map(o => `<button onclick="wizSelectK(this,'${o.phi}')"
        style="background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:1rem;text-align:left;cursor:pointer;transition:.15s;color:var(--text)"
        onmouseover="this.style.borderColor='var(--blue)'" onmouseout="">
        <div style="font-size:22px;font-family:var(--fh);color:var(--blue);margin-bottom:.4rem">φ*=${o.phi}</div>
        <div style="font-size:12px;font-weight:600">K ${o.k}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:3px">${o.hint}</div>
      </button>`).join('')}
    </div>
    <div style="background:var(--panel2);border-radius:8px;padding:.875rem;border:1px solid var(--line)">
      <div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.5rem">Your φ* selection</div>
      <div id="wiz-phi-display" style="font-size:24px;font-family:var(--fh);color:var(--blue)">0.32 <span style="font-size:14px;color:var(--muted);font-family:var(--fb)">(K=4, recommended)</span></div>
    </div>
    <input type="hidden" id="wiz-phi-val" value="0.32"/>`,
    cta: 'Continue',
    skip: null,
  },
  {
    id: 'values',
    icon: '◎',
    title: 'Identify your Values Custodians',
    subtitle: 'Step 3 of 4',
    body: `<p style="color:var(--muted);margin-bottom:1.25rem;font-size:14px">
      Values Custodians are the humans whose judgment keeps AI belief drift in check.
      They are automatically identified when HAV ≥ 0.70 AND NPF ≥ 0.65.
    </p>
    <div style="background:var(--panel2);border-radius:10px;border:1px solid var(--green-line);padding:1.25rem;margin-bottom:1rem">
      <div style="font-size:11px;color:var(--green);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.75rem;font-weight:600">What Tessera does automatically</div>
      ${[
        'Flags VCs in every HR decision — absence, departure, role change',
        'Requires coverage assignment before a VC can go on leave',
        'Raises a critical signal if a VC is at retention risk',
        'Blocks AI deployment that would drop VC count below safe threshold',
      ].map(t => `<div style="display:flex;gap:.75rem;align-items:flex-start;margin-bottom:.6rem">
        <span style="color:var(--green);font-size:14px;flex-shrink:0">◆</span>
        <span style="font-size:13px;color:var(--muted)">${t}</span>
      </div>`).join('')}
    </div>
    <div style="background:rgba(229,80,74,.08);border:1px solid rgba(229,80,74,.2);border-radius:8px;padding:.875rem">
      <span style="color:var(--red);font-size:13px;font-weight:500">Never let a Values Custodian leave without understanding what knowledge they carry.</span>
    </div>`,
    cta: 'Continue',
    skip: null,
  },
  {
    id: 'done',
    icon: '◈',
    title: 'You\'re ready',
    subtitle: 'Step 4 of 4',
    body: `<p style="color:var(--muted);margin-bottom:1.25rem;font-size:14px">Your Tessera workspace is set up. Here's what to check first:</p>
    <div style="display:flex;flex-direction:column;gap:.75rem;margin-bottom:1.5rem">
      ${[
        {icon:'◈', nav:'dashboard', label:'HAV Overview', desc:'Your org\'s HAV distribution and Values Custodians'},
        {icon:'◉', nav:'signals', label:'Signal Feed', desc:'Critical alerts — VC at risk, φ approaching crossover'},
        {icon:'◐', nav:'twin', label:'Digital Twin', desc:'φ trajectory, governance stage, belief drift'},
        {icon:'⊟', nav:'workforce', label:'Workforce Planning', desc:'Hire human vs deploy AI — powered by V_net math'},
      ].map(r => `<button onclick="wizGoTo('${r.nav}')"
        style="display:flex;align-items:center;gap:1rem;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:.875rem 1rem;text-align:left;cursor:pointer;transition:.15s;color:var(--text);width:100%"
        onmouseover="this.style.borderColor='var(--green-line)'" onmouseout="this.style.borderColor='var(--line)'">
        <span style="font-size:18px;opacity:.7;flex-shrink:0">${r.icon}</span>
        <div>
          <div style="font-size:13px;font-weight:600">${r.label}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:1px">${r.desc}</div>
        </div>
        <span style="margin-left:auto;color:var(--faint);font-size:14px">›</span>
      </button>`).join('')}
    </div>`,
    cta: 'Open Tessera',
    skip: null,
  }
];

let _wizStep = 0;
let _wizSource = null;

function wizSelectSource(src) {
  _wizSource = src;
  document.querySelectorAll('[id^="wiz-src-"]').forEach(b => {
    b.style.borderColor = 'var(--line)';
    b.style.background = 'var(--panel2)';
  });
  const btn = document.getElementById('wiz-src-' + src);
  if (btn) { btn.style.borderColor = 'var(--green)'; btn.style.background = 'var(--green-d)'; }

  const area = document.getElementById('wiz-import-area');
  if (!area) return;
  if (src === 'demo') {
    area.style.display = 'none';
  } else {
    area.style.display = 'block';
    area.innerHTML = `<div style="background:var(--panel2);border-radius:8px;padding:.875rem;border:1px solid var(--line);font-size:12px;color:var(--muted)">
      After clicking Continue, go to <strong style="color:var(--text)">Import Data</strong> in the sidebar to paste your ${src === 'workday' ? 'Workday' : src === 'bamboohr' ? 'BambooHR' : 'CSV'} export.
      Full field mapping guide: <a href="docs/onboarding.md" style="color:var(--green);text-decoration:none">docs/onboarding.md</a>
    </div>`;
  }
}

function wizSelectK(btn, phi) {
  document.querySelectorAll('#wiz-step-phi button[onclick^="wizSelectK"]').forEach(b => {
    b.style.borderColor = 'var(--line)'; b.style.background = 'var(--panel2)';
  });
  btn.style.borderColor = 'var(--blue)'; btn.style.background = 'rgba(74,142,229,.1)';
  document.getElementById('wiz-phi-val').value = phi;
  const kLabels = {'0.25':'K≥6, high interconnectivity','0.32':'K=4, recommended','0.44':'K≤2, low interconnectivity'};
  document.getElementById('wiz-phi-display').innerHTML =
    `${phi} <span style="font-size:14px;color:var(--muted);font-family:var(--fb)">(${kLabels[phi]})</span>`;
}

function wizGoTo(view) {
  closeWizard();
  loadView(view);
  nav(view);
}

function wizRender() {
  const s = WIZARD_STEPS[_wizStep];
  const overlay = document.getElementById('wiz-overlay');
  if (!overlay) return;
  const pct = Math.round((_wizStep / (WIZARD_STEPS.length - 1)) * 100);

  overlay.innerHTML = `
    <div id="wiz-card" style="background:var(--panel);border:1px solid var(--line2);border-radius:16px;width:100%;max-width:560px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.6)">
      <!-- progress bar -->
      <div style="height:3px;background:var(--line2)">
        <div style="height:100%;width:${pct}%;background:var(--green);transition:width .4s"></div>
      </div>
      <!-- header -->
      <div style="padding:2rem 2rem 1rem;border-bottom:1px solid var(--line2)">
        <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:.875rem">
          <div style="width:40px;height:40px;border-radius:10px;background:var(--green-d);border:1px solid var(--green-line);display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--green)">${s.icon}</div>
          <div>
            <div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.12em;font-weight:600">${s.subtitle}</div>
            <div style="font-size:20px;font-family:var(--fh);font-weight:400;color:var(--text);margin-top:1px">${s.title}</div>
          </div>
        </div>
      </div>
      <!-- body -->
      <div id="wiz-step-${s.id}" style="padding:1.5rem 2rem">${s.body}</div>
      <!-- footer -->
      <div style="padding:1rem 2rem 1.5rem;display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--line2)">
        <div style="display:flex;gap:6px">
          ${WIZARD_STEPS.map((_,i) => `<div style="width:${i===_wizStep?18:6}px;height:6px;border-radius:3px;background:${i===_wizStep?'var(--green)':i<_wizStep?'rgba(111,207,74,.35)':'var(--line)'};transition:.3s"></div>`).join('')}
        </div>
        <div style="display:flex;align-items:center;gap:.75rem">
          ${s.skip ? `<button onclick="wizSkip()" style="background:none;border:none;color:var(--faint);font-size:12px;cursor:pointer;font-family:var(--fb);padding:6px 10px">${s.skip}</button>` : ''}
          ${_wizStep > 0 ? `<button onclick="wizBack()" class="btn btn-outline">← Back</button>` : ''}
          <button onclick="wizNext()" class="btn btn-green" id="wiz-cta">${s.cta}</button>
        </div>
      </div>
    </div>`;
}

async function wizNext() {
  const s = WIZARD_STEPS[_wizStep];

  if (s.id === 'import' && _wizSource === 'demo') {
    const btn = document.getElementById('wiz-cta');
    if (btn) { btn.disabled = true; btn.textContent = 'Seeding…'; }
    try {
      await fetch('/demo/seed');
    } catch(e) {}
    if (btn) { btn.disabled = false; }
  }

  if (s.id === 'done') { closeWizard(); return; }
  _wizStep = Math.min(_wizStep + 1, WIZARD_STEPS.length - 1);
  wizRender();
}

function wizBack() {
  _wizStep = Math.max(0, _wizStep - 1);
  wizRender();
}

function wizSkip() {
  closeWizard();
}

function closeWizard() {
  localStorage.setItem(WIZARD_KEY, '1');
  const overlay = document.getElementById('wiz-overlay');
  if (overlay) {
    overlay.style.opacity = '0';
    overlay.style.transition = 'opacity .3s';
    setTimeout(() => overlay.remove(), 300);
  }
}

function showWizard() {
  if (localStorage.getItem(WIZARD_KEY)) return;
  const overlay = document.createElement('div');
  overlay.id = 'wiz-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1000;background:rgba(5,5,5,.85);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;padding:1.5rem;opacity:0;transition:opacity .3s';
  document.body.appendChild(overlay);
  requestAnimationFrame(() => { overlay.style.opacity = '1'; });
  wizRender();
}

// ─── GOAL PLANNER ────────────────────────────────────────────────────────

const DEPT_ICONS = { customers:'◎', sales:'◇', planning:'◐', fulfillment:'◈' };
const DEPT_COLS  = { customers:'var(--blue)', sales:'var(--green)', planning:'var(--amber)', fulfillment:'var(--purple)' };
const FW_BADGE   = { langgraph:'var(--blue)', crewai:'var(--purple)', autogen:'var(--amber)', custom:'var(--faint)' };
const EXEC_ICON  = { agent:'⟳', human:'◎', hybrid:'◑' };

async function loadGoals() {
  document.getElementById('goals-tree').innerHTML =
    '<div class="empty"><h3>Loading…</h3></div>';

  const r = await fetch('/api/v1/goal-planning/orgs/market360/gpa');
  if (!r.ok) {
    document.getElementById('goals-tree').innerHTML =
      `<div class="empty"><h3>No data</h3><p>Run <strong>Seed Demo Data</strong> first to populate the Market360 GPA tree, or click <strong>Seed Market360 GPA</strong> above.</p></div>`;
    return;
  }
  const d = await r.json();
  const sum = d.summary || {};

  document.getElementById('g-total').textContent = sum.total_goals ?? '—';
  document.getElementById('g-agents').textContent = sum.total_agent_steps ?? '—';
  document.getElementById('g-value').textContent =
    sum.total_value_monthly ? `$${Math.round(sum.total_value_monthly/1000)}K` : '—';
  document.getElementById('g-runs').textContent =
    sum.total_runs_per_day ? sum.total_runs_per_day.toLocaleString() : '—';

  const strategics = d.strategic_goals || [];
  const standalones = d.standalone_dept_goals || [];

  let html = '';

  for (const sg of strategics) {
    html += _renderStrategicGoal(sg);
  }
  if (standalones.length) {
    html += `<div style="margin-top:1.5rem;margin-bottom:.75rem;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)">Department Goals (standalone)</div>`;
    for (const dg of standalones) html += _renderDeptGoal(dg);
  }

  if (!html) {
    html = `<div class="empty"><h3>No goals yet</h3><p>Click <strong>Seed Market360 GPA</strong> to load the Market360 example, or run <strong>Seed Demo Data</strong> from the dashboard.</p></div>`;
  }
  document.getElementById('goals-tree').innerHTML = html;
}

function _progressBar(current, target, unit) {
  if (target == null || target === 0) return '';
  const pct = Math.min(100, Math.round((current / target) * 100));
  const isLower = unit === 's' || unit === 'ms'; // lower-is-better metrics
  const done = isLower ? current <= target : pct >= 100;
  const color = done ? 'var(--green)' : pct >= 80 ? 'var(--amber)' : 'var(--blue)';
  const displayPct = isLower
    ? Math.round(((target - current) / target) * 100) + '% improvement needed'
    : `${pct}% of target`;
  return `
    <div style="margin-top:.75rem">
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--faint);margin-bottom:4px">
        <span>${displayPct}</span>
        <span style="color:var(--muted)">Target: ${target}${unit||''}</span>
      </div>
      <div style="height:5px;background:var(--panel2);border-radius:3px;overflow:hidden">
        <div style="height:100%;width:${isLower ? Math.min(100, Math.round((1-current/target)*100)) : pct}%;background:${color};border-radius:3px;transition:.4s"></div>
      </div>
    </div>`;
}

function _renderStrategicGoal(sg) {
  const totalValue = sg.dept_goals
    ? sg.dept_goals.reduce((s, dg) => s + (dg.total_value_monthly||0), 0) : 0;
  const totalAgents = sg.dept_goals
    ? sg.dept_goals.reduce((s, dg) => s + (dg.agent_count||0), 0) : 0;

  const vcBadges = (sg.accountability||[]).filter(a => a.is_vc)
    .map(a => `<span class="badge vc" style="font-size:9px">◎ ${a.human_name} VC</span>`).join(' ');
  const ownerBadges = (sg.accountability||[]).filter(a => !a.is_vc)
    .map(a => `<span style="font-size:11px;color:var(--muted)">⬡ ${a.human_name} (${a.role})</span>`).join(' ');

  const statusColor = sg.status === 'completed' ? 'var(--green)'
    : sg.status === 'at_risk' ? 'var(--red)' : 'var(--amber)';

  let html = `
    <div style="background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:1.5rem">
      <!-- Strategic header -->
      <div style="padding:1.25rem 1.5rem;border-bottom:1px solid var(--line);background:var(--panel2)">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:.625rem;margin-bottom:.5rem">
              <div style="font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--amber);background:var(--amber-d);padding:2px 8px;border-radius:99px;border:1px solid rgba(229,168,58,.3)">Strategic Goal</div>
              <div style="width:6px;height:6px;border-radius:50%;background:${statusColor}"></div>
              ${vcBadges}
            </div>
            <div style="font-family:var(--fh);font-size:19px;font-weight:400;color:var(--text);line-height:1.35;margin-bottom:.5rem">${sg.title}</div>
            <div style="font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:.75rem">${sg.description||''}</div>
            <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
              ${ownerBadges}
            </div>
            ${_progressBar(sg.current_value, sg.target_value, sg.unit)}
          </div>
          <div style="display:flex;flex-direction:column;gap:.5rem;align-items:flex-end;flex-shrink:0">
            <div style="text-align:right">
              <div style="font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)">Monthly AI Value</div>
              <div style="font-family:var(--fh);font-size:22px;color:var(--green)">$${Math.round(totalValue/1000)}K</div>
            </div>
            <div style="text-align:right">
              <div style="font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)">Agent Steps</div>
              <div style="font-size:17px;font-weight:600;color:var(--text)">${totalAgents}</div>
            </div>
          </div>
        </div>
      </div>`;

  // Dept goals
  if (sg.dept_goals && sg.dept_goals.length) {
    html += `<div style="padding:.75rem 1.5rem .75rem;display:grid;grid-template-columns:1fr 1fr;gap:1rem">`;
    for (const dg of sg.dept_goals) html += _renderDeptGoal(dg, true);
    html += `</div>`;
  }

  html += `</div>`;
  return html;
}

function _renderDeptGoal(dg, compact = false) {
  const dept = dg.department || 'unknown';
  const icon = DEPT_ICONS[dept] || '◇';
  const col  = DEPT_COLS[dept] || 'var(--faint)';
  const steps = dg.plan_steps || [];
  const accts = dg.accountability || [];
  const vc = accts.find(a => a.is_vc);
  const owner = accts.find(a => a.role === 'owner');

  const agentSteps = steps.filter(s => s.executor_type === 'agent');
  const humanSteps = steps.filter(s => s.executor_type === 'human');

  const stepsHtml = steps.map(s => {
    const isAgent = s.executor_type === 'agent';
    const fwColor = FW_BADGE[s.agent_framework] || 'var(--faint)';
    const azure = s.azure_deployed
      ? `<span style="font-size:9px;background:rgba(74,142,229,.12);color:var(--blue);border:1px solid rgba(74,142,229,.25);padding:1px 5px;border-radius:4px">AKS</span>` : '';

    return `
      <div style="display:flex;align-items:flex-start;gap:.625rem;padding:.625rem 0;border-bottom:1px solid var(--line2)">
        <div style="width:22px;height:22px;border-radius:6px;background:${isAgent ? 'rgba(74,142,229,.12)':'rgba(111,207,74,.12)'};border:1px solid ${isAgent?'rgba(74,142,229,.25)':'rgba(111,207,74,.25)'};display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;margin-top:1px;color:${isAgent?'var(--blue)':'var(--green)'}">${EXEC_ICON[s.executor_type]||'?'}</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:500;color:var(--text);line-height:1.35;margin-bottom:2px">${s.title}</div>
          <div style="display:flex;align-items:center;gap:.375rem;flex-wrap:wrap">
            ${isAgent && s.agent_framework ? `<span style="font-size:9px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:${fwColor};background:rgba(255,255,255,.04);padding:1px 5px;border-radius:4px;border:1px solid rgba(255,255,255,.08)">${s.agent_framework}</span>` : ''}
            ${isAgent && s.estimated_runs_per_day ? `<span style="font-size:10px;color:var(--faint)">${s.estimated_runs_per_day}/day</span>` : ''}
            ${isAgent && s.estimated_value_monthly ? `<span style="font-size:10px;color:var(--green)">$${Math.round(s.estimated_value_monthly/1000)}K/mo</span>` : ''}
            ${!isAgent && s.human_name ? `<span style="font-size:10px;color:var(--muted)">◎ ${s.human_name}</span>` : ''}
            ${azure}
          </div>
        </div>
        <div style="font-size:9px;padding:2px 7px;border-radius:99px;background:${s.status==='active'?'var(--green-d)':'var(--panel2)'};color:${s.status==='active'?'var(--green)':'var(--faint)'};border:1px solid ${s.status==='active'?'var(--green-line)':'var(--line)'};white-space:nowrap;flex-shrink:0">${s.status}</div>
      </div>`;
  }).join('');

  const acctHtml = accts.map(a => {
    const havColor = (a.hav_score||0) >= 0.70 ? 'var(--green)' : (a.hav_score||0) >= 0.55 ? 'var(--amber)' : 'var(--faint)';
    return `<div style="display:flex;align-items:center;gap:.5rem;padding:.375rem 0;border-bottom:1px solid var(--line2)">
      <div style="width:6px;height:6px;border-radius:50%;background:${havColor};flex-shrink:0"></div>
      <div style="flex:1;font-size:12px;color:var(--muted)">${a.human_name}</div>
      ${a.is_vc ? `<span class="badge vc" style="font-size:9px">VC</span>` : ''}
      <div style="font-size:11px;font-weight:600;color:${havColor}">${a.hav_score!=null ? a.hav_score.toFixed(2) : '—'}</div>
      <div style="font-size:9px;color:var(--faint);min-width:40px;text-align:right">${a.role}</div>
    </div>`;
  }).join('');

  const valueStr = dg.total_value_monthly
    ? `$${Math.round(dg.total_value_monthly/1000)}K/mo` : '';

  const borderColor = col;

  return `
    <div style="background:var(--panel2);border:1px solid var(--line);border-top:3px solid ${borderColor};border-radius:10px;overflow:hidden${compact ? '' : ';margin-bottom:1rem'}">
      <!-- Dept header -->
      <div style="padding:1rem 1.125rem .75rem;border-bottom:1px solid var(--line)">
        <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem">
          <span style="font-size:15px;color:${col}">${icon}</span>
          <span style="font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:${col}">${dept}</span>
          <span style="margin-left:auto;font-size:11px;font-weight:600;color:var(--green)">${valueStr}</span>
        </div>
        <div style="font-size:13px;font-weight:500;color:var(--text);line-height:1.4;margin-bottom:.5rem">${dg.title}</div>
        ${_progressBar(dg.current_value, dg.target_value, dg.unit)}
      </div>
      <!-- Plan steps -->
      <div style="padding:.625rem 1.125rem 0">
        <div style="font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin-bottom:.25rem">Plan Steps (${steps.length})</div>
        ${stepsHtml}
      </div>
      <!-- Accountability -->
      <div style="padding:.625rem 1.125rem 1rem">
        <div style="font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin-bottom:.25rem">Accountability (${accts.length})</div>
        ${acctHtml || '<div style="font-size:11px;color:var(--faint);padding:.25rem 0">No accountability assigned</div>'}
      </div>
    </div>`;
}

async function seedGoals() {
  const btn = event.target;
  btn.disabled = true; btn.textContent = 'Seeding…';
  try {
    const r = await fetch('/api/v1/goal-planning/seed/market360', { method: 'POST' });
    if (!r.ok) throw new Error((await r.json()).detail || 'Failed');
    const d = await r.json();
    toast(`Market360 GPA seeded — ${d.seeded} goals`, 'success');
    loadGoals();
  } catch(e) {
    toast(`Error: ${e.message}`, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Seed Market360 GPA';
  }
}

// ─── INIT ────────────────────────────────────────────────────────────────
checkHealth();
loadView('dashboard');
setInterval(checkHealth, 30000);
// Preload signals so badge appears immediately
setTimeout(() => loadSignals(true), 1500);
// Show onboarding wizard for first-time users
setTimeout(showWizard, 600);
