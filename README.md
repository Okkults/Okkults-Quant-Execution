<div align="center">
<a href="https://railway.com/deploy/railway-template"><img src="https://railway.app/button.svg" alt="Deploy on Railway" height="28"/></a>
&nbsp;&nbsp;
<a href="https://render.com/deploy?repo=https://github.com/okkults/okkults-quant-execution"><img src="https://render.com/images/deploy-to-render-button.svg" alt="Deploy on Render" height="28"/></a>
</div>

# Okkults Quant Terminal

**Automated pre-earnings execution client.** Listens for the 3:50 PM EST
signal from your Okkults account, sizes the 6-12 ticker basket against your
live equity, fires all orders concurrently, rests payload-driven disaster
stops server-side, and liquidates everything into the 9:30:15 AM opening
cross. Open-source and non-custodial: it executes on **your** broker, with
**your** keys, on **your** machine.

## Quickstart (local)

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

No config file needed — the terminal onboarding wizard (OpenClaw-style)
launches, walks you through:

1. **Security notice** — the non-custodial architecture, up front
2. **Okkults license** — masked entry, live-probed against the signal server
3. **Broker** — Alpaca (IBKR coming in Phase 2)
4. **Alpaca keys** — paper by default; live requires typing `I UNDERSTAND`
5. **Execution mode** — `full_auto` (fires on signal) or `semi_auto` (you press Enter)
6. **Risk caps** — with a bold-red risk-parity warning above 10%/ticker
7. **Health check** — real equity fetch, then **SYSTEM ARMED**

Everything lands in a `chmod 600 .env` written atomically. Re-runs skip the
wizard when config is valid; `--onboard` forces it again.

### Prove it first — zero credentials

```bash
python main.py --mock
```

Simulates the complete cycle with a fake $50,000 account: 12-ticker batch →
concurrent fire (<200 ms) → server-side disaster stops → 9:30 open-cross exit →
PnL table.

### Flags

| Command | Behavior |
| --- | --- |
| `python main.py` | wizard if unconfigured, otherwise armed run loop |
| `python main.py --onboard` | force the wizard (re-enter/repair config) |
| `python main.py --mock` | full simulated cycle, no keys |

## One-line Docker

```bash
docker run -d --restart=always --name okkults-executor \
  -e OKKULTS_LICENSE_KEY="ok_live_..." \
  -e ALPACA_API_KEY="PK..." -e ALPACA_SECRET_KEY="SK..." \
  okkults/execution-client:latest
```

Heads-less containers run `full_auto` 24/7 in US-East (~$3/month on Railway
or Render — cheaper than an idle laptop missing the entry window).

## How Okkults Executes

> To protect your capital and eliminate broker credential risk, Okkults never
> touches your funds. Run the terminal locally on any Mac/Windows/Linux PC, or
> in the cloud via our 1-click Railway/Render/Docker template.

## Trust

- **Non-custodial by design.** The signal server can tell the terminal *when*
  to trade; it can never move, hold, or withdraw funds. It has no custody, no
  withdrawal path, and never receives your broker credentials.
- **Credentials never leave your machine.** Keys live in a local `.env`
  (or environment variables), are transmitted only to Alpaca, and are never
  logged, echoed, or shipped in telemetry.
- **Open source — verify the code.** No backdoors, no key-stealing scripts.
  Grep it yourself: there are zero indicator formulas (ATR, RSI, MACD…) in
  this repo. All sizing and exit numbers ride in the wire payload from the
  Okkults server — this "Dumb Runner" is math-blind by design.

## The three quant rules baked into the design

1. **Sizing IS the stop-loss.** 3-5-10% allocations, hard portfolio ceiling —
   a single -20% earnings gap costs at most a fraction of a percent of equity.
   A resting stop cannot protect against a gap; allocation caps can.
2. **Law of large numbers.** 6-12 uncorrelated reports per evening; the
   winners' basket overruns the single disaster name.
3. **Exit at the official 9:30 open cross.** Fairest spreads, deepest
   liquidity — no panic after-hours dumps. A `LOCAL_EXIT_FAILSAFE`
   liquidates locally at 9:30:16 AM if the signal server ever goes dark.

## Risk configuration defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_PORTFOLIO_RISK_PCT` | 0.30 | total exposure ceiling across the batch |
| `MAX_TRADE_ALLOCATION_PCT` | 0.10 | per-ticker target (auto-downscaled if needed) |
| `PER_TRADE_NOTIONAL_CAP` | blank | optional USD cap per order |
| `CASH_RESERVE` | 0.0 | dollars held out of sizing |
| `DISASTER_STOP_PCT` | 0.07 | payload-overridable emergency stop |
| `ALPACA_IS_PAPER` | true | paper until you explicitly go live |
| `EXECUTION_MODE` | full_auto | `semi_auto` adds the Enter-key gate |

## Architecture

```
Okkults signal server (math lives here)
        │  BATCH_SIGNAL / EXIT_SIGNAL / CLOSE_POSITION
        ▼
signals/listener.py ── core/executor.py ── core/risk.py
        │                                      │
        │ FILL_CONFIRMATION telemetry          │ sized blueprints
        ▼                                      ▼
     Alpaca  ◄──── brokers/alpaca_client.py ───┘
```

## Tests / verification

```bash
pip install -r requirements-dev.txt
pytest -q            # 53 tests: sizing parity, protocol, executor, wizard
mypy core brokers config signals main.py
ruff check .
```

MIT licensed. Trading involves risk; past performance never guarantees
future results.
