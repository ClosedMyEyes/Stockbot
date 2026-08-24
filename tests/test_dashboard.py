"""
P0-3: the dashboard package exists (renamed from dashboard__init__.py),
serves its API + HTML, and is compatible with the current orchestrator.
"""

import json
import urllib.request

from conftest import pkg


def test_dashboard_serves_status_and_html(orchestrator):
    dash = pkg("dashboard")
    server = dash.start_dashboard(orchestrator, port=0)   # ephemeral port
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status",
                                    timeout=5) as r:
            data = json.loads(r.read())
        assert data["halted"] is False
        assert data["positions"] == []
        assert len(data["strategy_rows"]) == 6            # one per strategy
        assert data["portfolio_dd_limit"] == 1500.0

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trades",
                                    timeout=5) as r:
            trades = json.loads(r.read())
        assert trades == {"today": [], "all": []}

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert b"ORCH // MONITOR" in r.read()
    finally:
        server.shutdown()
