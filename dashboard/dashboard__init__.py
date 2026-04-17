"""
dashboard/__init__.py — Live monitoring dashboard.

Three views:
  LIVE    — open positions, per-strategy risk/DD bars, halt status, live R
  TRADES  — every completed trade this session (reads trade_log.csv)
  HISTORY — daily summary across all sessions (reads daily_summary.csv)

Run alongside the orchestrator:
  from orchestrator.dashboard import start_dashboard
  start_dashboard(orchestrator)

Access at http://localhost:8050
"""

import csv
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..main import Orchestrator

from .. import config

log = logging.getLogger("dashboard")

_ORCHESTRATOR = None


def start_dashboard(orch, port: int = None):
    global _ORCHESTRATOR
    _ORCHESTRATOR = orch
    port = port or config.DASHBOARD_PORT

    def serve():
        server = HTTPServer(("0.0.0.0", port), DashboardHandler)
        log.info(f"Dashboard running at http://localhost:{port}")
        server.serve_forever()

    t = threading.Thread(target=serve, daemon=True, name="dashboard")
    t.start()
    return t


# =============================================================================
# REQUEST HANDLER
# =============================================================================

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_GET(self):
        if self.path == "/api/status":
            self._json(_build_status())
        elif self.path == "/api/trades":
            self._json(_read_trades())
        elif self.path == "/api/history":
            self._json(_read_history())
        else:
            self._html(_build_html())

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


# =============================================================================
# DATA BUILDERS
# =============================================================================

def _build_status() -> dict:
    if _ORCHESTRATOR is None:
        return {"error": "orchestrator not connected"}
    orch = _ORCHESTRATOR
    risk = orch.risk
    risk_sum = risk.summary()

    # Per-strategy status enriched with config targets
    strategy_rows = []
    for strat_id, cfg in config.STRATEGY_RISK.items():
        pnl  = risk_sum["strategy_pnl"].get(strat_id, 0.0)
        dd   = cfg["max_dd"]
        risk_pt = cfg["risk_per_trade"]
        pct  = max(0.0, min(1.0, abs(pnl) / dd)) if pnl < 0 else 0.0
        halted = strat_id in risk_sum.get("halted_strategies", [])
        strategy_rows.append({
            "id":             strat_id,
            "pnl":            round(pnl, 2),
            "max_dd":         dd,
            "risk_per_trade": risk_pt,
            "dd_pct":         round(pct * 100, 1),
            "halted":         halted,
        })

    return {
        "session":            orch._session_date,
        "halted":             risk.halted,
        "halted_strategies":  risk_sum.get("halted_strategies", []),
        "daily_r":            round(risk.daily_r_total, 3),
        "daily_dollars":      round(risk.daily_pnl_dollars, 2),
        "signals_fired":      orch._signal_count,
        "signals_accepted":   orch._signals_accepted,
        "wins":               orch._wins,
        "losses":             orch._losses,
        "eod_closes":         orch._eod_closes,
        "max_sim":            orch._max_sim,
        "positions":          risk_sum["positions"],
        "strategy_rows":      strategy_rows,
        "portfolio_dd_limit": config.DAILY_LOSS_LIMIT_DOLLARS,
    }


def _read_csv(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        log.warning(f"Dashboard CSV read failed ({path}): {e}")
        return []


def _read_trades() -> dict:
    """Return today's trades plus the full log, newest first."""
    rows = _read_csv(config.TRADE_LOG_CSV)
    today = _ORCHESTRATOR._session_date if _ORCHESTRATOR else ""
    today_rows = [r for r in rows if r.get("session") == today]
    return {
        "today":   list(reversed(today_rows)),
        "all":     list(reversed(rows[-200:])),  # cap at 200 for payload size
    }


def _read_history() -> list:
    rows = _read_csv(config.DAILY_SUMMARY_CSV)
    return list(reversed(rows[-60:]))  # last 60 sessions, newest first


# =============================================================================
# HTML
# =============================================================================

def _build_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ORCH // MONITOR</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #080b0f;
  --bg1:      #0d1219;
  --bg2:      #111820;
  --bg3:      #1a2333;
  --border:   #1e2d42;
  --border2:  #253347;
  --text:     #c8d8e8;
  --dim:      #4a6180;
  --accent:   #00c8ff;
  --green:    #00e5a0;
  --red:      #ff3d5a;
  --yellow:   #f5c400;
  --amber:    #ff8c00;
  --mono:     'IBM Plex Mono', monospace;
  --sans:     'IBM Plex Sans', sans-serif;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── HEADER ── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 28px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
}

.logo {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 4px;
  color: var(--accent);
  text-transform: uppercase;
}
.logo span { color: var(--dim); font-weight: 300; }

.header-right {
  display: flex;
  align-items: center;
  gap: 24px;
}

.session-badge {
  font-size: 11px;
  color: var(--dim);
  letter-spacing: 1px;
}
.session-badge b { color: var(--text); font-weight: 500; }

.pulse {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(0,229,160,0.4);
  animation: pulse 2s infinite;
}
.pulse.halted { background: var(--red); box-shadow: 0 0 0 0 rgba(255,61,90,0.4); animation: pulse-red 1s infinite; }

@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(0,229,160,0.4); }
  70%  { box-shadow: 0 0 0 8px rgba(0,229,160,0); }
  100% { box-shadow: 0 0 0 0 rgba(0,229,160,0); }
}
@keyframes pulse-red {
  0%,100% { box-shadow: 0 0 0 0 rgba(255,61,90,0.5); }
  50%  { box-shadow: 0 0 0 10px rgba(255,61,90,0); }
}

/* ── HALT BANNER ── */
.halt-banner {
  display: none;
  background: linear-gradient(90deg, #3a0010, #200008);
  border-bottom: 1px solid var(--red);
  padding: 10px 28px;
  color: var(--red);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 2px;
  text-transform: uppercase;
}
.halt-banner.on { display: flex; align-items: center; gap: 10px; }

/* ── TABS ── */
.tabs {
  display: flex;
  gap: 0;
  padding: 0 28px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
}
.tab {
  padding: 12px 20px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--dim);
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

/* ── MAIN LAYOUT ── */
main { padding: 24px 28px; }
.pane { display: none; }
.pane.active { display: block; }

/* ── STAT GRID ── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  margin-bottom: 20px;
}

.stat {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 14px 16px;
  position: relative;
  overflow: hidden;
}
.stat::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--border2);
}
.stat.pos::before { background: var(--green); }
.stat.neg::before { background: var(--red); }
.stat.info::before { background: var(--accent); }

.stat-label {
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 8px;
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  line-height: 1;
  color: var(--text);
}
.stat-value.green { color: var(--green); }
.stat-value.red   { color: var(--red); }
.stat-value.accent { color: var(--accent); }
.stat-sub {
  font-size: 10px;
  color: var(--dim);
  margin-top: 5px;
}

/* ── TWO COL ── */
.two-col { display: grid; grid-template-columns: 1fr 380px; gap: 16px; margin-bottom: 20px; }

/* ── SECTION ── */
.section {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.section-head {
  padding: 10px 16px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--dim);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.section-head b { color: var(--accent); font-weight: 500; }

/* ── TABLE ── */
.data-table { width: 100%; border-collapse: collapse; }
.data-table th {
  padding: 9px 14px;
  text-align: left;
  font-size: 9px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--dim);
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-weight: 500;
  white-space: nowrap;
}
.data-table td {
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  white-space: nowrap;
}
.data-table tr:last-child td { border-bottom: none; }
.data-table tbody tr:hover { background: var(--bg2); }
.data-table .empty { text-align: center; color: var(--dim); padding: 24px; }

.dir-long  { color: var(--green); font-weight: 600; }
.dir-short { color: var(--red); font-weight: 600; }
.pos-r { font-weight: 600; }
.pos-r.green { color: var(--green); }
.pos-r.red   { color: var(--red); }

/* ── STRATEGY RISK BARS ── */
.strat-list { padding: 6px 0; }
.strat-row {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  display: grid;
  grid-template-columns: 130px 1fr 70px 60px;
  align-items: center;
  gap: 12px;
}
.strat-row:last-child { border-bottom: none; }
.strat-name {
  font-size: 11px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.strat-name.halted { color: var(--red); }
.strat-name.halted::after {
  content: ' HALT';
  font-size: 8px;
  letter-spacing: 1px;
  background: var(--red);
  color: var(--bg);
  padding: 1px 4px;
  border-radius: 2px;
  margin-left: 5px;
  vertical-align: middle;
}
.dd-bar-wrap {
  height: 4px;
  background: var(--bg3);
  border-radius: 2px;
  overflow: hidden;
}
.dd-bar {
  height: 100%;
  border-radius: 2px;
  background: var(--green);
  transition: width 0.4s ease;
}
.dd-bar.warn  { background: var(--yellow); }
.dd-bar.crit  { background: var(--red); }
.strat-pnl {
  font-size: 11px;
  text-align: right;
  font-weight: 500;
}
.strat-risk {
  font-size: 10px;
  color: var(--dim);
  text-align: right;
}

/* ── HISTORY ── */
.history-wrap { overflow-x: auto; }

/* ── TRADE LOG ── */
.trade-filter {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 10px;
  background: var(--bg2);
}
.filter-btn {
  padding: 4px 10px;
  font-size: 10px;
  font-family: var(--mono);
  letter-spacing: 1px;
  text-transform: uppercase;
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--dim);
  border-radius: 2px;
  cursor: pointer;
  transition: all 0.15s;
}
.filter-btn:hover { border-color: var(--accent); color: var(--accent); }
.filter-btn.active { background: var(--accent); border-color: var(--accent); color: var(--bg); font-weight: 600; }

/* ── TICKER ── */
.ticker {
  font-size: 10px;
  letter-spacing: 1px;
  color: var(--dim);
  padding: 5px 28px;
  background: var(--bg1);
  border-top: 1px solid var(--border);
  position: fixed;
  bottom: 0; left: 0; right: 0;
}
.ticker b { color: var(--accent); }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

/* exit reason badges */
.badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 2px;
  font-size: 9px;
  letter-spacing: 1px;
  font-weight: 600;
  text-transform: uppercase;
}
.badge-tp   { background: rgba(0,229,160,0.15); color: var(--green); }
.badge-stop { background: rgba(255,61,90,0.15);  color: var(--red); }
.badge-eod  { background: rgba(0,200,255,0.12);  color: var(--accent); }
.badge-amb  { background: rgba(245,196,0,0.15);  color: var(--yellow); }
</style>
</head>
<body>

<header>
  <div class="logo">ORCH <span>//</span> MONITOR</div>
  <div class="header-right">
    <div class="session-badge">SESSION <b id="hdr-session">—</b></div>
    <div class="session-badge">LAST UPDATE <b id="hdr-ts">—</b></div>
    <div class="pulse" id="pulse-dot"></div>
  </div>
</header>

<div class="halt-banner" id="halt-banner">
  <span>⬛</span> PORTFOLIO DAILY LOSS LIMIT HIT — ALL NEW SIGNALS HALTED
</div>

<nav class="tabs">
  <div class="tab active" data-pane="live">Live</div>
  <div class="tab" data-pane="trades">Trades</div>
  <div class="tab" data-pane="history">History</div>
</nav>

<main>

  <!-- ── LIVE PANE ── -->
  <div class="pane active" id="pane-live">

    <div class="stat-grid">
      <div class="stat" id="sc-dollars">
        <div class="stat-label">Daily P&L</div>
        <div class="stat-value" id="sv-dollars">—</div>
        <div class="stat-sub" id="sv-r">— R</div>
      </div>
      <div class="stat info">
        <div class="stat-label">Signals</div>
        <div class="stat-value accent" id="sv-signals">—</div>
        <div class="stat-sub" id="sv-accepted">— accepted</div>
      </div>
      <div class="stat pos">
        <div class="stat-label">Wins</div>
        <div class="stat-value green" id="sv-wins">0</div>
      </div>
      <div class="stat neg">
        <div class="stat-label">Losses</div>
        <div class="stat-value red" id="sv-losses">0</div>
      </div>
      <div class="stat info">
        <div class="stat-label">Win Rate</div>
        <div class="stat-value accent" id="sv-wr">—</div>
        <div class="stat-sub" id="sv-eod">— EOD closes</div>
      </div>
      <div class="stat info">
        <div class="stat-label">Open / Max Sim</div>
        <div class="stat-value accent" id="sv-open">0</div>
        <div class="stat-sub" id="sv-maxsim">max — today</div>
      </div>
    </div>

    <div class="two-col">

      <!-- Open Positions -->
      <div class="section">
        <div class="section-head">Open Positions <b id="pos-count">0</b></div>
        <table class="data-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Symbol</th>
              <th>Dir</th>
              <th>Shares</th>
              <th>Entry</th>
              <th>Stop</th>
              <th>TP</th>
              <th>R $</th>
            </tr>
          </thead>
          <tbody id="pos-body">
            <tr><td colspan="8" class="empty">No open positions</td></tr>
          </tbody>
        </table>
      </div>

      <!-- Strategy Risk Bars -->
      <div class="section">
        <div class="section-head">Strategy Risk / DD</div>
        <div class="strat-list" id="strat-list">
          <div class="strat-row"><div class="strat-name dim">Loading…</div></div>
        </div>
      </div>

    </div>
  </div>

  <!-- ── TRADES PANE ── -->
  <div class="pane" id="pane-trades">
    <div class="section">
      <div class="section-head">
        Trade Log
        <span id="trade-count" style="color:var(--dim);font-size:10px;"></span>
      </div>
      <div class="trade-filter">
        <button class="filter-btn active" data-filter="today">Today</button>
        <button class="filter-btn" data-filter="all">All Sessions</button>
      </div>
      <div style="overflow-x:auto;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Strategy</th>
              <th>Symbol</th>
              <th>Dir</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Stop</th>
              <th>TP</th>
              <th>Shares</th>
              <th>Result R</th>
              <th>P&L $</th>
              <th>Bars</th>
              <th>Exit Reason</th>
            </tr>
          </thead>
          <tbody id="trade-body">
            <tr><td colspan="13" class="empty">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── HISTORY PANE ── -->
  <div class="pane" id="pane-history">
    <div class="section">
      <div class="section-head">Daily Summary — Last 60 Sessions</div>
      <div style="overflow-x:auto;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Accepted</th>
              <th>Won</th>
              <th>Lost</th>
              <th>EOD</th>
              <th>Win %</th>
              <th>Total R</th>
              <th>Total $</th>
              <th>Max Sim</th>
              <th>Halted</th>
              <th>Strategies</th>
            </tr>
          </thead>
          <tbody id="hist-body">
            <tr><td colspan="11" class="empty">Loading…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

</main>

<div class="ticker">
  AUTO-REFRESH <b id="tick-countdown">5</b>s &nbsp;|&nbsp; PORT <b>8050</b>
</div>

<script>
// ── Tab switching ──────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('pane-' + t.dataset.pane).classList.add('active');
    if (t.dataset.pane === 'trades') loadTrades();
    if (t.dataset.pane === 'history') loadHistory();
  });
});

// ── Trade filter ──────────────────────────────────────────────────────────
let tradeFilter = 'today';
let tradeData   = { today: [], all: [] };

document.querySelectorAll('.filter-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    tradeFilter = b.dataset.filter;
    renderTrades();
  });
});

// ── Helpers ──────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt$ = v => (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
const fmtR = v => (v >= 0 ? '+' : '') + parseFloat(v).toFixed(3) + 'R';

function colorR(v) {
  const n = parseFloat(v);
  return isNaN(n) ? '' : n > 0 ? 'green' : n < 0 ? 'red' : '';
}

function exitBadge(reason) {
  if (!reason) return '';
  const r = reason.toLowerCase();
  if (r.includes('tp'))         return `<span class="badge badge-tp">TP</span>`;
  if (r.includes('stop'))       return `<span class="badge badge-stop">STOP</span>`;
  if (r.includes('eod'))        return `<span class="badge badge-eod">EOD</span>`;
  if (r.includes('ambiguous'))  return `<span class="badge badge-amb">AMB</span>`;
  return `<span class="badge" style="background:rgba(74,97,128,0.2);color:var(--dim)">${reason.substring(0,6).toUpperCase()}</span>`;
}

// ── Live status ───────────────────────────────────────────────────────────
async function loadLive() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    $('hdr-session').textContent = d.session || '—';
    $('hdr-ts').textContent = new Date().toLocaleTimeString();

    const halted = d.halted;
    $('halt-banner').className = halted ? 'halt-banner on' : 'halt-banner';
    $('pulse-dot').className   = halted ? 'pulse halted' : 'pulse';

    // Daily P&L
    const dol = d.daily_dollars;
    $('sv-dollars').textContent = fmt$(dol);
    $('sv-dollars').className   = 'stat-value ' + (dol >= 0 ? 'green' : 'red');
    $('sc-dollars').className   = 'stat ' + (dol >= 0 ? 'pos' : 'neg');
    $('sv-r').textContent       = fmtR(d.daily_r);

    // Signals
    $('sv-signals').textContent  = d.signals_fired;
    $('sv-accepted').textContent = d.signals_accepted + ' accepted';

    // Wins / losses
    $('sv-wins').textContent   = d.wins;
    $('sv-losses').textContent = d.losses;

    const total = d.wins + d.losses;
    const wr    = total > 0 ? Math.round(d.wins / total * 100) + '%' : '—';
    $('sv-wr').textContent  = wr;
    $('sv-eod').textContent = d.eod_closes + ' EOD closes';

    // Open positions
    $('sv-open').textContent   = d.positions.length;
    $('pos-count').textContent = d.positions.length;
    $('sv-maxsim').textContent = 'max ' + d.max_sim + ' today';

    const pbody = $('pos-body');
    if (!d.positions.length) {
      pbody.innerHTML = '<tr><td colspan="8" class="empty">No open positions</td></tr>';
    } else {
      pbody.innerHTML = d.positions.map(p => `
        <tr>
          <td style="color:var(--dim);font-size:11px;">${p.strategy}</td>
          <td style="font-weight:600;color:var(--text)">${p.symbol}</td>
          <td class="${p.direction === 'long' ? 'dir-long' : 'dir-short'}">${p.direction.toUpperCase()}</td>
          <td>${p.shares}</td>
          <td>${parseFloat(p.entry).toFixed(4)}</td>
          <td style="color:var(--red)">${parseFloat(p.stop).toFixed(4)}</td>
          <td style="color:var(--green)">${parseFloat(p.tp).toFixed(4)}</td>
          <td style="color:var(--accent)">$${parseFloat(p.R_dollars).toFixed(2)}</td>
        </tr>
      `).join('');
    }

    // Strategy risk bars
    const sl = $('strat-list');
    if (d.strategy_rows && d.strategy_rows.length) {
      sl.innerHTML = d.strategy_rows.map(s => {
        const pct  = s.dd_pct;
        const barClass = pct >= 80 ? 'dd-bar crit' : pct >= 50 ? 'dd-bar warn' : 'dd-bar';
        const nameClass = s.halted ? 'strat-name halted' : 'strat-name';
        const pnlColor = s.pnl >= 0 ? 'var(--green)' : 'var(--red)';
        return `
          <div class="strat-row">
            <div class="${nameClass}">${s.id}</div>
            <div>
              <div class="dd-bar-wrap">
                <div class="${barClass}" style="width:${pct}%"></div>
              </div>
              <div style="font-size:9px;color:var(--dim);margin-top:3px;">${pct}% of $${s.max_dd} DD limit</div>
            </div>
            <div class="strat-pnl" style="color:${pnlColor}">${fmt$(s.pnl)}</div>
            <div class="strat-risk">$${s.risk_per_trade}/R</div>
          </div>
        `;
      }).join('');
    }

  } catch(e) {
    console.error('Live refresh error:', e);
  }
}

// ── Trades ────────────────────────────────────────────────────────────────
async function loadTrades() {
  try {
    const r = await fetch('/api/trades');
    tradeData = await r.json();
    renderTrades();
  } catch(e) { console.error('Trades load error:', e); }
}

function renderTrades() {
  const rows = tradeFilter === 'today' ? tradeData.today : tradeData.all;
  $('trade-count').textContent = rows.length + ' trades';
  const tbody = $('trade-body');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="13" class="empty">No trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(t => {
    const rVal = parseFloat(t.result_R || 0);
    const rClass = rVal > 0 ? 'green' : rVal < 0 ? 'red' : '';
    const dolVal = parseFloat(t.result_dollars || 0);
    return `<tr>
      <td style="color:var(--dim)">${t.session || ''}</td>
      <td style="color:var(--dim);font-size:11px;">${t.strategy_id || ''}</td>
      <td style="font-weight:600">${t.symbol || ''}</td>
      <td class="${t.direction === 'long' ? 'dir-long' : 'dir-short'}">${(t.direction||'').toUpperCase()}</td>
      <td>${parseFloat(t.entry_price||0).toFixed(4)}</td>
      <td>${parseFloat(t.exit_price||0).toFixed(4)}</td>
      <td style="color:var(--red)">${parseFloat(t.stop||0).toFixed(4)}</td>
      <td style="color:var(--green)">${parseFloat(t.tp||0).toFixed(4)}</td>
      <td>${t.shares || ''}</td>
      <td class="pos-r ${rClass}">${fmtR(rVal)}</td>
      <td class="pos-r ${rClass}">${fmt$(dolVal)}</td>
      <td style="color:var(--dim)">${t.bars_to_exit || '—'}</td>
      <td>${exitBadge(t.exit_reason)}</td>
    </tr>`;
  }).join('');
}

// ── History ───────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const rows = await r.json();
    const tbody = $('hist-body');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">No history yet</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(h => {
      const acc  = parseInt(h.signals_accepted || 0);
      const won  = parseInt(h.trades_won  || 0);
      const lost = parseInt(h.trades_lost || 0);
      const wr   = (won + lost) > 0 ? Math.round(won / (won + lost) * 100) + '%' : '—';
      const rVal = parseFloat(h.total_R || 0);
      const dVal = parseFloat(h.total_dollars || 0);
      const rCls = rVal > 0 ? 'green' : rVal < 0 ? 'red' : '';
      return `<tr>
        <td style="color:var(--accent);font-weight:500">${h.session || ''}</td>
        <td>${acc}</td>
        <td style="color:var(--green)">${won}</td>
        <td style="color:var(--red)">${lost}</td>
        <td style="color:var(--dim)">${h.trades_eod || 0}</td>
        <td>${wr}</td>
        <td class="pos-r ${rCls}">${fmtR(rVal)}</td>
        <td class="pos-r ${rCls}">${fmt$(dVal)}</td>
        <td style="color:var(--dim)">${h.max_simultaneous || '—'}</td>
        <td>${h.daily_loss_halted === 'True' ? '<span class="badge badge-stop">YES</span>' : '<span style="color:var(--dim)">—</span>'}</td>
        <td style="color:var(--dim);font-size:11px;">${(h.strategies_active || '').replace(/,/g, ', ')}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('History load error:', e); }
}

// ── Countdown + auto-refresh ──────────────────────────────────────────────
let countdown = 5;
setInterval(() => {
  countdown--;
  $('tick-countdown').textContent = countdown;
  if (countdown <= 0) {
    countdown = 5;
    loadLive();
    const activeTab = document.querySelector('.tab.active')?.dataset?.pane;
    if (activeTab === 'trades')  loadTrades();
    if (activeTab === 'history') loadHistory();
  }
}, 1000);

// Initial load
loadLive();
</script>
</body>
</html>"""
