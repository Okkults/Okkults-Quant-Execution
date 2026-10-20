"""Onboarding wizard — OpenClaw-style guided terminal setup.

Flow (exactly the order an operator needs to make the safety decision):

1. security notice (non-custodial architecture, up front)
2. **paper or live** — live requires typing ``I UNDERSTAND``
3. Alpaca API key + secret — verified live before anything is persisted
4. risk per trade and max total risk at one time — hard-capped at 10%/30%
5. Okkults license key — masked entry, probed against the signal server
6. execution mode (``full_auto`` / ``semi_auto``) and the local failsafe

The wizard never writes a config until the broker credentials have passed a
real connection probe. Secrets are never echoed, and the ``.env`` write is
atomic with 0600 permissions.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from config.settings import (
    DEFAULT_TRADE_PCT,
    HARD_MAX_PORTFOLIO_PCT,
    HARD_MAX_TRADE_PCT,
)
from core.envwriter import mask_secret, merge_env_updates, read_env_file, write_env_file

__all__ = [
    "ConsolePrompter",
    "OnboardingWizard",
    "Prompter",
    "WizardAnswers",
    "allocation_warning",
    "alpaca_key_error",
    "build_env_values",
    "license_error",
    "persist_answers",
    "probe_broker_credentials",
    "risk_pct_error",
    "verify_license_probe",
]

console = Console()

LICENSE_PREFIXES = ("ok_live_", "ok_paper_")
LIVE_ACK_WORDS = "I UNDERSTAND"
DEFAULT_WS_URL = "wss://okkults-api.okkults.workers.dev/v1/stream"


def verify_license_probe(
    ws_url: str, key: str, timeout_s: float = 4.0
) -> Callable[[], Awaitable[str | None]]:
    """Build a probe coroutine factory for the signal server.

    Returned callable -> None when the server accepts the license,
    a human message otherwise. Unreachable servers are non-fatal.
    """

    async def probe() -> str | None:
        try:
            import asyncio
            import json

            import websockets

            async with websockets.connect(
                f"{ws_url}?license={key}", open_timeout=timeout_s
            ) as socket:
                # Wait for the server's SUBSCRIBED confirmation instead of
                # trusting a bare handshake: some servers accept the upgrade
                # and then close with 4xxx auth codes.
                while True:
                    raw = await asyncio.wait_for(
                        socket.recv(), timeout=timeout_s
                    )
                    try:
                        frame = json.loads(raw) if isinstance(raw, str) else {}
                    except ValueError:
                        continue
                    if frame.get("event") == "SUBSCRIBED":
                        return None  # noqa: TRY300
                    if frame.get("event") in ("SYSTEM_PING", "PING"):
                        continue
                    # Reached a real WS server but it never confirmed a
                    # subscription — that is a MISMATCH (wrong server URL).
                    # Keep the word "rejected" so every downstream gate
                    # (web wizard's historical filter included) fails this.
                    return (
                        "rejected: connected, but the server never confirmed "
                        "your license (no SUBSCRIBED frame). Usually this "
                        "means 'Signal server URL' points somewhere else "
                        "than the server that issued this key — re-check "
                        "the ws:// URL your operator gave you."
                    )
        except Exception as exc:  # noqa: BLE001 — probe must never crash setup
            name = type(exc).__name__
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            if code == 4001:
                return "signal server rejected the license (code 4001: invalid key)"
            if code == 4002:
                return "signal server rejected the license (code 4002: live key on paper rig)"
            if name in ("InvalidStatus", "InvalidHandshake", "ConnectionClosed"):
                return f"signal server rejected the license ({name})"
            return (
                f"signal server not reachable ({name}) — continuing offline; "
                "the terminal will reconnect when you start it"
            )

    return probe


def humanize_license_rejection(
    verdict: str | None, ws_url: str, key: str
) -> str | None:
    """Translate a probe rejection into fix-it guidance when we can.

    The classic false alarm: a TEST key (ok_paper_test_…) probed against
    the PRODUCTION engine because the operator never changed the URL
    field. Name that mistake instead of showing raw (InvalidStatus).
    `verdict` is None when the probe SUCCEEDED — pass it through.
    """
    if verdict is None or "rejected" not in verdict:
        return verdict
    if ws_url == DEFAULT_WS_URL and key.startswith("ok_paper_test_"):
        return (
            "This is a TEST license key — it only works on the test rig, not on "
            f"production ({DEFAULT_WS_URL}). 'Signal server URL' still contains "
            "the production address: DELETE it and paste the ws:// endpoint your "
            "operator gave you (e.g. ws://192.168.x.x:8000/v1/stream), then "
            "verify again."
        )
    return verdict


class Prompter(Protocol):
    """Interactive-I/O boundary the wizard renders through."""

    def text(self, prompt: str, default: str = "") -> str: ...

    def secret(self, prompt: str) -> str: ...

    def choice(self, prompt: str, options: list[str], default: str) -> str: ...

    def float(self, prompt: str, default: float) -> float: ...

    def confirm(self, prompt: str, default: bool = True) -> bool: ...


class ConsolePrompter:
    """Rich-powered default prompter."""

    def text(self, prompt: str, default: str = "") -> str:
        from rich.prompt import Prompt

        return Prompt.ask(f"[cyan]{prompt}[/cyan]", default=default).strip()

    def secret(self, prompt: str) -> str:
        from rich.prompt import Prompt

        return Prompt.ask(
            f"[cyan]{prompt}[/cyan] [dim](input hidden)[/dim]", password=True
        ).strip()

    def choice(self, prompt: str, options: list[str], default: str) -> str:
        from rich.prompt import Prompt

        pretty = " / ".join(f"[bold]{o}[/bold]" for o in options)
        return Prompt.ask(
            f"[cyan]{prompt}[/cyan] [dim]({pretty})[/dim]",
            choices=options,
            default=default,
        )

    def float(self, prompt: str, default: float) -> float:
        from rich.prompt import FloatPrompt

        return float(FloatPrompt.ask(f"[cyan]{prompt}[/cyan]", default=default))

    def confirm(self, prompt: str, default: bool = True) -> bool:
        from rich.prompt import Confirm

        return Confirm.ask(f"[cyan]{prompt}[/cyan]", default=default)


# ---------------------------------------------------------------------- #
# Validation helpers (pure functions, unit-tested directly)
# ---------------------------------------------------------------------- #
def license_error(key: str) -> str | None:
    if not key:
        return "license key is empty"
    if not key.startswith(LICENSE_PREFIXES):
        return "license must start with ok_live_ or ok_paper_"
    if len(key) <= len("ok_live_"):
        return "license key looks truncated"
    return None


def allocation_warning(value: float) -> str:
    return (
        f"[bold red][!] WARNING: Sizing above {HARD_MAX_TRADE_PCT * 100:.0f}% per "
        "ticker on earnings plays violates statistical risk parity.[/bold red]"
        f"[red] You requested {value * 100:.1f}%. A -20% gap on one report would cost "
        f"~{value * 20:.1f}% of total equity. Recommended ceiling: 5-10%, and the "
        "client enforces a hard 10% cap.[/red]"
    )


def risk_pct_error(
    label: str,
    value: float,
    *,
    maximum: float,
    minimum: float = 0.0,
) -> str | None:
    """Human-readable validation for the two onboarding risk questions."""
    if value <= minimum:
        return f"{label} must be greater than {minimum * 100:.0f}%"
    if value > maximum:
        return (
            f"{label} cannot exceed {maximum * 100:.0f}% — this is a hard "
            "risk ceiling and cannot be overridden."
        )
    return None


def alpaca_key_error(key_id: str, secret_key: str) -> str | None:
    if not key_id or not secret_key:
        return "both key id and secret are required"
    if key_id == secret_key:
        return "key id and secret appear to be identical"
    return None


@dataclass
class WizardAnswers:
    license_key: str = ""
    broker: str = "alpaca"
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_is_paper: bool = True
    execution_mode: str = "full_auto"
    max_trade_allocation_pct: float = DEFAULT_TRADE_PCT
    max_portfolio_risk_pct: float = HARD_MAX_PORTFOLIO_PCT
    per_trade_notional_cap: float | None = None
    cash_reserve: float = 0.0
    allow_short_selling: bool = False
    local_exit_failsafe: bool = True
    probes: dict[str, str] = field(default_factory=dict)


LicenseProbeFactory = Callable[[str], Callable[[], Awaitable[str | None]]]
BrokerProbe = Callable[[str, str, bool], Awaitable[str]]


def build_env_values(
    answers: WizardAnswers, ws_url: str = DEFAULT_WS_URL
) -> dict[str, str]:
    """Serialize wizard answers into the exact ``.env`` key/value shape."""
    values = {
        "OKKULTS_LICENSE_KEY": answers.license_key,
        "OKKULTS_WS_URL": ws_url,
        "BROKER": answers.broker,
        "ALPACA_API_KEY": answers.alpaca_api_key,
        "ALPACA_SECRET_KEY": answers.alpaca_secret_key,
        "ALPACA_IS_PAPER": "true" if answers.alpaca_is_paper else "false",
        "LIVE_TRADING_ACK": "YES" if not answers.alpaca_is_paper else "NO",
        "EXECUTION_MODE": answers.execution_mode,
        "MAX_PORTFOLIO_RISK_PCT": f"{answers.max_portfolio_risk_pct}",
        "MAX_TRADE_ALLOCATION_PCT": f"{answers.max_trade_allocation_pct}",
        "CASH_RESERVE": f"{answers.cash_reserve}",
        "ALLOW_SHORT_SELLING": (
            "true" if answers.allow_short_selling else "false"
        ),
        "LOCAL_EXIT_FAILSAFE": (
            "true" if answers.local_exit_failsafe else "false"
        ),
    }
    if answers.per_trade_notional_cap is not None:
        values["PER_TRADE_NOTIONAL_CAP"] = f"{answers.per_trade_notional_cap}"
    return values


def persist_answers(
    answers: WizardAnswers,
    current: dict[str, str],
    env_path: str | Path,
    ws_url: str = DEFAULT_WS_URL,
) -> None:
    """Merge answers over the current file, drop legacy keys, write 0600."""
    merged = merge_env_updates(current, build_env_values(answers, ws_url))
    # Legacy knob removed from the client: never carry it forward.
    merged.pop("DISASTER_STOP_PCT", None)
    write_env_file(merged, env_path)


class OnboardingWizard:
    """Guided setup: security notice -> mode -> credentials -> risk -> license."""

    def __init__(
        self,
        prompter: Prompter | None = None,
        env_path: str = ".env",
        ws_url: str = DEFAULT_WS_URL,
        license_probe_factory: LicenseProbeFactory | None = None,
        broker_probe: BrokerProbe | None = None,
    ) -> None:
        self._prompt = prompter or ConsolePrompter()
        self._env_path = env_path
        self._ws_url = ws_url
        self._license_probe_factory = license_probe_factory or (
            lambda key: verify_license_probe(ws_url, key)
        )
        self._broker_probe = broker_probe
        self.answers = WizardAnswers()

    # ------------------------------------------------------------------ #
    # main flow
    # ------------------------------------------------------------------ #
    async def run(self) -> WizardAnswers:
        self.security_notice()
        # Signal-endpoint lane: the wizard default is the production engine.
        # A test rig runs its own server — allow pasting a ws:// LAN URL here
        # so the license probe (and the .env we write) point at it.
        saved_url = self._ws_url
        asked_url = self._prompt.text(
            "Signal server URL (ws:// or wss:// …/v1/stream)",
            default=saved_url,
        ).strip()
        if asked_url and asked_url != saved_url:
            self._ws_url = asked_url
            self._license_probe_factory = (
                lambda key: verify_license_probe(self._ws_url, key)
            )
        try:
            current = read_env_file(self._env_path)
            await self._trading_mode_lane(current)
            await self._credentials_lane(current)
            await self._risk_lane(current)
            await self._license_lane(current)
            await self._execution_lane(current)
            self.answers.local_exit_failsafe = self._prompt.confirm(
                "LOCAL_EXIT_FAILSAFE — auto-liquidate client-opened positions "
                "at 9:30:16 AM ET if the server's EXIT_SIGNAL never arrives?",
                default=self._env_bool(
                    current, "LOCAL_EXIT_FAILSAFE", self.answers.local_exit_failsafe
                ),
            )
        except EOFError:
            console.print(
                "[yellow][!] Input stream closed (EOF) — setup paused before "
                "any changes were committed. Re-run `python main.py` to resume "
                "and complete the wizard.[/yellow]"
            )
            raise SystemExit(1) from None
        self._persist(current)
        await self.health_check()
        return self.answers

    # ------------------------------------------------------------------ #
    # lanes
    # ------------------------------------------------------------------ #
    async def _trading_mode_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 1 — Paper or live"))
        default = "paper" if self._env_bool(current, "ALPACA_IS_PAPER", True) else "live"
        mode = self._prompt.choice(
            "Trading mode", ["paper", "live"], default=default
        )
        if mode == "live":
            ack = self._prompt.text(
                f"Type '{LIVE_ACK_WORDS}' to acknowledge real-money risk"
            )
            if ack != LIVE_ACK_WORDS:
                console.print(
                    "[yellow]Confirmation text mismatch — staying on PAPER[/yellow]"
                )
                mode = "paper"
        self.answers.alpaca_is_paper = mode == "paper"
        console.print(
            "[bold red]LIVE trading enabled — real money. Hard risk caps still "
            "apply; no resting stops will be placed.[/bold red]\n"
            if mode == "live"
            else "[green]PAPER endpoint selected (safe default).[/green]"
        )

    async def _credentials_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 2 — Alpaca credentials"))
        existing_key = current.get("ALPACA_API_KEY", "")
        existing_secret = current.get("ALPACA_SECRET_KEY", "")
        key_extra = f" [current {mask_secret(existing_key)}]" if existing_key else ""
        while True:
            key_id = self._prompt.secret(
                f"Alpaca API key id (PK…){key_extra}, Enter keeps current"
            )
            secret = self._prompt.secret(
                "Alpaca secret key (Enter keeps current)"
            )
            key_id = key_id or existing_key
            secret = secret or existing_secret
            problem = alpaca_key_error(key_id, secret)
            if problem:
                console.print(f"[bold red]✗ {problem}[/bold red]")
                continue

            console.print("[dim]Verifying credentials against Alpaca…[/dim]")
            verdict = await self._run_broker_probe(
                key_id, secret, self.answers.alpaca_is_paper
            )
            if verdict.startswith("OK"):
                self.answers.alpaca_api_key = key_id
                self.answers.alpaca_secret_key = secret
                self.answers.probes["broker"] = verdict
                console.print(f"[green][OK][/green] {verdict}")
                return

            console.print(f"[bold red]✗ {verdict}[/bold red]")
            if not self._prompt.confirm(
                "Retry Alpaca credentials?", default=True
            ):
                console.print(
                    "[bold red]Cannot arm without valid Alpaca credentials. "
                    "Nothing was saved.[/bold red]"
                )
                raise SystemExit(1)

    async def _risk_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 3 — Risk limits"))
        default_trade = min(
            max(
                self._current_float(
                    current, "MAX_TRADE_ALLOCATION_PCT", DEFAULT_TRADE_PCT
                ),
                0.01,
            ),
            HARD_MAX_TRADE_PCT,
        )
        while True:
            raw_trade = self._prompt.float(
                "Max risk per trade (% of equity, e.g. 5; hard cap 10)",
                default=default_trade * 100,
            )
            trade_pct = raw_trade / 100.0
            if raw_trade < 1:
                console.print(
                    "[bold red]✗ Enter a percentage such as 5, not a fraction.[/bold red]"
                )
                continue
            problem = risk_pct_error(
                "Max risk per trade",
                trade_pct,
                maximum=HARD_MAX_TRADE_PCT,
            )
            if problem:
                console.print(f"[bold red]✗ {problem}[/bold red]")
                continue
            break

        default_portfolio = min(
            max(
                self._current_float(
                    current, "MAX_PORTFOLIO_RISK_PCT", HARD_MAX_PORTFOLIO_PCT
                ),
                0.01,
            ),
            HARD_MAX_PORTFOLIO_PCT,
        )
        while True:
            raw_portfolio = self._prompt.float(
                "Max total risk at one time (% of equity, e.g. 30; hard cap 30)",
                default=default_portfolio * 100,
            )
            portfolio_pct = raw_portfolio / 100.0
            if raw_portfolio < 1:
                console.print(
                    "[bold red]✗ Enter a percentage such as 30, not a fraction.[/bold red]"
                )
                continue
            problem = risk_pct_error(
                "Max total risk at one time",
                portfolio_pct,
                maximum=HARD_MAX_PORTFOLIO_PCT,
            )
            if problem:
                console.print(f"[bold red]✗ {problem}[/bold red]")
                continue
            if portfolio_pct < trade_pct:
                console.print(
                    "[bold red]✗ Max total risk must be at least the per-trade "
                    "risk.[/bold red]"
                )
                continue
            break

        self.answers.max_trade_allocation_pct = trade_pct
        self.answers.max_portfolio_risk_pct = portfolio_pct
        self.answers.per_trade_notional_cap = self._current_optional_float(
            current, "PER_TRADE_NOTIONAL_CAP"
        )
        self.answers.cash_reserve = self._current_float(
            current, "CASH_RESERVE", 0.0
        )
        self.answers.allow_short_selling = self._prompt.confirm(
            "Allow SELL signals to open short positions? (default off)",
            default=self._env_bool(
                current, "ALLOW_SHORT_SELLING", self.answers.allow_short_selling
            ),
        )
        self._print_risk_summary()

    async def _license_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 4 — Okkults API key"))
        existing = current.get("OKKULTS_LICENSE_KEY", "")
        shown = mask_secret(existing)
        extra = "" if existing == "" else f" [current {shown}]"
        while True:
            raw = self._prompt.secret(
                f"License key (ok_live_… / ok_paper_…){extra}, Enter keeps current"
            )
            if raw == "" and existing:
                self.answers.license_key = existing
                self.answers.probes["license"] = "kept current"
                return
            problem = license_error(raw)
            if problem:
                console.print(f"[bold red]✗ {problem}[/bold red]")
                continue
            console.print("[dim]Probing the signal server…[/dim]")
            verdict = await self._license_probe_factory(raw)()
            verdict = humanize_license_rejection(verdict, self._ws_url, raw)
            if verdict is not None:
                if "rejected" in verdict:
                    console.print(f"[bold red]✗ {verdict}[/bold red]")
                    continue
                console.print(f"[yellow]{verdict}[/yellow]")
                if not self._prompt.confirm("Use this license anyway?", default=True):
                    continue
            self.answers.license_key = raw
            self.answers.probes["license"] = (
                "verified" if verdict is None else "saved (probe failed)"
            )
            return

    async def _execution_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 5 — Execution mode"))
        default = current.get("EXECUTION_MODE", "full_auto")
        if default not in ("full_auto", "semi_auto"):
            default = "full_auto"
        mode = self._prompt.choice(
            "Execution mode", ["full_auto", "semi_auto"], default=default
        )
        if mode == "semi_auto":
            console.print(
                "[dim]semi_auto: the batch renders, chimes, and waits for your "
                "Enter/Spacebar before firing; timeout is 90s or the entry "
                "cutoff, whichever comes first.[/dim]"
            )
            if not sys.stdin.isatty():
                console.print(
                    "[yellow][!] semi_auto requires an interactive TTY; pre-flight "
                    "will refuse to arm in headless/cloud mode. Use full_auto "
                    "there.[/yellow]"
                )
        else:
            console.print(
                "[dim]full_auto: orders fire the moment the WebSocket signal "
                "lands — no human in the loop.[/dim]"
            )
        self.answers.execution_mode = mode

    def _print_risk_summary(self) -> None:
        a = self.answers
        table = Table(
            title="Risk limits (hard caps enforced)", header_style="bold magenta"
        )
        table.add_column("Knob", style="bold")
        table.add_column("Value", justify="right")
        table.add_row(
            "Max risk per trade",
            f"{a.max_trade_allocation_pct * 100:.1f}%",
        )
        table.add_row(
            "Max total risk at one time",
            f"{a.max_portfolio_risk_pct * 100:.1f}%",
        )
        table.add_row(
            "Short selling", "ENABLED" if a.allow_short_selling else "disabled"
        )
        if a.per_trade_notional_cap is not None:
            table.add_row("Per-trade notional cap", f"${a.per_trade_notional_cap:,.0f}")
        if a.cash_reserve:
            table.add_row("Cash reserve", f"${a.cash_reserve:,.0f}")
        console.print(table)
        console.print(
            f"[dim]Worst single-gap math: a -20% overnight gap on one report "
            f"costs about {a.max_trade_allocation_pct * 20:.1f}% of equity "
            f"(sizing {a.max_trade_allocation_pct * 100:.1f}% × gap).[/dim]"
        )

    # ------------------------------------------------------------------ #
    # persistence + health check
    # ------------------------------------------------------------------ #
    def env_values(self) -> dict[str, str]:
        return build_env_values(self.answers, self._ws_url)

    def _persist(self, current: dict[str, str]) -> None:
        persist_answers(self.answers, current, self._env_path, self._ws_url)
        console.print(
            f"[green][OK][/green] Configuration written to {self._env_path} "
            "(atomic write, chmod 600)."
        )

    async def health_check(self) -> bool:
        console.print(Rule("[bold]Pre-flight health check"))
        table = Table(show_header=True, header_style="bold")
        table.add_column("Check", style="bold")
        table.add_column("Result")
        table.add_row(
            "License",
            f"{mask_secret(self.answers.license_key)} "
            f"({self.answers.probes.get('license', 'checked')})",
        )
        mode = "PAPER" if self.answers.alpaca_is_paper else "LIVE"
        color = "green" if mode == "PAPER" else "bold red"
        table.add_row(
            "Broker", f"Alpaca [{color}]{mode}[/{color}] credentials verified"
        )
        table.add_row(
            "Risk",
            f"{self.answers.max_trade_allocation_pct * 100:.1f}% per trade / "
            f"{self.answers.max_portfolio_risk_pct * 100:.1f}% total at one time",
        )
        table.add_row(
            "Short selling",
            "ENABLED" if self.answers.allow_short_selling else "disabled",
        )
        table.add_row("Exec mode", self.answers.execution_mode)
        table.add_row(
            "Failsafe", "ARMED" if self.answers.local_exit_failsafe else "disabled"
        )
        verdict = await self._run_broker_probe(
            self.answers.alpaca_api_key,
            self.answers.alpaca_secret_key,
            self.answers.alpaca_is_paper,
        )
        table.add_row("Live broker probe", verdict)
        console.print(table)
        if verdict.startswith("OK"):
            console.print(
                "[bold green]SYSTEM ARMED — listening for the 3:50 PM entry "
                "window. Keep this machine running until the open-cross "
                "liquidation (or until the next EXIT_SIGNAL clears you)."
                "[/bold green]"
            )
            return True
        console.print(
            "[yellow]Setup saved. Fix the failing probe and re-run "
            "`python main.py --onboard`.[/yellow]"
        )
        return False

    async def _run_broker_probe(
        self, api_key: str, secret_key: str, paper: bool
    ) -> str:
        if self._broker_probe is not None:
            return await self._broker_probe(api_key, secret_key, paper)
        return await probe_broker_credentials(api_key, secret_key, paper)

    def security_notice(self) -> None:
        console.print(
            Panel(
                "[bold]Okkults Quant Terminal[/bold] — non-custodial.\n\n"
                "This terminal executes against [cyan]your[/cyan] broker account "
                "with [cyan]your[/cyan] credentials.\n"
                "Broker keys are stored only on this disk, transmitted only to "
                "Alpaca, and printed nowhere — not in logs, not in telemetry.\n"
                "The Okkults server can instruct when to trade, but can never "
                "move, hold, or withdraw your funds.\n"
                "This client never places resting stop orders; exits are "
                "server-driven plus an optional local failsafe.",
                title="[bold cyan]Security notice",
                border_style="cyan",
            )
        )

    @staticmethod
    def _env_bool(current: dict[str, str], key: str, default: bool) -> bool:
        raw = current.get(key)
        if raw is None or raw.strip() == "":
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _current_float(current: dict[str, str], key: str, default: float) -> float:
        raw = current.get(key)
        if raw is None or raw.strip() == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    @staticmethod
    def _current_optional_float(
        current: dict[str, str], key: str
    ) -> float | None:
        raw = current.get(key)
        if raw is None or raw.strip() == "":
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None


async def probe_broker_credentials(
    api_key: str, secret_key: str, paper: bool
) -> str:
    """Standalone live-credential probe, reused by onboarding and pre-flight."""
    from brokers.alpaca_client import AlpacaBroker

    try:
        broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
        if not await broker.connect():
            return "FAIL — Alpaca rejected the credentials"
        equity = await broker.get_equity()
        mode = "PAPER" if paper else "LIVE"
        return f"OK — connected, equity ${equity:,.2f} ({mode})"
    except Exception as exc:  # noqa: BLE001 — probe returns a verdict, never raises
        return f"FAIL — {type(exc).__name__}: {exc}"
