"""Browser-based live dashboard served from the terminal process.

Same safety model as onboarding: binds 127.0.0.1 only, requires a
random token per session, never touches the network. The HTML has zero
external assets (fully offline-safe) and polls a small JSON endpoint.
"""
from __future__ import annotations

import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable

from brokers.base import BaseBroker

TOKEN_LEN_S = 22


class DashboardState:
    """Thread-safe snapshot container shared with the asyncio side."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "connected": False,
            "armed_for": "0:00:00",
            "account": None,
            "positions": [],
            "events": [],
        }

    def push(self, **fields: Any) -> None:
        with self._lock:
            self._data.update(fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    @property
    def lock(self) -> threading.Lock:
        return self._lock


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_page(token: str, port: int) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Okkults · Live Dashboard</title>
<link rel="icon" href="data:," />
<style>
:root {{
  --bg: #0b0f14; --card: #11161d; --edge: #1d2530;
  --text: #e6edf3; --dim: #7d8896;
  --green: #3fb950; --red: #f85149; --blue: #58a6ff;
  --magenta: #d2a8ff; --yellow: #e3b341;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--bg); color: var(--text); padding: 24px; }}
.wrap {{ max-width: 1180px; margin: 0 auto; }}
header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 20px; flex-wrap: wrap;}}
h1 {{ font-size: 20px; letter-spacing: 2px; }}
.badge {{ padding: 4px 12px; border-radius: 999px; font-size: 12px; border: 1px solid; }}
.badge.on  {{ color: var(--green); border-color: var(--green); }}
.badge.off {{ color: var(--red); border-color: var(--red); }}
.armed {{ color: var(--dim); font-size: 12px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
.card {{
  background: var(--card); border: 1px solid var(--edge);
  border-radius: 12px; padding: 16px; text-align: left;
}}
.muted   {{ color: var(--dim); }}
.big     {{ font-size: 30px; margin-top: 8px; }}
.label   {{ color: var(--dim); font-size: 11px; letter-spacing: 1.5px; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }}
th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--edge); text-align: left; }}
th {{ color: var(--dim); font-weight: 500; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; }}
tr:last-child td {{ border-bottom: none; }}
.section-title {{ color: var(--dim); font-size: 11px; letter-spacing: 1.5px;
                  text-transform: uppercase; margin: 22px 0 8px; }}
.event {{ padding: 7px 10px; border-left: 3px solid var(--edge); margin: 6px 0;
          background: var(--card); border-radius: 0 8px 8px 0; font-size: 13px; }}
.event .t {{ color: var(--dim); margin-right: 10px; }}
.event.signal {{ border-left-color: var(--magenta); }}
.event.order  {{ border-left-color: var(--green); }}
.event.close  {{ border-left-color: var(--yellow); }}
.kind {{ display: inline-block; min-width: 74px; color: var(--blue); font-weight: 600; }}
.pos  {{ color: var(--green); }} .neg {{ color: var(--red); }}
footer {{ color: var(--dim); font-size: 11px; margin-top: 22px; }}
</style>
<script>
async function tick() {{
  try {{
    const r = await fetch('/api/state?token={token}', {{ cache: 'no-store' }});
    render(await r.json());
  }} catch (e) {{ /* server restarting */ }}
}}
function money(v) {{ return (v === null || v === undefined) ? '—' : '$' + Number(v).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}); }}
function signed(v) {{
  if (v === null || v === undefined) return '—';
  const s = v >= 0 ? '+$' : '-$';
  return s + Math.abs(v).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
}}
function render(s) {{
  document.getElementById('ws').textContent = s.connected ? 'CONNECTED' : 'RECONNECTING…';
  document.getElementById('ws').className = 'badge ' + (s.connected ? 'on' : 'off');
  document.getElementById('armed').textContent = 'armed for ' + s.armed_for;
  const a = s.account || {{}};
  equity.textContent = money(a.equity);
  cash.textContent   = a.cash === null || a.cash === undefined ? '—' : money(a.cash);
  acctStatus.textContent = (a.status || '?') + ' · ' + (a.paper ? 'paper' : 'LIVE');
  const tbody = document.getElementById('posreq'); tbody.innerHTML = '';
  (s.positions || []).forEach(p => {{
    const pl = p.unrealized_pl;
    const cls = (pl === null || pl === undefined) ? '' : (pl >= 0 ? 'pos' : 'neg');
    tbody.insertAdjacentHTML('beforeend',
      `<tr><td>${{p.symbol}}</td><td>${{p.qty}}</td><td>${{p.side}}</td>` +
      `<td>${{money(p.avg_entry_price)}}</td><td>${{money(p.current_price)}}</td>` +
      `<td class="${{cls}}">${{signed(pl)}}</td></tr>`);
  }});
  if (!(s.positions || []).length)
    tbody.innerHTML = '<tr><td colspan="6" class="muted">no open positions</td></tr>';
  const feed = document.getElementById('feed'); feed.innerHTML = '';
  (s.events || []).slice().reverse().forEach(e => {{
    feed.insertAdjacentHTML('beforeend',
      `<div class="event ${{e.kind.toLowerCase()}}"><span class="t">${{e.time}}</span>` +
      `<span class="kind">${{e.kind}}</span>${{_esc(e.detail)}}</div>`);
  }});
}}
function _esc(t) {{
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
tick(); setInterval(tick, 2000);
</script></head>
<body><div class="wrap">
  <header>
    <h1>OKKULTS <span style="color:var(--dim)">· LIVE DASHBOARD</span></h1>
    <span id="ws" class="badge off">…</span>
    <span id="armed" class="armed"></span>
  </header>

  <div class="grid">
    <div class="card"><div class="label">Equity</div><div class="big" id="equity">—</div></div>
    <div class="card"><div class="label">Cash</div><div class="big" id="cash">—</div></div>
    <div class="card"><div class="label">Account</div><div class="big" id="acctStatus">—</div></div>
  </div>

  <div class="section-title">Open positions</div>
  <div class="card"><table><thead><tr>
    <th>symbol</th><th>qty</th><th>side</th><th>entry</th><th>last</th><th>unrealized P/L</th>
  </tr></thead><tbody id="posreq"></tbody></table></div>

  <div class="section-title">Signal &amp; order feed</div>
  <div id="feed" class="muted">loading…</div>

  <footer>bound to 127.0.0.1 · token-gated · this terminal process is your only server</footer>
</div></body></html>
"""


def start_web_dashboard(
    state: DashboardState,
    state_provider: Callable[[], dict[str, Any]] | None = None,
) -> tuple[str, threading.Thread]:
    token = secrets.token_urlsafe(TOKEN_LEN_S)

    class Handler(BaseHTTPRequestHandler):
        def _forbidden(self) -> None:
            body = b'{"ok": false, "error": "bad token"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 stdlib signature
            parsed = urlparse(self.path)
            got = (parse_qs(parsed.query).get("token") or [""])[0]
            if not secrets.compare_digest(got, token):
                self._forbidden()
                return
            if parsed.path == "/api/state":
                import json as _json

                raw = _json.dumps(
                    {"ok": True, "state": state.snapshot()}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif parsed.path == "/":
                raw = render_page(token, self.server.server_port).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._forbidden()

        def log_message(self, *args: Any) -> None:  # silence
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=serve_loop, args=(server,), daemon=True, name="dashboard-http")
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    return url, thread


def serve_loop(server: ThreadingHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=0.2)
    except Exception:
        pass

