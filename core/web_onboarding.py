"""Localhost browser onboarding — the same wizard, in a web page.

Only ever binds ``127.0.0.1`` on an ephemeral port and requires a random
one-time token, so nothing on the network (or in another browser tab) can
read the form or submit credentials. Secrets are masked before they are
sent to the page, written once with the same atomic 0600 ``.env`` helper
the terminal wizard uses, and never logged.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import webbrowser
from collections.abc import Awaitable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import parse_qs, urlparse

from rich.console import Console

from config.settings import HARD_MAX_PORTFOLIO_PCT, HARD_MAX_TRADE_PCT
from core.envwriter import mask_secret, read_env_file
from core.onboarding import (
    DEFAULT_WS_URL,
    humanize_license_rejection,
    LIVE_ACK_WORDS,
    BrokerProbe,
    LicenseProbeFactory,
    WizardAnswers,
    alpaca_key_error,
    license_error,
    persist_answers,
    probe_broker_credentials,
    risk_pct_error,
    verify_license_probe,
)

console = Console()

MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 30 * 60


_T = TypeVar("_T")


def _await(awaitable: Awaitable[_T]) -> _T:
    """Run a probe awaitable from a synchronous request-handler thread."""

    async def runner() -> _T:
        return await awaitable

    return asyncio.run(runner())


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _current_float(current: dict[str, str], key: str, default: float) -> float:
    raw = current.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class WebOnboardingServer:
    """Ephemeral localhost HTTP server that collects one set of answers."""

    def __init__(
        self,
        env_path: str | Path = ".env",
        ws_url: str = DEFAULT_WS_URL,
        broker_probe: BrokerProbe | None = None,
        license_probe_factory: LicenseProbeFactory | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.env_path = Path(env_path)
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.answers: WizardAnswers | None = None
        self.current = read_env_file(self.env_path)
        self._ws_url = ws_url
        self._broker_probe = broker_probe or probe_broker_credentials
        self._license_probe_factory = license_probe_factory or (
            lambda key: verify_license_probe(ws_url, key)
        )
        self._done = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @property
    def url(self) -> str:
        port = self.port
        if self._httpd is not None and port == 0:
            port = int(self._httpd.server_address[1])
        return f"http://{self.host}:{port}/?token={self.token}"

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="okkults-onboarding",
            daemon=True,
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def wait(self, timeout: float = DEFAULT_TIMEOUT_S) -> WizardAnswers:
        if not self._done.wait(timeout):
            raise TimeoutError("onboarding form was never submitted")
        if self.answers is None:
            raise TimeoutError("onboarding finished without answers")
        return self.answers

    def run(self, open_browser: bool = True, timeout: float = DEFAULT_TIMEOUT_S) -> WizardAnswers | None:
        url = self.start()
        console.print(
            "[cyan]Onboarding is ready in your browser:[/cyan] "
            f"[bold]{url}[/bold]"
        )
        console.print(
            "[dim]This page is bound to 127.0.0.1 with a one-time token. "
            "Use `python main.py --onboard --cli` for the terminal wizard.[/dim]"
        )
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # browser may not exist in server environments
                pass
        try:
            return self.wait(timeout)
        except TimeoutError:
            console.print(
                "[yellow][!] Browser onboarding timed out — nothing was saved. "
                "Run `python main.py` again when ready.[/yellow]"
            )
            return None
        finally:
            self.stop()

    # ------------------------------------------------------------------ #
    # request handlers
    # ------------------------------------------------------------------ #
    def page_html(self) -> str:
        has_key = bool(self.current.get("ALPACA_API_KEY"))
        has_license = bool(self.current.get("OKKULTS_LICENSE_KEY"))
        trade_pct = _current_float(
            self.current, "MAX_TRADE_ALLOCATION_PCT", 0.05
        ) * 100
        portfolio_pct = _current_float(
            self.current, "MAX_PORTFOLIO_RISK_PCT", HARD_MAX_PORTFOLIO_PCT
        ) * 100
        bootstrap = {
            "token": self.token,
            "ws_url": self._ws_url,
            "defaults": {
                "trading_mode": (
                    "paper"
                    if self.current.get("ALPACA_IS_PAPER", "true").lower()
                    not in ("false", "0", "no")
                    else "live"
                ),
                "max_trade_pct": round(min(max(trade_pct, 1.0), 10.0), 2),
                "max_portfolio_pct": round(
                    min(max(portfolio_pct, 1.0), 30.0), 2
                ),
                "allow_short_selling": self.current.get(
                    "ALLOW_SHORT_SELLING", "false"
                ).lower()
                in ("true", "1", "yes"),
                "execution_mode": (
                    self.current.get("EXECUTION_MODE", "full_auto")
                    if self.current.get("EXECUTION_MODE", "full_auto")
                    in ("full_auto", "semi_auto")
                    else "full_auto"
                ),
                "local_exit_failsafe": self.current.get(
                    "LOCAL_EXIT_FAILSAFE", "true"
                ).lower()
                not in ("false", "0", "no"),
            },
            "existing": {
                "alpaca_key_masked": mask_secret(
                    self.current.get("ALPACA_API_KEY", "")
                ),
                "alpaca_secret_masked": mask_secret(
                    self.current.get("ALPACA_SECRET_KEY", "")
                ),
                "license_masked": mask_secret(
                    self.current.get("OKKULTS_LICENSE_KEY", "")
                ),
                "has_alpaca": has_key,
                "has_license": has_license,
            },
        }
        return HTML_PAGE.replace(
            "__BOOTSTRAP__", json.dumps(bootstrap)
        )

    def verify_broker(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        mode = str(payload.get("trading_mode") or "paper")
        paper = mode != "live"
        key = str(payload.get("alpaca_key") or "").strip() or self.current.get(
            "ALPACA_API_KEY", ""
        )
        secret = str(
            payload.get("alpaca_secret") or ""
        ).strip() or self.current.get("ALPACA_SECRET_KEY", "")
        problem = alpaca_key_error(key, secret)
        if problem:
            return 400, {"ok": False, "error": problem}
        verdict = _await(self._broker_probe(key, secret, paper))
        if not verdict.startswith("OK"):
            return 400, {"ok": False, "error": verdict}
        return 200, {"ok": True, "verdict": verdict}

    def resolve_ws_url(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        """Parse the signal server URL from the submitted payload."""
        ws_url = str(payload.get("ws_url") or "").strip() or self._ws_url
        if not ws_url.startswith(("ws://", "wss://")):
            return "", "Signal server URL must start with ws:// or wss://."
        return ws_url, None

    def _probe_for(self, ws_url: str, key: str) -> Any:
        """Real probe for custom URLs; injected factory keeps default-path tests fake."""
        if ws_url == self._ws_url:
            return self._license_probe_factory(key)()
        return verify_license_probe(ws_url, key)()

    def verify_license(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        key = str(payload.get("license_key") or "").strip() or self.current.get(
            "OKKULTS_LICENSE_KEY", ""
        )
        problem = license_error(key)
        if problem:
            return 400, {"ok": False, "error": problem}
        ws_url, url_problem = self.resolve_ws_url(payload)
        if url_problem:
            return 400, {"ok": False, "error": url_problem}
        verdict = _await(self._probe_for(ws_url, key))
        verdict = humanize_license_rejection(verdict, ws_url, key)
        if verdict is not None and "rejected" in verdict:
            return 400, {"ok": False, "error": verdict}
        return 200, {"ok": True, "verdict": verdict or "license verified"}

    def submit(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        mode = str(payload.get("trading_mode") or "")
        if mode not in ("paper", "live"):
            return 400, {"ok": False, "error": "Choose paper or live first."}
        if mode == "live" and str(payload.get("live_ack") or "").strip() != LIVE_ACK_WORDS:
            return 400, {
                "ok": False,
                "error": f'Type "{LIVE_ACK_WORDS}" exactly to enable live trading.',
            }

        key = str(payload.get("alpaca_key") or "").strip() or self.current.get(
            "ALPACA_API_KEY", ""
        )
        secret = str(
            payload.get("alpaca_secret") or ""
        ).strip() or self.current.get("ALPACA_SECRET_KEY", "")
        problem = alpaca_key_error(key, secret)
        if problem:
            return 400, {"ok": False, "error": problem}
        verdict = _await(self._broker_probe(key, secret, mode == "paper"))
        if not verdict.startswith("OK"):
            return 400, {"ok": False, "error": verdict}

        trade_raw = _num(payload.get("max_trade_pct"))
        portfolio_raw = _num(payload.get("max_portfolio_pct"))
        if trade_raw is None:
            return 400, {"ok": False, "error": "Max risk per trade is required."}
        if portfolio_raw is None:
            return 400, {
                "ok": False,
                "error": "Max total risk at one time is required.",
            }
        if trade_raw < 1:
            return 400, {
                "ok": False,
                "error": "Enter a percentage such as 5, not a fraction.",
            }
        if portfolio_raw < 1:
            return 400, {
                "ok": False,
                "error": "Enter a percentage such as 30, not a fraction.",
            }
        trade_pct = trade_raw / 100.0
        portfolio_pct = portfolio_raw / 100.0
        problem = risk_pct_error(
            "Max risk per trade", trade_pct, maximum=HARD_MAX_TRADE_PCT
        )
        if problem:
            return 400, {"ok": False, "error": problem}
        problem = risk_pct_error(
            "Max total risk at one time",
            portfolio_pct,
            maximum=HARD_MAX_PORTFOLIO_PCT,
        )
        if problem:
            return 400, {"ok": False, "error": problem}
        if portfolio_pct < trade_pct:
            return 400, {
                "ok": False,
                "error": "Max total risk must be at least the per-trade risk.",
            }

        license_key = str(
            payload.get("license_key") or ""
        ).strip() or self.current.get("OKKULTS_LICENSE_KEY", "")
        problem = license_error(license_key)
        if problem:
            return 400, {"ok": False, "error": problem}
        ws_url, url_problem = self.resolve_ws_url(payload)
        if url_problem:
            return 400, {"ok": False, "error": url_problem}
        license_verdict = _await(self._probe_for(ws_url, license_key))
        license_verdict = humanize_license_rejection(
            license_verdict, ws_url, license_key
        )
        if license_verdict is not None and "rejected" in license_verdict:
            return 400, {"ok": False, "error": license_verdict}

        execution_mode = str(payload.get("execution_mode") or "full_auto")
        if execution_mode not in ("full_auto", "semi_auto"):
            return 400, {"ok": False, "error": "Unknown execution mode."}

        answers = WizardAnswers(
            license_key=license_key,
            alpaca_api_key=key,
            alpaca_secret_key=secret,
            alpaca_is_paper=mode == "paper",
            max_trade_allocation_pct=trade_pct,
            max_portfolio_risk_pct=portfolio_pct,
            allow_short_selling=bool(payload.get("allow_short_selling")),
            execution_mode=execution_mode,
            local_exit_failsafe=bool(payload.get("local_exit_failsafe", True)),
        )
        answers.probes["broker"] = verdict
        answers.probes["license"] = (
            "verified" if license_verdict is None else "saved (probe failed)"
        )
        persist_answers(answers, self.current, self.env_path, ws_url)
        self.answers = answers
        return 200, {
            "ok": True,
            "message": "Configuration saved. SYSTEM ARMED.",
            "mode": mode,
        }

    def mark_done(self) -> None:
        self._done.set()


def _make_handler(app: WebOnboardingServer) -> type[BaseHTTPRequestHandler]:
    class OnboardingHandler(BaseHTTPRequestHandler):
        server_version = "OkkultsOnboarding/1.0"

        def log_message(self, *args: object) -> None:
            return None

        # ---------------- helpers ---------------- #
        def _token_ok(self, data: dict[str, Any] | None = None) -> bool:
            supplied = self.headers.get("X-Okkults-Token", "")
            if not supplied and data is not None:
                supplied = str(data.get("token") or "")
            return secrets.compare_digest(supplied, app.token)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request body")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return cast("dict[str, Any]", data)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        # ---------------- routes ---------------- #
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(200, {"ok": True})
                return
            if parsed.path != "/":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            token = parse_qs(parsed.query).get("token", [""])[0]
            if not secrets.compare_digest(token, app.token):
                self._send(
                    401,
                    b"Invalid or expired onboarding link. Re-run python main.py.",
                    "text/plain; charset=utf-8",
                )
                return
            self._send(
                200,
                app.page_html().encode("utf-8"),
                "text/html; charset=utf-8",
            )

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                data = self._read_json()
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "Invalid JSON body."})
                return
            if not self._token_ok(data):
                self._send_json(401, {"ok": False, "error": "Invalid token."})
                return

            handlers: dict[str, Any] = {
                "/api/verify-broker": app.verify_broker,
                "/api/verify-license": app.verify_license,
                "/api/submit": app.submit,
            }
            handler = handlers.get(path)
            if handler is None:
                self._send_json(404, {"ok": False, "error": "Unknown endpoint."})
                return
            try:
                status, payload = handler(data)
            except Exception as exc:  # never echo tracebacks/secrets
                status, payload = 500, {
                    "ok": False,
                    "error": f"{type(exc).__name__}: setup failed",
                }
            self._send_json(status, payload)
            if status == 200 and path == "/api/submit":
                app.mark_done()

    return OnboardingHandler


def run_web_onboarding(
    env_path: str | Path = ".env",
    ws_url: str = DEFAULT_WS_URL,
    open_browser: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> WizardAnswers | None:
    """Serve the browser wizard until it submits; None on timeout."""
    server = WebOnboardingServer(env_path=env_path, ws_url=ws_url)
    return server.run(open_browser=open_browser, timeout=timeout)


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Okkults Quant Terminal — Setup</title>
<style>
  :root {
    --bg: #080b12; --card: #0f1520; --line: #1e293b;
    --text: #e6edf7; --dim: #8b9bb4; --accent: #34d399; --danger: #f87171;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: radial-gradient(1200px 600px at 50% -20%, #12203a 0%, var(--bg) 60%);
    color: var(--text); font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: flex-start; justify-content: center; padding: 48px 16px;
  }
  .shell { width: 100%; max-width: 560px; }
  .brand { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px; }
  .brand b { letter-spacing: .14em; font-size: 14px; }
  .brand span { color: var(--dim); font-size: 12px; }
  .dots { display: flex; gap: 6px; margin: 18px 0; }
  .dots i { height: 3px; flex: 1; background: var(--line); border-radius: 3px; transition: .25s; }
  .dots i.on { background: var(--accent); }
  .card {
    display: none; background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 26px; box-shadow: 0 20px 60px rgba(0,0,0,.45);
  }
  .card.on { display: block; }
  h1 { font-size: 19px; margin: 0 0 4px; }
  p.sub { color: var(--dim); margin: 0 0 20px; font-size: 13px; }
  label { display: block; font-size: 12px; color: var(--dim); margin: 14px 0 6px; }
  input[type=text], input[type=password], input[type=number], select {
    width: 100%; padding: 11px 12px; border-radius: 9px; border: 1px solid var(--line);
    background: #0a0f18; color: var(--text); font-size: 14px; outline: none;
  }
  input:focus, select:focus { border-color: var(--accent); }
  .hint { color: var(--dim); font-size: 12px; margin-top: 6px; }
  .chip { display: inline-block; font-size: 11px; color: var(--dim); border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; margin-left: 6px; }
  .row { display: flex; gap: 12px; }
  .row > * { flex: 1; }
  .choice { display: flex; gap: 10px; }
  .choice button {
    flex: 1; padding: 16px; border-radius: 11px; border: 1px solid var(--line);
    background: #0a0f18; color: var(--text); cursor: pointer; font-size: 14px; text-align: left;
  }
  .choice button b { display: block; margin-bottom: 3px; }
  .choice button small { color: var(--dim); }
  .choice button.sel { border-color: var(--accent); background: #0c1a16; }
  .check { display: flex; align-items: center; gap: 9px; margin-top: 14px; color: var(--text); font-size: 13px; }
  .check input { width: 16px; height: 16px; accent-color: var(--accent); }
  footer { display: flex; gap: 10px; margin-top: 24px; }
  button.primary, button.ghost {
    padding: 11px 18px; border-radius: 9px; cursor: pointer; font-size: 14px;
    border: 1px solid var(--line); background: transparent; color: var(--text);
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #04120c; font-weight: 600; }
  button.ghost:hover { border-color: var(--dim); }
  button:disabled { opacity: .5; cursor: default; }
  .err { color: var(--danger); font-size: 13px; margin-top: 12px; min-height: 18px; }
  .ok { color: var(--accent); font-size: 13px; margin-top: 12px; min-height: 18px; }
  .summary { border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin-top: 8px; }
  .summary div { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
  .summary span:first-child { color: var(--dim); }
  .done { text-align: center; padding: 10px 0 4px; }
  .done .mark { font-size: 42px; color: var(--accent); }
  .note { background: #0a0f18; border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; color: var(--dim); font-size: 12px; margin-top: 14px; }
</style>
</head>
<body>
<main class="shell">
  <div class="brand"><b>OKKULTS</b><span>quant terminal setup</span></div>
  <div class="dots" id="dots"><i></i><i></i><i></i><i></i><i></i></div>

  <section class="card on" id="s1">
    <h1>Choose an account</h1>
    <p class="sub">Non-custodial. Keys stay on this machine and go only to Alpaca.</p>
    <div class="choice" id="mode">
      <button data-mode="paper"><b>Paper trading</b><small>Safe default. Fake money.</small></button>
      <button data-mode="live"><b>Live trading</b><small>Real money. Typed confirmation required.</small></button>
    </div>
    <label id="ackLabel" style="display:none">Type "I UNDERSTAND" to enable live trading</label>
    <input id="ack" type="text" style="display:none" autocomplete="off">
    <div class="err" id="e1"></div>
    <footer><button class="primary" id="n1">Continue</button></footer>
  </section>

  <section class="card" id="s2">
    <h1>Alpaca API keys</h1>
    <p class="sub">Verified against Alpaca before anything is saved.</p>
    <label>API key id <span class="chip" id="keyChip"></span></label>
    <input id="key" type="text" placeholder="PK..." autocomplete="off">
    <label>Secret key <span class="chip" id="secretChip"></span></label>
    <input id="secret" type="password" placeholder="SK..." autocomplete="off">
    <div class="hint">Leave blank to keep the existing saved credentials.</div>
    <div class="err" id="e2"></div>
    <footer>
      <button class="ghost" id="b2">Back</button>
      <button class="primary" id="n2">Verify &amp; continue</button>
    </footer>
  </section>

  <section class="card" id="s3">
    <h1>Risk limits</h1>
    <p class="sub">Sizing is the stop-loss: allocation caps cap the overnight gap damage.</p>
    <div class="row">
      <div>
        <label>Max risk per trade (%)</label>
        <input id="trade" type="number" min="1" max="10" step="0.5">
      </div>
      <div>
        <label>Max total risk at one time (%)</label>
        <input id="portfolio" type="number" min="1" max="30" step="1">
      </div>
    </div>
    <div class="hint">Default 5% / 30%. Hard caps 10% / 30% — the terminal refuses anything larger.</div>
    <div class="hint" id="gapHint"></div>
    <label class="check"><input id="shorts" type="checkbox"> Allow SELL signals to open short positions</label>
    <div class="err" id="e3"></div>
    <footer>
      <button class="ghost" id="b3">Back</button>
      <button class="primary" id="n3">Continue</button>
    </footer>
  </section>

  <section class="card" id="s4">
    <h1>Connect the signal server</h1>
    <p class="sub">Your Okkults license authenticates the signal stream.</p>
    <label>Signal server URL</label>
    <input id="wsurl" type="text" placeholder="wss://okkults-api.okkults.workers.dev/v1/stream" autocomplete="off">
    <div class="hint">Paste exactly the endpoint your operator gave you (test rigs look like ws://192.168.x.x:8000/v1/stream).</div>
    <label>Okkults API key <span class="chip" id="licenseChip"></span></label>
    <input id="license" type="password" placeholder="ok_paper_... / ok_live_..." autocomplete="off">
    <label>Execution mode</label>
    <select id="execMode">
      <option value="full_auto">full_auto — fire the moment a signal lands</option>
      <option value="semi_auto">semi_auto — press Enter to confirm (needs a terminal)</option>
    </select>
    <label class="check"><input id="failsafe" type="checkbox"> Local failsafe: liquidate client-opened positions at 9:30:16 AM if the server goes dark</label>
    <div class="err" id="e4"></div>
    <footer>
      <button class="ghost" id="b4">Back</button>
      <button class="primary" id="n4">Verify &amp; continue</button>
    </footer>
  </section>

  <section class="card" id="s5">
    <h1>Review</h1>
    <p class="sub">Nothing is saved until you arm the terminal.</p>
    <div class="summary" id="summary"></div>
    <div class="ok" id="ok5"></div>
    <div class="err" id="e5"></div>
    <footer>
      <button class="ghost" id="b5">Back</button>
      <button class="primary" id="arm">Arm terminal</button>
    </footer>
  </section>

  <section class="card" id="s6">
    <div class="done">
      <div class="mark">✓</div>
      <h1>SYSTEM ARMED</h1>
      <p class="sub" id="doneText">Configuration saved. Close this tab — the terminal is starting.</p>
    </div>
    <div class="note">This process keeps running in your terminal window. Keep the machine on until the open-cross exit (or the next EXIT_SIGNAL clears you).</div>
  </section>
</main>
<script>
const BOOT = __BOOTSTRAP__;
const D = BOOT.defaults, X = BOOT.existing;
const $ = (id) => document.getElementById(id);
const state = {
  trading_mode: D.trading_mode,
  live_ack: "",
  alpaca_key: "", alpaca_secret: "",
  max_trade_pct: D.max_trade_pct,
  max_portfolio_pct: D.max_portfolio_pct,
  allow_short_selling: D.allow_short_selling,
  ws_url: BOOT.ws_url || "",
  license_key: "",
  execution_mode: D.execution_mode,
  local_exit_failsafe: D.local_exit_failsafe,
};
let step = 1;

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Okkults-Token": BOOT.token },
    body: JSON.stringify(Object.assign({ token: BOOT.token }, body)),
  });
  return { status: res.status, data: await res.json().catch(() => ({})) };
}

function show(n) {
  step = n;
  document.querySelectorAll(".card").forEach((c) => c.classList.remove("on"));
  $("s" + n).classList.add("on");
  document.querySelectorAll("#dots i").forEach((d, i) => d.classList.toggle("on", i < n));
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function fail(id, msg) { $(id).textContent = msg || ""; }
function busy(btn, on, label) { btn.disabled = on; btn.textContent = on ? "Working…" : label; }

// step 1
function syncAck() {
  const live = state.trading_mode === "live";
  $("ackLabel").style.display = live ? "block" : "none";
  $("ack").style.display = live ? "block" : "none";
}
document.querySelectorAll("#mode button").forEach((btn) => {
  btn.classList.toggle("sel", btn.dataset.mode === state.trading_mode);
  btn.onclick = () => {
    state.trading_mode = btn.dataset.mode;
    document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("sel", b === btn));
    syncAck();
  };
});
syncAck();
$("n1").onclick = () => {
  fail("e1", "");
  if (state.trading_mode === "live" && $("ack").value.trim() !== "I UNDERSTAND") {
    return fail("e1", 'Type "I UNDERSTAND" exactly to enable live trading.');
  }
  state.live_ack = $("ack").value.trim();
  show(2);
};
$("b2").onclick = () => show(1);

// step 2
$("keyChip").textContent = X.has_alpaca ? "saved " + X.alpaca_key_masked : "none saved";
$("secretChip").textContent = X.has_alpaca ? "saved " + X.alpaca_secret_masked : "none saved";
$("n2").onclick = async () => {
  fail("e2", "");
  const key = $("key").value.trim(), secret = $("secret").value.trim();
  if ((key && !secret) || (!key && secret)) return fail("e2", "Enter both the key id and the secret, or leave both blank to keep current.");
  state.alpaca_key = key; state.alpaca_secret = secret;
  const btn = $("n2"); busy(btn, true, "Verify & continue");
  const r = await api("/api/verify-broker", { trading_mode: state.trading_mode, alpaca_key: key, alpaca_secret: secret });
  busy(btn, false, "Verify & continue");
  if (!r.data.ok) return fail("e2", r.data.error || "Verification failed.");
  fail("e2", ""); $("e2").className = "ok"; $("e2").textContent = r.data.verdict;
  setTimeout(() => { $("e2").textContent = ""; $("e2").className = "err"; show(3); }, 650);
};
$("b3").onclick = () => show(2);

// step 3
$("trade").value = state.max_trade_pct;
$("portfolio").value = state.max_portfolio_pct;
$("shorts").checked = state.allow_short_selling;
function gap() {
  const t = parseFloat($("trade").value) || 0;
  $("gapHint").textContent = t > 0 ? `A -20% overnight gap on one report costs about ${(t * 0.2).toFixed(1)}% of equity.` : "";
}
$("trade").oninput = gap; gap();
$("n3").onclick = () => {
  fail("e3", "");
  const t = parseFloat($("trade").value), p = parseFloat($("portfolio").value);
  if (!(t > 0) || isNaN(p)) return fail("e3", "Enter both risk percentages.");
  if (t > 10) return fail("e3", "Max risk per trade cannot exceed 10%.");
  if (p > 30) return fail("e3", "Max total risk cannot exceed 30%.");
  if (p < t) return fail("e3", "Max total risk must be at least the per-trade risk.");
  state.max_trade_pct = t; state.max_portfolio_pct = p;
  state.allow_short_selling = $("shorts").checked;
  show(4);
};
$("b4").onclick = () => show(3);

// step 4
$("licenseChip").textContent = X.has_license ? "saved " + X.license_masked : "none saved";
$("wsurl").value = state.ws_url;
$("execMode").value = state.execution_mode;
$("failsafe").checked = state.local_exit_failsafe;
$("n4").onclick = async () => {
  fail("e4", "");
  const key = $("license").value.trim();
  state.ws_url = $("wsurl").value.trim();
  if (!state.ws_url) return fail("e4", "Enter the signal server URL.");
  if (!key && !X.has_license) return fail("e4", "Paste your Okkults license key.");
  state.license_key = key;
  state.execution_mode = $("execMode").value;
  state.local_exit_failsafe = $("failsafe").checked;
  const btn = $("n4"); busy(btn, true, "Verify & continue");
  const r = await api("/api/verify-license", { license_key: key, ws_url: state.ws_url });
  busy(btn, false, "Verify & continue");
  if (!r.data.ok) return fail("e4", r.data.error || "License check failed.");
  $("e4").className = "ok"; $("e4").textContent = r.data.verdict || "verified";
  setTimeout(() => { $("e4").textContent = ""; $("e4").className = "err"; summarize(); show(5); }, 650);
};
$("b5").onclick = () => show(4);

// step 5
function summarize() {
  const rows = [
    ["Account", state.trading_mode === "live" ? "LIVE — real money" : "Paper"],
    ["Signal server", state.ws_url || "(default)"],
    ["Max risk per trade", state.max_trade_pct + "%"],
    ["Max total risk at one time", state.max_portfolio_pct + "%"],
    ["Short selling", state.allow_short_selling ? "enabled" : "disabled"],
    ["Execution mode", state.execution_mode],
    ["Local failsafe", state.local_exit_failsafe ? "armed" : "disabled"],
    ["Resting stops", "none — never placed"],
  ];
  $("summary").innerHTML = rows.map((r) => `<div><span>${r[0]}</span><span>${r[1]}</span></div>`).join("");
}
$("arm").onclick = async () => {
  fail("e5", ""); $("ok5").textContent = "";
  const btn = $("arm"); busy(btn, true, "Arm terminal");
  const r = await api("/api/submit", state);
  busy(btn, false, "Arm terminal");
  if (!r.data.ok) return fail("e5", r.data.error || "Setup failed.");
  $("ok5").textContent = r.data.message || "Saved.";
  show(6);
  $("doneText").textContent = "Configuration saved. Close this tab — the terminal is starting.";
};
show(1);
</script>
</body>
</html>
"""


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "WebOnboardingServer",
    "run_web_onboarding",
]
