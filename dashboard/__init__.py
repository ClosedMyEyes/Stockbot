"""
dashboard/__init__.py — Live monitoring dashboard.

Serves a simple HTML page showing:
  - Open positions with current P&L (mark-to-market)
  - Daily P&L in R and dollars
  - Signals fired this session
  - Halt status

Run alongside the orchestrator:
  from orchestrator.dashboard import start_dashboard
  start_dashboard(orchestrator)   # non-blocking, runs in background thread

Access at http://localhost:8050
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..main import Orchestrator

log = logging.getLogger("dashboard")

_ORCHESTRATOR = None


def start_dashboard(orch, port: int = 8050):
    """Start dashboard server in a background thread."""
    global _ORCHESTRATOR
    _ORCHESTRATOR = orch

    def serve():
        server = HTTPServer(("0.0.0.0", port), DashboardHandler)
        log.info(f"Dashboard running at http://localhost:{port}")
        server.serve_forever()

    t = threading.Thread(target=serve, daemon=True, name="dashboard")
    t.start()
    return t


class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass   # suppress default access log

    def do_GET(self):
        if self.path == "/api/status":
            self._serve_json()
        else:
            self._serve_html()

    def _serve_json(self):
        data = self._build_data()
        body = json.dumps(data, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        html = _build_html()
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _build_data(self) -> dict:
        if _ORCHESTRATOR is None:
            return {"error": "orchestrator not connected"}
        risk = _ORCHESTRATOR.risk
        return {
            "session":          _ORCHESTRATOR._session_date,
            "halted":           risk.halted,
            "daily_r":          round(risk.daily_r_total, 3),
            "daily_dollars":    round(risk.daily_pnl_dollars, 2),
            "signals_fired":    _ORCHESTRATOR._signal_count,
            "signals_accepted": _ORCHESTRATOR._signals_accepted,
            "wins":             _ORCHESTRATOR._wins,
            "losses":           _ORCHESTRATOR._losses,
            "positions":        risk.summary()["positions"],
        }


def _build_html() -> str:
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Orchestrator Dashboard</title>
  <meta http-equiv="refresh" content="5">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Courier New', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
    h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 25px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; }
    .card .label { font-size: 0.75em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
    .card .value { font-size: 1.6em; font-weight: bold; margin-top: 5px; }
    .green  { color: #3fb950; }
    .red    { color: #f85149; }
    .yellow { color: #d29922; }
    .blue   { color: #58a6ff; }
    table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
    th { background: #21262d; padding: 10px 15px; text-align: left; font-size: 0.8em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
    td { padding: 10px 15px; border-bottom: 1px solid #21262d; font-size: 0.9em; }
    tr:last-child td { border-bottom: none; }
    .halt-banner { background: #da3633; color: white; padding: 12px 20px; border-radius: 6px; margin-bottom: 20px; font-weight: bold; font-size: 1.1em; display: none; }
    #halt.active { display: block; }
  </style>
</head>
<body>
  <h1>⚡ Orchestrator Dashboard</h1>
  <div class="halt-banner" id="halt">🛑 DAILY LOSS LIMIT HIT — NEW SIGNALS HALTED</div>

  <div class="grid">
    <div class="card">
      <div class="label">Session</div>
      <div class="value blue" id="session">—</div>
    </div>
    <div class="card">
      <div class="label">Daily P&L</div>
      <div class="value" id="daily-dollars">$0.00</div>
    </div>
    <div class="card">
      <div class="label">Daily R</div>
      <div class="value" id="daily-r">0.00R</div>
    </div>
    <div class="card">
      <div class="label">Signals / Accepted</div>
      <div class="value blue" id="signals">0 / 0</div>
    </div>
    <div class="card">
      <div class="label">Open Positions</div>
      <div class="value blue" id="open-count">0</div>
    </div>
    <div class="card">
      <div class="label">Wins</div>
      <div class="value green" id="wins">0</div>
    </div>
    <div class="card">
      <div class="label">Losses</div>
      <div class="value red" id="losses">0</div>
    </div>
    <div class="card">
      <div class="label">W/L Ratio</div>
      <div class="value" id="wl">—</div>
    </div>
  </div>

  <table id="positions-table">
    <thead>
      <tr>
        <th>Strategy</th>
        <th>Symbol</th>
        <th>Dir</th>
        <th>Shares</th>
        <th>Entry</th>
        <th>Stop</th>
        <th>TP</th>
        <th>R ($)</th>
      </tr>
    </thead>
    <tbody id="positions-body">
      <tr><td colspan="8" style="text-align:center; color:#8b949e;">No open positions</td></tr>
    </tbody>
  </table>

  <script>
    async function refresh() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();

        document.getElementById('session').textContent = d.session || '—';
        const dollars = d.daily_dollars;
        const dollarsEl = document.getElementById('daily-dollars');
        dollarsEl.textContent = (dollars >= 0 ? '+$' : '-$') + Math.abs(dollars).toFixed(2);
        dollarsEl.className = 'value ' + (dollars >= 0 ? 'green' : 'red');

        const rVal = d.daily_r;
        const rEl = document.getElementById('daily-r');
        rEl.textContent = (rVal >= 0 ? '+' : '') + rVal.toFixed(3) + 'R';
        rEl.className = 'value ' + (rVal >= 0 ? 'green' : 'red');

        document.getElementById('signals').textContent = d.signals_fired + ' / ' + d.signals_accepted;
        document.getElementById('open-count').textContent = d.positions.length;
        document.getElementById('wins').textContent = d.wins;
        document.getElementById('losses').textContent = d.losses;

        const wl = d.losses > 0 ? (d.wins / d.losses).toFixed(2) : (d.wins > 0 ? '∞' : '—');
        document.getElementById('wl').textContent = wl;

        document.getElementById('halt').className = d.halted ? 'halt-banner active' : 'halt-banner';

        const tbody = document.getElementById('positions-body');
        if (d.positions.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#8b949e;">No open positions</td></tr>';
        } else {
          tbody.innerHTML = d.positions.map(p => `
            <tr>
              <td>${p.strategy}</td>
              <td>${p.symbol}</td>
              <td class="${p.direction === 'long' ? 'green' : 'red'}">${p.direction.toUpperCase()}</td>
              <td>${p.shares}</td>
              <td>${p.entry.toFixed(4)}</td>
              <td class="red">${p.stop.toFixed(4)}</td>
              <td class="green">${p.tp.toFixed(4)}</td>
              <td>$${p.R_dollars.toFixed(2)}</td>
            </tr>
          `).join('');
        }
      } catch(e) {
        console.error('Dashboard refresh error:', e);
      }
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
