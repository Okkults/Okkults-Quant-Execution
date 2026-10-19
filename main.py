"""Okkults Quant Terminal — entrypoint.

Routing (OpenClaw-style, by config state):

* ``python main.py`` -> config missing/incomplete -> onboarding wizard;
  valid -> pre-flight -> armed run loop
* ``python main.py --onboard`` -> force the wizard to re-run
* ``python main.py --mock`` -> full simulated cycle, no credentials needed

Non-custodial: this process talks to YOUR broker with YOUR keys. Keys
never leave the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import NoReturn

from rich.align import Align
from rich.console import Console
from rich.panel import Panel

from config.settings import Settings, load_settings, missing_required_fields
from core.envwriter import ENV_HEADER, write_env_file

console = Console()

BANNER = r"""
 [cyan] _  _  _  _ _ _ ._ _  _  | _  _  _|_ _
 (_|(_|||) | | (_)| (_)_\/_)(_|| |  _|(_) _|
 --
                    https://okkults.okkults.workers.dev
"""


def print_banner() -> None:
    console.print(
        Panel(
            Align.center(
                BANNER + "\n[b]OKKULTS QUANT TERMINAL[/b]\n"
                "[dim]pre-earnings execution client — non-custodial, "
                "open-source[/dim]"
            ),
            border_style="cyan",
            padding=(1, 4),
        )
    )


def ensure_env_file() -> None:
    """First-run convenience: create an empty .env (chmod 600) if absent."""
    if os.path.exists(".env"):
        return
    write_env_file(
        {
            "OKKULTS_LICENSE_KEY": "",
            "OKKULTS_WS_URL": "wss://okkults-api.okkults.workers.dev/v1/stream",
            "BROKER": "alpaca",
            "ALPACA_API_KEY": "",
            "ALPACA_SECRET_KEY": "",
            "ALPACA_IS_PAPER": "true",
            "LIVE_TRADING_ACK": "NO",
            "EXECUTION_MODE": "full_auto",
            "MAX_PORTFOLIO_RISK_PCT": "0.30",
            "MAX_TRADE_ALLOCATION_PCT": "0.05",
            "CASH_RESERVE": "0.0",
            "ALLOW_SHORT_SELLING": "false",
            "LOCAL_EXIT_FAILSAFE": "true",
        },
        ".env",
        header=ENV_HEADER,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Okkults Quant Terminal — automated pre-earnings execution client",
    )
    parser.add_argument(
        "--onboard",
        action="store_true",
        help="force the guided onboarding wizard to re-run",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="use the terminal onboarding wizard instead of the browser",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run a full simulated signal cycle with no credentials",
    )
    return parser.parse_args(argv)


def _settings_or_none() -> Settings | None:
    try:
        settings = load_settings()
    except Exception as exc:
        console.print(f"[bold red]✗ .env is invalid: {exc}[/bold red]")
        return None
    problems = missing_required_fields(settings)
    if problems:
        console.print(f"[yellow]Missing required fields: {', '.join(problems)}[/yellow]")
        return None
    return settings


def resolve_config(force_onboard: bool, prefer_cli: bool = False) -> Settings:
    """Route by config state — browser wizard first, terminal on request."""
    headless = os.environ.get("HEADLESS", "") == "1"
    if not force_onboard:
        settings = _settings_or_none()
        if settings is not None:
            return settings
    if headless:
        console.print(
            "[bold red]✗ Headless mode with incomplete config — cannot run the "
            "interactive wizard. Configure secrets via environment "
            "variables and restart.[/bold red]"
        )
        raise SystemExit(1)
    if not prefer_cli:
        from core.web_onboarding import run_web_onboarding

        console.print("[cyan]Opening browser onboarding…[/cyan]")
        answers = run_web_onboarding(env_path=".env")
        if answers is None:
            console.print(
                "[red]Setup was not completed — nothing was saved.[/red]"
            )
            raise SystemExit(1)
    else:
        console.print("[cyan]Entering terminal onboarding wizard…[/cyan]")
        from core.onboarding import OnboardingWizard

        wizard = OnboardingWizard(env_path=".env")
        asyncio.run(wizard.run())
    reloaded = load_settings()
    missing = missing_required_fields(reloaded)
    if missing:
        console.print(
            f"[bold red]Configuration incomplete — still missing: "
            f"{', '.join(missing)}. Re-run `python main.py`.[/bold red]"
        )
        raise SystemExit(1)
    return reloaded


async def preflight(settings: Settings) -> bool:
    console.rule("[bold]Pre-flight")
    from brokers.ibkr_client import IBKRClient

    if settings.BROKER == "ibkr":
        stub = IBKRClient()
        await stub.connect()
        return False

    if settings.EXECUTION_MODE == "semi_auto" and not sys.stdin.isatty():
        console.print(
            "[bold red]✗ semi_auto requires an interactive TTY. Use full_auto "
            "in headless/cloud mode, or run the terminal in a real terminal."
            "[/bold red]"
        )
        return False

    if not settings.ALPACA_IS_PAPER:
        console.print(
            "[bold red]LIVE mode — real money. No resting stops will be placed; "
            "max 5% default per trade, 10% hard cap, 30% total.[/bold red]"
        )
    if settings.ALLOW_SHORT_SELLING:
        console.print(
            "[yellow]ALLOW_SHORT_SELLING=true — SELL signals can open short "
            "positions.[/yellow]"
        )

    from brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_IS_PAPER,
    )
    try:
        summary = await broker.get_account_summary()
        positions = await broker.get_open_positions()
    except Exception as exc:  # noqa: BLE001 — preflight reports, never crashes
        console.print(
            f"[bold red]✗ Alpaca pre-flight failed — "
            f"{type(exc).__name__}: {exc}[/bold red]"
        )
        return False
    mode = "[green]PAPER[/green]" if summary["paper"] else "[bold red]LIVE[/bold red]"
    console.print(
        f"[cyan][OK][/cyan] Alpaca {mode} — status={summary['status']} "
        f"equity=${summary['equity']:,.2f} cash=${summary['cash']:,.2f}"
    )
    if positions:
        console.print(
            f"[yellow][!] {len(positions)} existing broker position(s) detected. "
            "This terminal is not managing them and will never close them; it "
            "only tracks positions it opens itself.[/yellow]"
        )
    return summary["status"] == "ACTIVE"


async def run_loop(settings: Settings) -> None:
    from datetime import datetime

    from brokers.alpaca_client import AlpacaBroker
    from core.executor import SignalExecutor, seconds_until_next_open_cross
    from signals.listener import SignalListener
    from signals.protocol import ET

    broker = AlpacaBroker(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_IS_PAPER,
    )
    if not await broker.connect():
        console.print(
            "[bold red]✗ Broker connection failed after pre-flight — refusing "
            "to arm.[/bold red]"
        )
        return
    executor = SignalExecutor(broker=broker, settings=settings)
    listener = SignalListener(
        ws_url=settings.OKKULTS_WS_URL,
        license_key=settings.OKKULTS_LICENSE_KEY,
        callbacks=executor.callbacks(),
        broker_status=lambda: broker.connected,
    )
    tasks = [
        asyncio.create_task(listener.run(), name="signal-listener"),
        asyncio.create_task(executor.failsafe_watchdog(), name="failsafe-watchdog"),
    ]
    console.print(
        f"[bold green]ARMED[/bold green] — mode [bold]{settings.EXECUTION_MODE}[/bold] "
        f"({settings.BROKER}, "
        + ("paper" if settings.ALPACA_IS_PAPER else "[bold red]live[/bold red]")
        + f"). Risk {settings.MAX_TRADE_ALLOCATION_PCT:.1%}/trade, "
        f"{settings.MAX_PORTFOLIO_RISK_PCT:.1%} total. No resting stops. "
        f"Next open-cross in {seconds_until_next_open_cross() / 3600:.1f}h. "
        "Press Ctrl+C to stop (open positions are loudly confirmed, never silently abandoned)."
    )
    console.print("[dim]Entry window: 3:50 PM ET · Exit: 9:30:15 AM ET open cross[/dim]")
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        console.print(f"[dim]shutdown at {datetime.now(tz=ET).isoformat()}[/dim]")


async def shutdown_guard(settings: Settings) -> None:
    """Ctrl+C handler: warn loudly; never liquidate unrelated holdings."""
    from brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_IS_PAPER,
    )
    try:
        positions = await broker.get_open_positions()
    except Exception:
        positions = []
    if not positions:
        console.print("[green]No open positions — terminal stopped clean.[/green]")
        return
    console.print(
        f"[bold red][!!] {len(positions)} OPEN POSITION(S) at your broker when "
        "shutting down:[/bold red]"
    )
    for pos in positions:
        console.print(
            f"  - {pos['symbol']}: qty={pos['qty']} "
            f"entry=${float(pos['avg_entry_price']):.2f}"
        )
    console.print(
        "[yellow]This terminal does not place resting stops and cannot tell "
        "which of these positions it opened after a restart, so it will not "
        "liquidate them automatically. Restart before the open to receive the "
        "EXIT_SIGNAL/failsafe, or manage the positions in your broker app."
        "[/yellow]"
    )


def main(argv: list[str] | None = None) -> int:
    print_banner()
    parsed = parse_args(argv)

    if parsed.mock:
        console.rule("[bold magenta]MOCK MODE — zero credentials")
        from brokers.mock import MockBroker
        from config.settings import Settings as Defaults
        from core.executor import SignalExecutor
        from core.mock import MockSignalInjector

        # Deliberately ignore any local .env: the demo always shows defaults.
        mock_settings = Defaults(_env_file=None)  # type: ignore[call-arg]
        injector = MockSignalInjector()
        executor = SignalExecutor(
            broker=MockBroker(),
            listener=injector.listener,
            settings=mock_settings,
        )
        asyncio.run(injector.run_cycle(mock_settings, executor))
        return 0

    ensure_env_file()
    settings = resolve_config(parsed.onboard, prefer_cli=parsed.cli)

    if not asyncio.run(preflight(settings)):
        console.print("[red]Pre-flight failed. Re-run `python main.py --onboard` or edit .env.[/red]")
        return 1

    asyncio.run(run_loop(settings))
    return 0


def main_guarded(argv: list[str] | None = None) -> NoReturn:
    """Wrapper that routes Ctrl+C through the shutdown guard."""
    code = 0
    try:
        code = main(argv)
    except (KeyboardInterrupt, EOFError):
        console.print("[yellow]Ctrl+C — shutdown guard engaged[/yellow]")
        settings = _settings_or_none()
        if settings is not None:
            asyncio.run(shutdown_guard(settings))
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main_guarded()
