"""Onboarding wizard — OpenClaw-style guided terminal setup.

Design mirrors OpenClaw's ``onboard`` flow:

* route by config state (missing/broken -> wizard; valid -> run)
* security notice before gathering any secret
* **verify live before persisting** — a failed check never writes
* masked secret entry; secrets never re-echoed to the screen
* atomic ``.env`` write with 0600 permissions
* health-check summary at the end, then the terminal arms itself

All interactive I/O flows through the :class:`Prompter` protocol so tests
can drive a scripted fake prompter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from core.envwriter import mask_secret, merge_env_updates, read_env_file, write_env_file

__all__ = [
    "PROFILE_ORDER",
    "RISK_PROFILES",
    "ConsolePrompter",
    "OnboardingWizard",
    "Prompter",
    "WizardAnswers",
    "allocation_warning",
    "alpaca_key_error",
    "license_error",
    "verify_license_probe",
]

console = Console()

LICENSE_PREFIXES = ("ok_live_", "ok_paper_")
LIVE_ACK_WORDS = "I UNDERSTAND"
MAX_ALLOCATION_PCT_WARN = 0.10
DEFAULT_WS_URL = "wss://okkults-api.okkults.workers.dev/v1/stream"

# Install-time risk profiles. Level maps to concrete caps; `custom` walks
# every knob individually. Standard matches the website dashboard defaults.
RISK_PROFILES: dict[str, dict[str, float]] = {
    "conservative": {
        "max_portfolio_risk_pct": 0.20,
        "max_trade_allocation_pct": 0.05,
        "disaster_stop_pct": 0.05,
    },
    "standard": {
        "max_portfolio_risk_pct": 0.30,
        "max_trade_allocation_pct": 0.10,
        "disaster_stop_pct": 0.07,
    },
    "aggressive": {
        "max_portfolio_risk_pct": 0.40,
        "max_trade_allocation_pct": 0.15,
        "disaster_stop_pct": 0.10,
    },
}
PROFILE_ORDER = ["conservative", "standard", "aggressive", "custom"]


def verify_license_probe(
    ws_url: str, key: str, timeout_s: float = 4.0
) -> Callable[[], Awaitable[str | None]]:
    """Build a probe coroutine factory for the signal server.

    Returned callable -> None when the server accepts the license,
    a human message otherwise. Unreachable servers are non-fatal.
    """

    async def probe() -> str | None:
        try:
            import websockets

            async with websockets.connect(
                f"{ws_url}?license={key}", open_timeout=timeout_s
            ):
                return None
        except Exception as exc:  # noqa: BLE001 — probe must never crash setup
            name = type(exc).__name__
            if name in ("InvalidStatus", "InvalidHandshake", "ConnectionClosed"):
                return f"signal server rejected the license ({name})"
            return (
                f"signal server not reachable ({name}) — continuing offline; "
                "the terminal will reconnect when you start it"
            )

    return probe


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
        f"[bold red][!] WARNING: Sizing above {MAX_ALLOCATION_PCT_WARN * 100:.0f}% per "
        "ticker on earnings plays violates statistical risk parity.[/bold red]"
        f"[red] You requested {value * 100:.1f}%. A -20% gap on one report would cost "
        f"~{value * 20:.1f}% of total equity. Recommended ceiling: 5-10%.[/red]"
    )


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
    risk_profile: str = "standard"
    max_portfolio_risk_pct: float = 0.30
    max_trade_allocation_pct: float = 0.10
    per_trade_notional_cap: float | None = None
    cash_reserve: float = 0.0
    disaster_stop_pct: float = 0.07
    local_exit_failsafe: bool = True
    probes: dict[str, str] = field(default_factory=dict)


LicenseProbeFactory = Callable[[str], Callable[[], Awaitable[str | None]]]
BrokerProbe = Callable[[], Awaitable[str]]


class OnboardingWizard:
    """Guided setup: security notice -> creds -> live verify -> persist."""

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
        try:
            current = read_env_file(self._env_path)
            await self._license_lane(current)
            await self._broker_lane()
            await self._credentials_lane()
            await self._execution_lane()
            await self._risk_lane()
        except EOFError:
            console.print(
                "[yellow][!] Input stream closed (EOF) — setup paused after "
                "no changes were committed. Re-run `python main.py` to resume "
                "and complete the wizard.[/yellow]"
            )
            raise SystemExit(1) from None
        self._persist(current)
        await self.health_check()
        return self.answers

    # ------------------------------------------------------------------ #
    # lanes
    # ------------------------------------------------------------------ #
    async def _license_lane(self, current: dict[str, str]) -> None:
        console.print(Rule("[bold]Step 1 — Okkults license"))
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
            if verdict is not None:
                console.print(f"[yellow]{verdict}[/yellow]")
                if not self._prompt.confirm("Use this license anyway?", default=True):
                    continue
            self.answers.license_key = raw
            self.answers.probes["license"] = "verified" if verdict is None else "skipped-verify (saved anyway)"
            return

    async def _broker_lane(self) -> None:
        console.print(Rule("[bold]Step 2 — Broker"))
        raw = self._prompt.choice("Broker", ["alpaca", "ibkr"], default="alpaca")
        if raw == "ibkr":
            console.print(
                "[yellow][!] Interactive Brokers (IB Gateway) integration is "
                "currently in private beta. Coming in Phase 2. Please use Alpaca."
                "[/yellow]"
            )
            await self._broker_lane()
            return
        self.answers.broker = raw

    async def _credentials_lane(self) -> None:
        console.print(Rule("[bold]Step 3 — Alpaca credentials"))
        while True:
            key_id = self._prompt.secret("Alpaca API key id (PK…)")
            secret = self._prompt.secret("Alpaca secret key (…)")
            problem = alpaca_key_error(key_id, secret)
            if problem:
                console.print(f"[bold red]✗ {problem}[/bold red]")
                continue
            mode = self._prompt.choice(
                "Alpaca endpoint", ["paper", "live"], default="paper"
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
            self.answers.alpaca_api_key = key_id
            self.answers.alpaca_secret_key = secret
            self.answers.alpaca_is_paper = mode == "paper"
            console.print(
                "[bold red]LIVE trading enabled — caps still enforced.[/bold red]\n"
                if mode == "live"
                else "[green]PAPER endpoint selected (safe default).[/green]"
            )
            return

    async def _execution_lane(self) -> None:
        console.print(Rule("[bold]Step 4 — Execution mode"))
        mode = self._prompt.choice(
            "Execution mode", ["full_auto", "semi_auto"], default="full_auto"
        )
        if mode == "semi_auto":
            console.print(
                "[dim]semi_auto: the batch renders, chimes, and waits for your "
                "Enter/Spacebar before firing; timeout is 90s or 3:58 PM, "
                "whichever comes first.[/dim]"
            )
        else:
            console.print(
                "[dim]full_auto: orders fire the moment the WebSocket signal "
                "lands — no human in the loop.[/dim]"
            )
        self.answers.execution_mode = mode

    async def _risk_lane(self) -> None:
        """Risk profile chooser + custom knob walk + failsafe toggle."""
        console.print(Rule("[bold]Step 5 — Risk profile"))
        profile = self._prompt.choice(
            "Risk profile (per-trade sizing vs max risk budget)",
            PROFILE_ORDER,
            default="standard",
        )
        if profile != "custom":
            mapped = RISK_PROFILES[profile]
            self.answers.risk_profile = profile
            self.answers.max_portfolio_risk_pct = mapped["max_portfolio_risk_pct"]
            self.answers.max_trade_allocation_pct = mapped["max_trade_allocation_pct"]
            self.answers.disaster_stop_pct = mapped["disaster_stop_pct"]
            if profile == "aggressive":
                console.print(
                    allocation_warning(self.answers.max_trade_allocation_pct)
                )
                if not self._prompt.confirm(
                    "Override the risk-parity ceiling anyway?", default=False
                ):
                    fallback = RISK_PROFILES["standard"]
                    self.answers.risk_profile = "standard"
                    self.answers.max_portfolio_risk_pct = fallback["max_portfolio_risk_pct"]
                    self.answers.max_trade_allocation_pct = fallback["max_trade_allocation_pct"]
                    self.answers.disaster_stop_pct = fallback["disaster_stop_pct"]
                    console.print(
                        "[green]Kept STANDARD caps: 10% per ticker / 30% batch ceiling.[/green]"
                    )
        else:
            self.answers.risk_profile = "custom"
            await self._custom_risk_knobs()

        self.answers.local_exit_failsafe = self._prompt.confirm(
            "LOCAL_EXIT_FAILSAFE — auto-liquidate at 9:30:16 AM ET if the "
            "server's EXIT_SIGNAL never arrives?",
            default=True,
        )
        self._print_profile_summary()

    async def _custom_risk_knobs(self) -> None:
        """Custom profile: every risk knob walked individually."""
        portfolio = self._prompt.float(
            "MAX_PORTFOLIO_RISK_PCT (total exposure ceiling, e.g. 0.30)",
            self.answers.max_portfolio_risk_pct,
        )
        per_ticker = self._prompt.float(
            "MAX_TRADE_ALLOCATION_PCT (per-ticker target, e.g. 0.10)",
            self.answers.max_trade_allocation_pct,
        )
        if per_ticker > MAX_ALLOCATION_PCT_WARN:
            console.print(allocation_warning(per_ticker))
            if not self._prompt.confirm(
                "Override the risk-parity ceiling anyway?", default=False
            ):
                per_ticker = MAX_ALLOCATION_PCT_WARN
                console.print(
                    f"[green]Kept safe ceiling: {MAX_ALLOCATION_PCT_WARN * 100:.0f}% per ticker.[/green]"
                )
        cap = self._prompt.float(
            "PER_TRADE_NOTIONAL_CAP USD (0 = none)",
            self.answers.per_trade_notional_cap or 0.0,
        )
        reserve = self._prompt.float(
            "CASH_RESERVE USD held out of sizing", self.answers.cash_reserve
        )
        stop_pct = self._prompt.float(
            "DISASTER_STOP_PCT (default stop; server may override per signal)",
            self.answers.disaster_stop_pct,
        )
        self.answers.max_portfolio_risk_pct = max(0.0, portfolio)
        self.answers.max_trade_allocation_pct = max(0.0, per_ticker)
        self.answers.per_trade_notional_cap = cap if cap > 0 else None
        self.answers.cash_reserve = max(0.0, reserve)
        self.answers.disaster_stop_pct = min(max(0.0, stop_pct), 0.5)

    def _print_profile_summary(self) -> None:
        """Show the chosen caps as an institutional summary card."""
        a = self.answers
        table = Table(title=f"Risk profile — {a.risk_profile}", header_style="bold magenta")
        table.add_column("Knob", style="bold")
        table.add_column("Value", justify="right")
        table.add_row(
            "Max risk per trade (per ticker)",
            f"{a.max_trade_allocation_pct * 100:.0f}%",
        )
        table.add_row(
            "Max batch risk (portfolio ceiling)",
            f"{a.max_portfolio_risk_pct * 100:.0f}%",
        )
        table.add_row("Disaster stop (defaults; payload may override)", f"{a.disaster_stop_pct * 100:.0f}%")
        if a.per_trade_notional_cap is not None:
            table.add_row("Per-trade notional cap", f"${a.per_trade_notional_cap:,.0f}")
        if a.cash_reserve:
            table.add_row("Cash reserve", f"${a.cash_reserve:,.0f}")
        table.add_row("Execution mode", a.execution_mode)
        table.add_row("Local failsafe", "ARMED" if a.local_exit_failsafe else "disabled")
        console.print(table)
        worst_gap = a.max_trade_allocation_pct * 20
        console.print(
            f"[dim]Worst single-gap math: a -20% overnight gap on one report "
            f"costs about {worst_gap:.1f}% of equity "
            f"(sizing {a.max_trade_allocation_pct * 100:.0f}% × gap).[/dim]"
        )

    # ------------------------------------------------------------------ #
    # persistence + health check
    # ------------------------------------------------------------------ #
    def env_values(self) -> dict[str, str]:
        a = self.answers
        values = {
            "OKKULTS_LICENSE_KEY": a.license_key,
            "OKKULTS_WS_URL": self._ws_url,
            "BROKER": a.broker,
            "ALPACA_API_KEY": a.alpaca_api_key,
            "ALPACA_SECRET_KEY": a.alpaca_secret_key,
            "ALPACA_IS_PAPER": "true" if a.alpaca_is_paper else "false",
            "EXECUTION_MODE": a.execution_mode,
            "MAX_PORTFOLIO_RISK_PCT": f"{a.max_portfolio_risk_pct}",
            "MAX_TRADE_ALLOCATION_PCT": f"{a.max_trade_allocation_pct}",
            "CASH_RESERVE": f"{a.cash_reserve}",
            "DISASTER_STOP_PCT": f"{a.disaster_stop_pct}",
            "LOCAL_EXIT_FAILSAFE": "true" if a.local_exit_failsafe else "false",
        }
        if a.per_trade_notional_cap is not None:
            values["PER_TRADE_NOTIONAL_CAP"] = f"{a.per_trade_notional_cap}"
        return values

    def _persist(self, current: dict[str, str]) -> None:
        merged = merge_env_updates(current, self.env_values())
        write_env_file(merged, self._env_path)
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
            f"{mask_secret(self.answers.license_key)} ({self.answers.probes.get('license', 'checked')})",
        )
        mode = "PAPER" if self.answers.alpaca_is_paper else "LIVE"
        color = "green" if mode == "PAPER" else "bold red"
        table.add_row(
            "Broker", f"Alpaca [{color}]{mode}[/{color}] credentials verified"
        )
        table.add_row("Exec mode", self.answers.execution_mode)
        table.add_row(
            "Risk profile",
            f"{self.answers.risk_profile} — "
            f"{self.answers.max_trade_allocation_pct * 100:.0f}% per ticker / "
            f"{self.answers.max_portfolio_risk_pct * 100:.0f}% batch / "
            f"{self.answers.disaster_stop_pct * 100:.0f}% stop",
        )
        table.add_row(
            "Failsafe", "ARMED" if self.answers.local_exit_failsafe else "disabled"
        )
        verdict = await self._run_broker_probe()
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

    async def _run_broker_probe(self) -> str:
        if self._broker_probe is not None:
            return await self._broker_probe()
        from brokers.alpaca_client import AlpacaBroker

        broker = AlpacaBroker(
            api_key=self.answers.alpaca_api_key,
            secret_key=self.answers.alpaca_secret_key,
            paper=self.answers.alpaca_is_paper,
        )
        connected = await broker.connect()
        if connected:
            try:
                equity = await broker.get_equity()
                mode = "PAPER" if self.answers.alpaca_is_paper else "LIVE"
                return f"OK — connected, equity ${equity:,.2f} ({mode})"
            except Exception as exc:  # noqa: BLE001
                return f"OK — connected, but equity failed ({type(exc).__name__})"
        return "FAIL — Alpaca rejected the credentials"

    def security_notice(self) -> None:
        console.print(
            Panel(
                "[bold]Okkults Quant Terminal[/bold] — non-custodial.\n\n"
                "This terminal executes against [cyan]your[/cyan] broker account "
                "with [cyan]your[/cyan] credentials.\n"
                "Broker keys are stored only on this disk, transmitted only to "
                "Alpaca, and printed nowhere — not in logs, not in telemetry.\n"
                "The Okkults server can instruct when to trade, but can never "
                "move, hold, or withdraw your funds.",
                title="[bold cyan]Security notice",
                border_style="cyan",
            )
        )


async def probe_broker_credentials(
    api_key: str, secret_key: str, paper: bool
) -> str:
    """Standalone live-credential probe, reused by pre-flight after onboarding."""
    from brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
    if not await broker.connect():
        return "FAIL — Alpaca rejected the credentials"
    equity = await broker.get_equity()
    mode = "PAPER" if paper else "LIVE"
    return f"OK — connected, equity ${equity:,.2f} ({mode})"
