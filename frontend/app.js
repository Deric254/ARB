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
  document.getElementById('kpiPortfolio').textContent = '$' + parseFloat(data.portfolio_usdt_value || 0).toFixed(2);

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
    html += `<tr><td><strong>${name.toUpperCase()}</strong></td><td style="color:${color}">${status}</td><td>${info.msgs || 0}</td><td>${JSON.stringify(info.balances || {})}</td></tr>`;
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
  document.getElementById('analyticsPage').classList.toggle('hidden', page !== 'analytics');
  document.getElementById('settingsPage').classList.toggle('hidden', page !== 'settings');
  document.querySelectorAll('.nav-item').forEach((el, i) => {
    const isActive = (page === 'dashboard' && i === 0) || (page === 'trades' && i === 1) ||
                      (page === 'analytics' && i === 2) || (page === 'settings' && i === 3);
    el.classList.toggle('active', isActive);
  });
  const titles = { dashboard: 'Dashboard', trades: 'Trade Log', analytics: 'Analytics', settings: 'Settings' };
  document.getElementById('pageTitle').textContent = titles[page] || 'Dashboard';
  if (page === 'trades') loadTrades();
  if (page === 'settings') loadSettingsConfig();
  if (page === 'analytics') loadAnalytics();
}

function exportAnalyticsCsv() {
  window.open(`${API_BASE}/api/analytics/export`, '_blank');
}

async function loadAnalytics() {
  try {
    const res = await fetch(`${API_BASE}/api/analytics`);
    const a = await res.json();

    document.getElementById('anTotalPnl').textContent = '$' + parseFloat(a.total_pnl).toFixed(2);
    document.getElementById('anTotalPnl').style.color = parseFloat(a.total_pnl) >= 0 ? 'var(--green)' : 'var(--red)';
    document.getElementById('anWinRate').textContent = a.win_rate_pct + '%';
    document.getElementById('anCaptureRate').textContent = a.capture_rate_pct + '%';
    document.getElementById('anTrades').textContent = a.total_trades;
    document.getElementById('anBestTrade').textContent = '$' + parseFloat(a.best_trade).toFixed(2);
    document.getElementById('anAvgWin').textContent = '$' + parseFloat(a.avg_win).toFixed(2);

    const pairBody = document.getElementById('pairPerfTable');
    pairBody.innerHTML = a.pair_performance.map(p => `
      <tr>
        <td>${p.pair}</td>
        <td>${p.trades}</td>
        <td style="color:${parseFloat(p.total_pnl) >= 0 ? 'var(--green)' : 'var(--red)'}">$${parseFloat(p.total_pnl).toFixed(2)}</td>
        <td>$${parseFloat(p.avg_pnl).toFixed(2)}</td>
      </tr>
    `).join('') || '<tr><td colspan="4" style="color:var(--text-dim)">No trades yet</td></tr>';

    const skipBody = document.getElementById('skipReasonTable');
    const reasons = Object.entries(a.skip_reasons || {});
    skipBody.innerHTML = reasons.map(([reason, count]) => `
      <tr><td>${reason}</td><td>${count}</td></tr>
    `).join('') || '<tr><td colspan="2" style="color:var(--text-dim)">No skipped opportunities</td></tr>';
  } catch (e) { console.log('Could not load analytics', e); }
}

function showSettingsTab(id, tabEl) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  tabEl.classList.add('active');
  document.getElementById(id + 'Tab').classList.add('active');
}

async function saveKeys(exchange) {
  const data = {
    api_key: document.getElementById(exchange + '_key').value,
    api_secret: document.getElementById(exchange + '_secret').value,
    passphrase: document.getElementById(exchange + '_passphrase')?.value || ''
  };
  try {
    const res = await fetch(`${API_BASE}/api/config/keys/${exchange}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const json = await res.json();
    alert(json.ok ? `${exchange.toUpperCase()} keys saved!` : 'Failed to save');
  } catch (e) { alert('Error: ' + e.message); }
}

async function saveTradingConfig() {
  const data = {
    symbol: document.getElementById('symbol').value,
    target_volume: document.getElementById('target_volume').value,
    min_profit_usd: document.getElementById('min_profit').value,
    max_slippage_pct: document.getElementById('max_slippage').value,
    vwap_depth: parseInt(document.getElementById('vwap_depth').value),
    poll_interval_ms: parseInt(document.getElementById('poll_ms').value),
    max_daily_trades: parseInt(document.getElementById('max_trades').value),
    max_daily_loss_usd: document.getElementById('max_loss').value,
    cooldown_seconds: parseInt(document.getElementById('cooldown').value),
    max_drawdown_pct: document.getElementById('drawdown').value,
    position_sizing_mode: document.getElementById('sizing_mode').value,
    position_size_pct: document.getElementById('size_pct').value,
    fixed_cost_usd: document.getElementById('fixed_cost').value,
    paper_trading: true,
    demo_mode: false
  };
  try {
    const res = await fetch(`${API_BASE}/api/config/trading`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const json = await res.json();
    alert(json.ok ? 'Trading settings saved!' : 'Failed to save');
  } catch (e) { alert('Error: ' + e.message); }
}

function updatePreview() {
  document.getElementById('previewName').textContent = document.getElementById('brand_name').value || 'ARB Pro';
  document.getElementById('previewSlogan').textContent = document.getElementById('brand_slogan').value || 'High-Frequency Cross-Exchange Arbitrage';
}

async function saveBranding() {
  const data = {
    name: document.getElementById('brand_name').value,
    slogan: document.getElementById('brand_slogan').value
  };
  try {
    const res = await fetch(`${API_BASE}/api/config/branding`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const json = await res.json();
    if (json.ok) {
      alert('Branding saved! Refreshing sidebar...');
      loadBranding();
    } else {
      alert('Failed to save');
    }
  } catch (e) { alert('Error: ' + e.message); }
}

async function selectLogoFile() {
  if (window.electronAPI) {
    const path = await window.electronAPI.selectLogo();
    if (path) alert('Selected: ' + path + '\nCopy this file to the app folder as logo.png, then click Refresh Preview.');
  } else {
    alert('Logo selection available in desktop app only.');
  }
}

async function loadSettingsConfig() {
  try {
    const res = await fetch(`${API_BASE}/api/config`);
    const cfg = await res.json();
    if (cfg.trading) {
      const t = cfg.trading;
      document.getElementById('symbol').value = t.symbol || 'BTC/USDT';
      document.getElementById('target_volume').value = t.target_volume || '0.001';
      document.getElementById('min_profit').value = t.min_profit_usd || '15.0';
      document.getElementById('max_slippage').value = t.max_slippage_pct || '0.1';
      document.getElementById('vwap_depth').value = t.vwap_depth || 20;
      document.getElementById('poll_ms').value = t.poll_interval_ms || 50;
      document.getElementById('max_trades').value = t.max_daily_trades || 50;
      document.getElementById('max_loss').value = t.max_daily_loss_usd || '500';
      document.getElementById('cooldown').value = t.cooldown_seconds || 30;
      document.getElementById('drawdown').value = t.max_drawdown_pct || '0.05';
      document.getElementById('sizing_mode').value = t.position_sizing_mode || 'fixed';
      document.getElementById('size_pct').value = t.position_size_pct || '0.1';
      document.getElementById('fixed_cost').value = t.fixed_cost_usd || '2.50';
      document.getElementById('sizePctGroup').style.display = (t.position_sizing_mode === 'pct_of_balance') ? 'block' : 'none';
    }
    if (cfg.branding) {
      document.getElementById('brand_name').value = cfg.branding.name || 'ARB Pro';
      document.getElementById('brand_slogan').value = cfg.branding.slogan || '';
      updatePreview();
    }
  } catch (e) { console.log('Could not load settings config', e); }
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

if (window.electronAPI && window.electronAPI.onNavigateSettings) {
  window.electronAPI.onNavigateSettings(() => showPage('settings'));
}
