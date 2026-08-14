const API_BASE = 'http://localhost:8765';
let ws = null;
let charts = {};
let historyData = { timestamps: [], pnl: [], detected: [], executed: [], spread_a: [], spread_b: [], price_a: [], price_b: [], circuit: [] };

// Chart.js defaults
Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#333';
Chart.defaults.font.family = "'Segoe UI', monospace";

function initCharts() {
  const common = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#e0e0e0' } } }, scales: { x: { ticks: { color: '#888' }, grid: { color: '#333' } }, y: { ticks: { color: '#888' }, grid: { color: '#333' } } } };

  charts.pnl = new Chart(document.getElementById('pnlChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'P&L ($)', data: [], borderColor: '#00ff88', backgroundColor: '#00ff8833', fill: true, tension: 0.3, pointRadius: 0 }] },
    options: common
  });
  charts.lat = new Chart(document.getElementById('latChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'Latency (ms)', data: [], borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0 }] },
    options: common
  });
  charts.sig = new Chart(document.getElementById('sigChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'Detected', data: [], borderColor: '#ffaa00', backgroundColor: '#ffaa0033', fill: true, tension: 0.3, pointRadius: 0 }, { label: 'Executed', data: [], borderColor: '#00ff88', backgroundColor: '#00ff8833', fill: true, tension: 0.3, pointRadius: 0 }] },
    options: common
  });
  charts.spread = new Chart(document.getElementById('spreadChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'Ex A', data: [], borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0 }, { label: 'Ex B', data: [], borderColor: '#ff4444', backgroundColor: '#ff444433', fill: false, tension: 0.3, pointRadius: 0 }] },
    options: common
  });
  charts.price = new Chart(document.getElementById('priceChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'Ex A', data: [], borderColor: '#66ccff', backgroundColor: '#66ccff33', fill: false, tension: 0.3, pointRadius: 0 }, { label: 'Ex B', data: [], borderColor: '#ffaa00', backgroundColor: '#ffaa0033', fill: false, tension: 0.3, pointRadius: 0 }] },
    options: common
  });
  charts.circuit = new Chart(document.getElementById('circuitChart'), {
    type: 'line', data: { labels: [], datasets: [{ label: 'Closed=1 / Open=0', data: [], borderColor: '#ff4444', backgroundColor: '#ff444433', fill: true, tension: 0, pointRadius: 0, stepped: true }] },
    options: { ...common, scales: { ...common.scales, y: { ...common.scales.y, min: -0.1, max: 1.1 } } }
  });
}

function updateKPIs(data) {
  document.getElementById('kpiPnl').textContent = '$' + parseFloat(data.total_pnl || 0).toFixed(2);
  document.getElementById('kpiRate').textContent = (data.success_rate || 0) + '%';
  document.getElementById('kpiDetected').textContent = data.detected || 0;
  document.getElementById('kpiExecuted').textContent = data.executed || 0;
  document.getElementById('kpiLatency').textContent = '50 ms';
  document.getElementById('kpiSpread').textContent = '12 bps';
  document.getElementById('kpiDaily').textContent = data.daily_trades || 0;
  document.getElementById('kpiCircuit').textContent = data.circuit_state || 'CLOSED';
  document.getElementById('kpiCircuit').style.color = (data.circuit_state === 'CLOSED') ? 'var(--green)' : 'var(--red)';

  const badge = document.getElementById('modeBadge');
  badge.textContent = data.mode || 'STOPPED';
  badge.className = 'mode-badge mode-' + (data.mode || 'stopped').toLowerCase();
}

function updateExTable(data) {
  const tbody = document.getElementById('exTable');
  let html = '';
  for (const [name, info] of Object.entries(data.exchanges || {})) {
    const color = info.connected ? 'var(--green)' : 'var(--red)';
    const status = info.connected ? 'CONNECTED' : 'DISCONNECTED';
    html += `<tr><td><strong>${name.toUpperCase()}</strong></td><td style="color:${color}">${status}</td><td>${info.messages || 0}</td><td>${JSON.stringify(info.balances || {})}</td></tr>`;
  }
  tbody.innerHTML = html;
}

async function loadTrades() {
  try {
    const res = await fetch(`${API_BASE}/api/trades?limit=50`);
    const trades = await res.json();
    const tbody = document.getElementById('tradeTable');
    let html = '';
    for (const t of trades) {
      const cls = t.status === 'FILLED' ? 'status-filled' : 'status-failed';
      html += `<tr><td>${t.ts ? t.ts.slice(0,19) : ''}</td><td>${t.eid ? t.eid.slice(0,12) : ''}</td><td>${t.buy_ex}</td><td>${t.sell_ex}</td><td>${parseFloat(t.buy_px || 0).toFixed(2)}</td><td>${parseFloat(t.sell_px || 0).toFixed(2)}</td><td>${t.vol}</td><td style="color:${t.pnl >= 0 ? 'var(--green)' : 'var(--red)'}">$${parseFloat(t.pnl || 0).toFixed(2)}</td><td class="${cls}">${t.status}</td><td>${t.latency_ms} ms</td></tr>`;
    }
    tbody.innerHTML = html || '<tr><td colspan="10" style="text-align:center;color:var(--text-dim)">No trades yet</td></tr>';
  } catch (e) { console.log('Trade load error', e); }
}

function updateCharts(data) {
  const hist = data.history || [];
  if (hist.length === 0) return;

  const labels = hist.map(h => new Date(h.ts * 1000).toLocaleTimeString());
  const pnls = hist.map(h => h.pnl || 0);
  const dets = hist.map(h => h.detected || 0);
  const execs = hist.map(h => h.executed || 0);

  charts.pnl.data.labels = labels;
  charts.pnl.data.datasets[0].data = pnls;
  charts.pnl.update('none');

  charts.sig.data.labels = labels;
  charts.sig.data.datasets[0].data = dets;
  charts.sig.data.datasets[1].data = execs;
  charts.sig.update('none');
}

async function fetchStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/status`);
    const data = await res.json();
    updateKPIs(data);
    updateExTable(data);
    updateCharts(data);

    const running = data.running;
    document.getElementById('startBtn').classList.toggle('hidden', running);
    document.getElementById('stopBtn').classList.toggle('hidden', !running);
  } catch (e) { console.log('Status fetch error', e); }
}

async function startEngine() {
  const mode = document.getElementById('modeSelect').value;
  try {
    const res = await fetch(`${API_BASE}/api/engine/start`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode})
    });
    const json = await res.json();
    if (json.ok) {
      document.getElementById('startBtn').classList.add('hidden');
      document.getElementById('stopBtn').classList.remove('hidden');
    } else {
      alert('Failed to start: ' + (json.message || 'Check API keys'));
    }
  } catch (e) { alert('Error: ' + e.message); }
}

async function stopEngine() {
  try {
    await fetch(`${API_BASE}/api/engine/stop`, {method: 'POST'});
    document.getElementById('startBtn').classList.remove('hidden');
    document.getElementById('stopBtn').classList.add('hidden');
  } catch (e) { alert('Error: ' + e.message); }
}

function showPage(page) {
  document.getElementById('dashboardPage').classList.toggle('hidden', page !== 'dashboard');
  document.getElementById('tradesPage').classList.toggle('hidden', page !== 'trades');
  document.querySelectorAll('.nav-item').forEach((el, i) => {
    el.classList.toggle('active', (page === 'dashboard' && i === 0) || (page === 'trades' && i === 1));
  });
  if (page === 'trades') loadTrades();
}

function openSettings() {
  if (window.electronAPI) {
    window.electronAPI.openSettings();
  } else {
    window.open('settings.html', '_blank', 'width=900,height=700');
  }
}

async function loadBranding() {
  try {
    const res = await fetch(`${API_BASE}/api/config`);
    const cfg = await res.json();
    if (cfg.branding) {
      document.getElementById('brandName').textContent = cfg.branding.name || 'ARB Pro';
      document.getElementById('brandSlogan').textContent = cfg.branding.slogan || 'High-Frequency Arbitrage';
    }
  } catch (e) {}
}

// WebSocket for real-time updates
function connectWS() {
  try {
    ws = new WebSocket('ws://localhost:8765/ws');
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      updateKPIs(data);
      updateExTable(data);
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
  } catch (e) { setTimeout(connectWS, 3000); }
}

// Init
initCharts();
loadBranding();
fetchStatus();
loadTrades();
connectWS();
setInterval(fetchStatus, 3000);
setInterval(loadTrades, 5000);
