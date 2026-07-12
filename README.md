# Nyx Scalper

Binance USDT-M Futures scalping bot with WebSocket real-time data, multi-strategy signal detection, adaptive market regime engine, and ML-based learning loop.

> ⚠️ **Disclaimer:** Trading futures with leverage carries significant risk. This software is for educational purposes. Backtest thoroughly on testnet first. You can lose money. Not financial advice.

---

## Features

- **WebSocket real-time** — kline streams (5m + 15m), bookTicker, depth, user data stream
- **5 strategy engine** — reversal, trend-follow, mean-revert, breakout, EMA momentum
- **Adaptive market regime** — per-symbol volatility + trend detection, auto-adjusts entry filters
- **Learning engine** — nightly job analyzes trade history, outputs adaptive config (per-pair weights, blacklists, confidence floors)
- **Partial TP 50/50** — TP1 at half target (moves SL to breakeven), TP2 at full target
- **Risk management** — daily loss limit circuit breaker, consecutive loss cooldown, trailing stops, tiered position timeout (soft/hard)
- **Telegram alerts** — entry, TP, SL, close notifications
- **Dashboard API** — lightweight HTTP server for external dashboard integration
- **Testnet + Live** — hybrid mode: live market data for signals, testnet or live for execution

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   scalper-v2.py                  │
│                                                   │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────┐ │
│  │  WS Streams  │→ │ Signal Engine │→ │ Executor │ │
│  │ (kline,depth │  │ (5 strategies │  │ (orders, │ │
│  │  bookTicker, │  │  + regime     │  │  TP/SL,  │ │
│  │  user data)  │  │  + confidence)│  │  trail)  │ │
│  └─────────────┘  └──────────────┘  └─────────┘ │
│         ↑                                    ↓   │
│  ┌──────┴──────┐                    ┌──────────┐│
│  │market_regime │                    │api-server ││
│  │   .py        │                    │   .py     ││
│  │(vol + trend) │                    │ (port     ││
│  └─────────────┘                    │  3778)    ││
│                                      └──────────┘│
│  ┌──────────────────────────────────┐            │
│  │       learning-engine.py         │            │
│  │  (nightly: WR per pair/strategy, │            │
│  │   outputs adaptive-config.json)  │            │
│  └──────────────────────────────────┘            │
└─────────────────────────────────────────────────┘
```

### Files

| File | Purpose |
|---|---|
| `scalper-v2.py` | Main bot — WebSocket streams, signal detection, order execution |
| `market_regime.py` | Per-symbol volatility + trend regime detection |
| `learning-engine.py` | Nightly trade history analysis → adaptive config |
| `learner.py` | Lightweight learner module |
| `api-server.py` | HTTP API server for dashboard (port 3778) |
| `close-all.py` | Emergency close all positions |
| `daily-report.py` | Daily PnL summary report |
| `testnet-entry.py` | Testnet-specific entry testing |
| `test-connectivity.py` | API + WebSocket connectivity test |

---

## Quick Start

### Prerequisites

- Python 3.10+
- Binance account with Futures enabled
- API keys (testnet or live)

### Install

```bash
git clone https://github.com/nicogulo/nyx-scalper.git
cd nyx-scalper

python3 -m venv venv
source venv/bin/activate
pip install requests websockets
```

### Configure

```bash
cp .env.example .env.scalper
```

Edit `.env.scalper`:

```bash
export BINANCE_TESTNET_API_KEY=your_testnet_key
export BINANCE_TESTNET_SECRET_KEY=your_testnet_secret
export BINANCE_API_KEY=your_live_key
export BINANCE_SECRET_KEY=your_live_secret
export TELEGRAM_BOT_TOKEN=your_bot_token    # optional
export TELEGRAM_CHAT_ID=your_chat_id         # optional
```

### Run

**Testnet (recommended first):**

```bash
source .env.scalper
python3 scalper-v2.py
```

Or via the start script:

```bash
chmod +x start-v2.sh
./start-v2.sh testnet
```

**Live:**

```bash
export SCALPER_MODE=live
python3 scalper-v2.py
```

---

## Configuration

All parameters configurable via environment variables. Defaults are in `scalper-v2.py`.

| Env Var | Default | Description |
|---|---|---|
| `SCALPER_MODE` | `testnet` | `testnet` or `live` |
| `SCALPER_DRY_RUN` | `false` | Detect signals only, no execution |
| `SCALPER_PAIRS` | `BTCUSDT,SOLUSDT,...` | Comma-separated, max 10, must end with USDT |
| `SCALPER_LEVERAGE` | `15` | Leverage multiplier |
| `SCALPER_SIZE_PCT` | `0.30` | Position size as % of available balance |
| `SCALPER_MAX_TRADES_DAY` | `10` | Max trades per day |
| `SCALPER_MAX_GLOBAL_POSITIONS` | `1` | Max concurrent open positions |
| `SCALPER_TP_PCT` | `0.008` | Take profit % |
| `SCALPER_SL_PCT` | `0.008` | Stop loss % |
| `SCALPER_DAILY_LOSS_LIMIT_PCT` | `0.03` | Daily loss circuit breaker (-3%) |
| `SCALPER_TRAILING_ACTIVATE` | `0.003` | Profit % to activate trailing |
| `SCALPER_TRAILING_DISTANCE` | `0.002` | Trailing stop distance |
| `SCALPER_CONSEC_LOSS_LIMIT` | `3` | Consecutive losses before cooldown |
| `SCALPER_LOSS_COOLDOWN_HOURS` | `1` | Cooldown duration after loss limit |
| `SCALPER_SOFT_TIMEOUT_SEC` | `1800` | Soft position timeout (30 min) |
| `SCALPER_HARD_TIMEOUT_SEC` | `3600` | Hard position timeout (60 min) |
| `SCALPER_MIN_NET_RR` | `1.5` | Minimum risk-reward ratio |
| `SCALPER_MAX_SPREAD_BPS` | `5` | Max spread in basis points |
| `SCALPER_FUNDING_MAX` | `0.001` | Max funding rate threshold |

Full list in `.env.example`.

---

## Strategies

| Strategy | Type | Description |
|---|---|---|
| **Reversal** | Mean-reversion | RSI overbought/oversold + volume spike + wick rejection |
| **Trend Follow** | Trend | EMA alignment + pullback entry in trend direction |
| **Mean Revert** | Range | Bollinger Band bounce in ranging markets |
| **Breakout** | Momentum | ATR breakout + volume confirmation |
| **EMA Momentum** | Trend | EMA9/EMA21 cross + RSI momentum |

Signal engine collects all matching signals, ranks by confidence level + regime preference, picks best candidate. No first-match bias.

### Confidence Levels

- **C1** — lowest confidence, highest risk
- **C2** — below average
- **C3** — average confidence (default minimum)
- **C4** — above average
- **C5** — highest confidence

### Market Regimes

| Regime | Description | Preferred Strategies |
|---|---|---|
| `STRONG_UP` | Strong uptrend | trend_follow > ema_momentum > breakout |
| `UP` | Mild uptrend | trend_follow > ema_momentum |
| `RANGE` | Sideways | mean_revert > reversal > breakout |
| `DOWN` | Mild downtrend | reversal > breakout |
| `STRONG_DOWN` | Strong downtrend | reversal > breakout |

---

## API Server

`api-server.py` serves state + live data for external dashboards.

```bash
python3 api-server.py
# Serves on http://localhost:3778
```

Endpoints:

| Route | Description |
|---|---|
| `/api/state` | Bot state, trades, positions |
| `/api/config` | Current bot configuration |
| `/api/balance` | Live account balance |
| `/api/positions` | Open positions |

---

## Learning Engine

Run nightly (recommended via cron):

```bash
python3 learning-engine.py
```

Analyzes trade history and outputs `state/adaptive-config.json` with:
- Per-pair win rate + PnL
- Per-strategy win rate
- Auto-blacklist underperforming pairs
- Adaptive confidence floors
- Size multipliers

---

## Deployment (systemd)

Example service file for production:

```ini
[Unit]
Description=Nyx Scalper Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/nyx-scalper
EnvironmentFile=/path/to/.env.scalper
ExecStart=/path/to/venv/bin/python3 scalper-v2.py
Restart=always
RestartSec=15
ExecStartPre=/bin/sleep 10

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp nyx-scalper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nyx-scalper
sudo systemctl start nyx-scalper
```

---

## Tech Stack

- **Python 3** — no heavy frameworks, pure stdlib + `requests` + `websockets`
- **Binance Futures API** — REST + WebSocket (new 2026 URL structure)
- **No database** — JSON/JSONL files for state and trade history

---

## License

MIT — see [LICENSE](LICENSE).

---

## Contributing

PRs welcome. For major changes, open an issue first.

---

> Built by [Nico](https://github.com/nicogulo). Use at your own risk.
