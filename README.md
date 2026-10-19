<div align="center">
<a href="https://railway.com/deploy/railway-template"><img src="https://railway.app/button.svg" alt="Deploy on Railway" height="28"/></a>
&nbsp;&nbsp;
<a href="https://render.com/deploy?repo=https://github.com/okkults/okkults-quant-execution"><img src="https://render.com/images/deploy-to-render-button.svg" alt="Deploy on Render" height="28"/></a>
</div>

# Okkults Quant Terminal

**Automated pre-earnings execution client.** Listens for the 3:50 PM ET
signal from your Okkults account, sizes the 6-12 ticker basket against your
live equity, fires all orders concurrently, and liquidates only the positions
it opened into the 9:30:15 AM opening cross. No resting stops are placed.
Open-source and non-custodial: it executes on **your** broker, with **your**
keys, on **your** machine.

## Install from public GitHub — step by step

Works in the macOS/Linux Terminal, iTerm, or VS Code's terminal
(`Terminal → New Terminal`). A GitHub URL by itself is **not** a command:
you must prefix it with `git clone`.

### 1. Clone the repo

```bash
git clone https://github.com/Okkults/Okkults-Quant-Execution.git
cd Okkults-Quant-Execution
```

### 2. Check Python 3.11

```bash
python3.11 --version
```

If that fails: macOS `brew install python@3.11`; Ubuntu/Debian
`sudo apt install python3.11 python3.11-venv`; Windows install Python 3.11
from python.org and tick **Add Python to PATH**.

### 3. Create the virtual environment and install

macOS/Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Your prompt should now start with `(.venv)`.

### 4. Prove it works with zero keys

```bash
python main.py --mock
```

You should see a simulated 12-ticker batch, 12 `FILLED` rows, and a PnL table.

### 5. Start the real terminal

```bash
python main.py
```

No config file needed — a local onboarding page opens in your browser
(`http://127.0.0.1:<random-port>`, one-time token, never exposed to the
network). You will need a signal-server WebSocket URL and **your personal**
`ok_paper_...` license key from the server operator — setup guide:
[okkults-api-test-v1.00](https://github.com/Okkults/okkults-api-test-v1.00#documentation).

It walks you through, in this order:

1. **Security notice** — the non-custodial architecture, up front
2. **Paper or live** — paper is the default; live requires typing `I UNDERSTAND`
3. **Alpaca API key + secret** — verified against Alpaca before anything is saved
4. **Risk per trade + max total risk at one time** — default 5% / 30%, with
   hard caps of 10% / 30%; oversized values are rejected, not silently accepted
5. **Okkults API key** — masked entry, live-probed against the signal server
6. **Execution mode** — `full_auto` (fires on signal) or `semi_auto` (you press
   Enter; requires an interactive TTY) and the local failsafe toggle
7. **Health check** — real equity fetch, then **SYSTEM ARMED**

Prefer the terminal? `python main.py --onboard --cli` runs the same wizard
step-by-step in the console.

### 6. Later runs

```bash
cd Okkults-Quant-Execution
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
python main.py
```

Everything lands in a `chmod 600 .env` written atomically. Re-runs skip the
wizard when config is valid; `--onboard` forces it again. The client never
places resting stop orders, and it tracks only positions it opens itself —
pre-existing broker holdings are never touched.

### Flags

| Command | Behavior |
| --- | --- |
| `python main.py` | browser onboarding if unconfigured, otherwise armed run loop |
| `python main.py --onboard` | force browser onboarding (re-enter/repair config) |
| `python main.py --onboard --cli` | force the terminal wizard instead of the browser |
| `python main.py --mock` | full simulated cycle, no keys |

## One-line Docker

```bash
docker run -d --restart=always --name okkults-executor \
  -e OKKULTS_LICENSE_KEY="ok_live_..." \
  -e ALPACA_API_KEY="PK..." -e ALPACA_SECRET_KEY="SK..." \
  okkults/execution-client:latest
```

Headless containers run `full_auto` 24/7 in US-East (~$3/month on Railway
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
  logged, echoed, or shipped in telemetry. Browser onboarding binds to
  `127.0.0.1` only and requires a random one-time token.
- **Open source — verify the code.** No backdoors, no key-stealing scripts.
  Grep it yourself: there are zero indicator formulas (ATR, RSI, MACD…) in
  this repo. Prices and exit instructions ride in the wire payload from the
  Okkults server; local code only applies your risk caps — this "Dumb Runner"
  is math-blind by design.

## The three quant rules baked into the design

1. **Sizing IS the stop-loss.** 5% default per ticker, 10% hard cap, 30%
   total exposure ceiling — a single -20% earnings gap costs at most a small
   fraction of equity. A resting stop cannot protect against a gap; allocation
   caps can, so this client places no resting stops.
2. **Law of large numbers.** 6-12 uncorrelated reports per evening; the
   winners' basket overruns the single disaster name.
3. **Exit at the official 9:30 open cross.** Fairest spreads, deepest
   liquidity — no panic after-hours dumps. A `LOCAL_EXIT_FAILSAFE`
   liquidates client-opened positions locally at 9:30:16 AM if the signal
   server ever goes dark.

## Risk configuration defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_PORTFOLIO_RISK_PCT` | 0.30 | total exposure at one time (hard cap 0.30) |
| `MAX_TRADE_ALLOCATION_PCT` | 0.05 | per-ticker target (hard cap 0.10, auto-downscaled) |
| `PER_TRADE_NOTIONAL_CAP` | blank | optional USD cap per order |
| `CASH_RESERVE` | 0.0 | dollars held out of sizing |
| `ALLOW_SHORT_SELLING` | false | SELL signals open shorts only when enabled |
| `ALPACA_IS_PAPER` | true | paper until you explicitly go live |
| `LIVE_TRADING_ACK` | NO | must be `YES` when `ALPACA_IS_PAPER=false` |
| `EXECUTION_MODE` | full_auto | `semi_auto` adds the Enter-key gate (TTY only) |
| `LOCAL_EXIT_FAILSAFE` | true | liquidate client-opened positions at 9:30:16 if needed |

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
pytest -q            # 80 tests: sizing, protocol, executor, terminal + browser wizard
mypy .
ruff check .
```

MIT licensed. Trading involves risk; past performance never guarantees
future results.
