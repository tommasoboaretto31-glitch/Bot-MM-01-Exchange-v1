/**
 * NuovoBot Dashboard — real-time client v2
 * Connects via WebSocket for live updates with volume tracking
 */

const WS_URL = `ws://${location.host}/ws`;
let ws = null;
let reconnectTimer = null;
let equityData = [];
const VOLUME_TARGET = 100000; // Weekly target

// ── WebSocket Connection ──────────────────────────

function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        addLog('✅ WebSocket connected');
        clearTimeout(reconnectTimer);
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateDashboard(data);
        } catch (e) {
            console.error('Parse error:', e);
        }
    };

    ws.onclose = () => {
        addLog('⚠️ WebSocket disconnected — reconnecting...');
        reconnectTimer = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

// ── Update Dashboard ──────────────────────────────

function updateDashboard(data) {
    if (data.bot) updateStatus(data.bot);
    if (data.signal) updateSignal(data.signal);
    if (data.performance) updatePerformance(data.performance);
    if (data.positions) updatePositions(data.positions);
    if (data.volumes) updateVolumes(data.volumes);
}

function updateStatus(bot) {
    const badge = document.getElementById('status-badge');
    const text = document.getElementById('status-text');

    badge.className = `status-badge ${bot.status}`;
    text.textContent = bot.status.toUpperCase();

    const mode = document.getElementById('stat-mode');
    mode.textContent = bot.paper_mode ? 'PAPER' : 'LIVE';
    mode.className = `stat-value ${bot.paper_mode ? 'neutral' : 'positive'}`;

    if (bot.start_time) {
        const elapsed = Math.floor(Date.now() / 1000 - bot.start_time);
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        const s = elapsed % 60;
        document.getElementById('stat-uptime').textContent = `Uptime: ${h}h ${m}m ${s}s`;
    }
}

function updateSignal(signal) {
    const regimeTag = document.getElementById('regime-tag');
    const regime = (signal.regime || 'market_making').toUpperCase().replace('_', ' ');
    regimeTag.textContent = regime;
    regimeTag.className = `regime-tag ${signal.regime || 'market_making'}`;

    document.getElementById('bias-direction').textContent = signal.bias_direction || 'NEUTRAL';
    document.getElementById('bias-score').textContent = (signal.bias_score || 0).toFixed(2);

    const score = signal.bias_score || 0;
    const longBar = document.getElementById('bias-long');
    const shortBar = document.getElementById('bias-short');

    if (score > 0) {
        longBar.style.width = `${Math.min(score * 50, 50)}%`;
        shortBar.style.width = '0%';
    } else if (score < 0) {
        shortBar.style.width = `${Math.min(Math.abs(score) * 50, 50)}%`;
        longBar.style.width = '0%';
    } else {
        longBar.style.width = '0%';
        shortBar.style.width = '0%';
    }
}

function updatePerformance(perf) {
    const capital = perf.capital || 50;
    const initial = perf.initial_capital || 50;
    const pnl = perf.pnl_today || 0;
    const ret = perf.total_return_pct || 0;

    document.getElementById('stat-capital').textContent = `$${capital.toFixed(2)}`;
    document.getElementById('stat-capital').className =
        `stat-value ${capital >= initial ? 'positive' : 'negative'}`;

    document.getElementById('stat-initial').textContent = `Initial: $${initial.toFixed(2)}`;

    const pnlSign = pnl >= 0 ? '+' : '-';
    document.getElementById('stat-pnl').textContent = `${pnlSign}$${Math.abs(pnl).toFixed(2)}`;
    document.getElementById('stat-pnl').className =
        `stat-value ${pnl >= 0 ? 'positive' : 'negative'}`;

    document.getElementById('stat-return').textContent = `Return: ${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%`;
    
    const placed = perf.orders_placed || 0;
    const trades = perf.trades_today || 0;
    const fillRate = placed > 0 ? (trades / placed) * 100 : 0;
    document.getElementById('stat-trades').textContent = trades;
    document.getElementById('stat-fillrate').textContent = `${fillRate.toFixed(1)}%`;
    
    const apiTotal = perf.api_calls_total || 0;
    const apiFailed = perf.api_calls_failed || 0;
    const errRate = apiTotal > 0 ? (apiFailed / apiTotal) * 100 : 0;
    document.getElementById('stat-errrate').textContent = `${errRate.toFixed(1)}%`;

    // Update equity chart
    equityData.push(capital);
    if (equityData.length > 500) equityData.shift();
    drawEquityChart();
}

function updatePositions(positions) {
    const tbody = document.getElementById('positions-body');

    if (!positions || positions.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 24px;">
                No open positions
            </td></tr>`;
        document.getElementById('stat-positions').textContent = '0';
        return;
    }

    document.getElementById('stat-positions').textContent = positions.length;

    tbody.innerHTML = positions.map(p => {
        const pnlClass = (p.pnl || 0) >= 0 ? 'positive' : 'negative';
        const sideClass = p.side === 'LONG' ? 'side-long' : 'side-short';
        return `
            <tr>
                <td>${p.symbol || '--'}</td>
                <td class="${sideClass}">${p.side || '--'}</td>
                <td>${(p.size || 0).toFixed(4)}</td>
                <td>$${(p.entry || 0).toFixed(4)}</td>
                <td>$${(p.mark || 0).toFixed(4)}</td>
                <td class="stat-value ${pnlClass}" style="font-size: 12px;">
                    ${(p.pnl || 0) >= 0 ? '+$' : '-$'}${Math.abs(p.pnl || 0).toFixed(2)}
                </td>
            </tr>`;
    }).join('');
}

// ── VOLUME TRACKING ───────────────────────────────

function updateVolumes(volumes) {
    const totalVol = volumes.total || 0;
    const markets = volumes.per_market || {};
    const uptimeHours = volumes.uptime_hours || 0;

    // Hero section
    document.getElementById('vol-total').textContent = formatUSD(totalVol);

    const progress = Math.min((totalVol / VOLUME_TARGET) * 100, 100);
    document.getElementById('vol-progress').textContent = `${progress.toFixed(1)}%`;
    document.getElementById('vol-bar').style.width = `${progress}%`;

    // Rate per hour
    const rate = uptimeHours > 0 ? totalVol / uptimeHours : 0;
    document.getElementById('vol-rate').textContent = formatUSD(rate);

    // Per-market breakdown
    const container = document.getElementById('volume-markets');
    const marketEntries = Object.entries(markets);

    if (marketEntries.length === 0) {
        container.innerHTML = '<div class="volume-market-empty">Waiting for trades...</div>';
        return;
    }

    // Sort by volume descending
    marketEntries.sort((a, b) => b[1].volume - a[1].volume);
    const maxVol = marketEntries[0][1].volume || 1;

    container.innerHTML = marketEntries.map(([symbol, data]) => {
        const vol = data.volume || 0;
        const trades = data.trades || 0;
        const pnl = data.pnl || 0;
        const barWidth = (vol / maxVol) * 100;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';

        return `
            <div class="volume-market-row">
                <div class="volume-market-info">
                    <span class="volume-market-symbol">${symbol}</span>
                    <span class="volume-market-trades">${trades} trades</span>
                </div>
                <div class="volume-market-bar-container">
                    <div class="volume-market-bar-fill" style="width: ${barWidth}%"></div>
                </div>
                <div class="volume-market-values">
                    <span class="volume-market-vol">${formatUSD(vol)}</span>
                    <span class="volume-market-pnl ${pnlClass}">${pnl >= 0 ? '+$' : '-$'}${Math.abs(pnl).toFixed(2)}</span>
                </div>
            </div>`;
    }).join('');
}

function formatUSD(val) {
    if (val >= 1000000) return `$${(val / 1000000).toFixed(2)}M`;
    if (val >= 1000) return `$${(val / 1000).toFixed(1)}K`;
    return `$${val.toFixed(2)}`;
}

// ── Equity Chart (Canvas) ─────────────────────────

function drawEquityChart() {
    const canvas = document.getElementById('equity-canvas');
    if (!canvas || equityData.length < 2) return;

    const ctx = canvas.getContext('2d');
    const container = canvas.parentElement;
    canvas.width = container.offsetWidth;
    canvas.height = container.offsetHeight;

    const W = canvas.width;
    const H = canvas.height;
    const pad = 40;

    ctx.clearRect(0, 0, W, H);

    const minVal = Math.min(...equityData) * 0.998;
    const maxVal = Math.max(...equityData) * 1.002;
    const range = maxVal - minVal || 1;

    const xStep = (W - pad * 2) / (equityData.length - 1);

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, pad, 0, H - pad);
    const lastVal = equityData[equityData.length - 1];
    const initVal = equityData[0];

    if (lastVal >= initVal) {
        gradient.addColorStop(0, 'rgba(16, 185, 129, 0.1)');
        gradient.addColorStop(1, 'rgba(16, 185, 129, 0.0)');
    } else {
        gradient.addColorStop(0, 'rgba(239, 68, 68, 0.1)');
        gradient.addColorStop(1, 'rgba(239, 68, 68, 0.0)');
    }

    // Line + fill path
    ctx.beginPath();
    ctx.moveTo(pad, H - pad - ((equityData[0] - minVal) / range) * (H - pad * 2));

    for (let i = 1; i < equityData.length; i++) {
        const x = pad + i * xStep;
        const y = H - pad - ((equityData[i] - minVal) / range) * (H - pad * 2);
        ctx.lineTo(x, y);
    }

    // Stroke line
    ctx.strokeStyle = lastVal >= initVal ? '#10b981' : '#ef4444';
    ctx.lineWidth = 2.5;
    ctx.stroke();

    // Fill area
    ctx.lineTo(pad + (equityData.length - 1) * xStep, H - pad);
    ctx.lineTo(pad, H - pad);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // Current value label
    ctx.fillStyle = lastVal >= initVal ? '#10b981' : '#ef4444';
    ctx.font = '600 13px Outfit, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(`$${lastVal.toFixed(2)}`, W - 10, 25);

    // Min/Max labels
    ctx.fillStyle = '#555570';
    ctx.font = '10px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(`$${maxVal.toFixed(2)}`, 4, pad + 10);
    ctx.fillText(`$${minVal.toFixed(2)}`, 4, H - pad - 4);
}

// ── Bot Control ───────────────────────────────────

async function controlBot(command) {
    try {
        const resp = await fetch('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command }),
        });
        const data = await resp.json();
        addLog(`Bot ${command}: ${data.status}`);
    } catch (e) {
        addLog(`Control error: ${e.message}`);
    }
}

// ── Activity Log ──────────────────────────────────

function addLog(msg) {
    const list = document.getElementById('log-list');
    const now = new Date().toTimeString().slice(0, 8);
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `
        <span class="log-time">${now}</span>
        <span class="log-msg">${msg}</span>
    `;
    list.prepend(entry);

    while (list.children.length > 200) {
        list.removeChild(list.lastChild);
    }
}

// ── Poll fallback (if WebSocket fails) ────────────

async function pollFull() {
    try {
        const [statusResp, perfResp, posResp, volResp] = await Promise.all([
            fetch('/api/status'),
            fetch('/api/performance'),
            fetch('/api/positions'),
            fetch('/api/volumes'),
        ]);
        const status = await statusResp.json();
        const perf = await perfResp.json();
        const pos = await posResp.json();
        const vol = await volResp.json();

        if (status.bot) updateStatus(status.bot);
        if (status.signal) updateSignal(status.signal);
        updatePerformance(perf);
        if (pos.positions) updatePositions(pos.positions);
        updateVolumes(vol);
    } catch (e) { /* ignore */ }
}

// ── Clock ─────────────────────────────────────────

function updateClock() {
    document.getElementById('clock').textContent = new Date().toTimeString().slice(0, 8);
}

// ── Init ──────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    connect();
    setInterval(pollFull, 5000);
    setInterval(updateClock, 1000);
    updateClock();
    addLog('Dashboard initialized');
});
