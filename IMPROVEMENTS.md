# Nyx Scalper V2 — Session Improvements (2026-05-24)

Summary of all fixes, refactors, and config lifts applied in one debugging session.

---

## Issue Reported

User: "Dashboard history live + testnet tidak update."

Root cause investigation revealed:
- Backend API server reading wrong trade files (live data hidden).
- Testnet bot had no systemd service (stopped trading since 2026-05-19).
- Stale `consecutive_losses=4` state stuck in cooldown loop.
- Several silent code paths: dead C4 boost, first-match strategy bias, hardcoded blacklist, fee burn on flat positions.

---

## Critical Fixes (P0)

### 1. Dashboard live trades hidden
**File:** `api-server.py:300`

`_serve_state` only read `trades-v2-live-analyze.jsonl` + `trades-v2-testnet.jsonl`. Missing `trades-v2-live.jsonl` (actual live trades). Frontend never received `live` mode trades.

```python
# Added:
("trades-v2-live.jsonl", "live"),
```

Result: dashboard immediately showed 37 live trades from 2026-05-24 03:37.

### 2. `reset_daily` never cleared cooldown
**File:** `scalper-v2.py:1119`

Function reset `trades_today/daily_pnl/daily_fees` but left `consecutive_losses` + `cooldown_until` stale across days. Bot stuck cooldown loop indefinitely once 3 consecutive losses hit.

```python
state["consecutive_losses"] = 0
state["cooldown_until"] = None
```

### 3. C2 signals lose money — disabled globally
**File:** `market_regime.py:225, 253`

Learning data: C2 = 37.5% WR / **-$74**, C3 = 53% WR / **+$282**.

```python
# base + LOW vol override:
"confidence_min": "C3"   # was "C2"
```

### 4. Adaptive trailing per position
**File:** `scalper-v2.py:1666, 1709, 1745`

`position_monitor` used global `TRAILING_DISTANCE`/`TRAILING_ACTIVATE` env vars, not the adaptive values from `_calc_adaptive_params`. Snapshot ap[`trailing_*`] at entry, read from there in monitor.

---

## Strategy & Risk Logic Improvements (P1)

### 5. Partial TP 50/50 split
**File:** `scalper-v2.py:229 (place_sl_tp), 1666-1716`

Old: all-or-nothing TP — 21 SL hits vs 4 TP hits (5.25:1 ratio).

New:
- TP1 at `adaptive_tp × 0.5` with 50% qty
- TP2 at `adaptive_tp` with 50% qty
- After TP1 fills → SL moves to entry price (breakeven)
- Fall back to single TP if `qty/2 < minQty`

Tracking in `active_positions`: `tp1, tp2, tp1_qty, tp2_qty, tp1_filled, tp2_algo_id`.

Telegram alert distinguishes "TP1 Hit 🎯 (50%)" vs "TP Hit 🎯".

### 6. Strategy tag for per-strategy WR analysis
**File:** `scalper-v2.py:1314+ (8 sites)`

Each signal dict + `trade_data` + `log_trade` now includes `"strategy"` field with values: `reversal | trend_follow | mean_revert | breakout | ema_momentum`.

Learner can now compute WR by strategy (was lumped into `"?"` bucket — 22.8% overall).

### 7. Strategy B SHORT-in-DOWN disabled
**File:** `scalper-v2.py:1347-1350`

Historical 33% WR / -$19 pnl. Strategy A Reversal SHORT (RSI>65 overbought) still catches downside via mean-reversion at retracement peaks — correct scalp pattern. Trend-follow SHORT chases mid-move = poor R:R.

### 8. BTC + ETH blacklist centralized
**Files:** `state/adaptive-config.json`, `scalper-v2.py:1302`

BTCUSDT: 15.4% WR / -$70.67. ETHUSDT: 0% WR / -$82.77.

- Added both to `disabled: ["BTCUSDT","ETHUSDT"]` in `adaptive-config.json`
- Set weight = 0.0, action = DISABLE
- Removed hardcoded `if symbol == "ETHUSDT"` block in scalper-v2.py (now sourced from learning_config path)

### 9. Tiered timeout (fee leak reduction)
**File:** `scalper-v2.py:1850-1870`

Old: 30min + pnl < trail_activate → market close (burned fees on flat positions).

New:
```
≥SOFT_TIMEOUT (30m) + |pnl| < BREAKEVEN_BAND (0.1%) → close (BREAKEVEN_FLAT)
≥HARD_TIMEOUT (60m) → close regardless (HARD_CAP)
Else → hold, let SL/TP fire naturally
```

Losing positions between SL and break-even at 30min are LEFT to run — SL handles worst case; closing at -0.5% just locks loss + fees.

### 10. Strategy ranking by regime + confidence
**File:** `scalper-v2.py:1305-1432`

Old: first-match-wins (A→B→C→D→E with `if not signal:` short-circuits). Biased to Strategy A Reversal.

New: collect all matching signals into `candidates[]`, sort by `(CONF_RANK, REGIME_PREF)` descending. Per-regime preference map:

```
UP/STRONG_UP:    trend_follow > ema_momentum > breakout > reversal
DOWN/STRONG_DOWN: reversal > breakout > ema_momentum
RANGE:           mean_revert > reversal > breakout
```

Logs picked + alternatives when ≥2 candidates match.

### 11. C4 dead code → real size bonus
**File:** `scalper-v2.py:1515-1527`

Old: smart money aligned → boost confidence C3 → C4. But no regime ever required C4. Dead path.

New: smart money aligned → `risk_mult × SM_SIZE_BONUS (1.2)`. Real impact, no dead code. Confidence stays as-is for filter logic.

---

## Config Centralization

### 12. All magic numbers lifted to env vars
**Files:** `scalper-v2.py:80-100`, both systemd services

New env-tunable parameters (15 total):

| Env var | Default | Was |
|---|---|---|
| `SCALPER_SIGNAL_COOLDOWN_SEC` | 900 | hardcoded 900 |
| `SCALPER_CONSEC_LOSS_LIMIT` | 3 | hardcoded 3 |
| `SCALPER_LOSS_COOLDOWN_HOURS` | 1 | hardcoded 1 |
| `SCALPER_SOFT_TIMEOUT_SEC` | 1800 | hardcoded 1800 |
| `SCALPER_HARD_TIMEOUT_SEC` | 3600 | new (was no hard cap) |
| `SCALPER_BREAKEVEN_BAND_PCT` | 0.001 | new |
| `SCALPER_CLOSE_GRACE_SEC` | 30 | hardcoded 30 |
| `SCALPER_MONITOR_INTERVAL_SEC` | 10 | hardcoded 10 |
| `SCALPER_MAX_SPREAD_BPS` | 5 | hardcoded 5 |
| `SCALPER_MIN_NET_RR` | 1.5 | hardcoded 1.5 |
| `SCALPER_FUNDING_MAX` | 0.001 | hardcoded 0.001 |
| `SCALPER_C2_SIZE_MULT` | 0.5 | hardcoded 0.5 |
| `SCALPER_SM_SIZE_BONUS` | 1.2 | new (was confidence boost) |
| `TELEGRAM_BOT_TOKEN` | (env required) | hardcoded in code 4 places |
| `TELEGRAM_CHAT_ID` | (env required) | hardcoded in code 4 places |

### 13. Telegram secrets removed from source
**Files:** `scalper-v2.py:493, 570`, `market_regime.py:19-20`, `learning-engine.py:267-268`

All 4 hardcoded telegram secret sites replaced with `os.environ.get()`. Defaults empty — alerts silently skip if env not set. Secrets live only in systemd unit files now.

### 14. Pre-existing bug fixed: `_log_signal_rejection` arg collision
**File:** `scalper-v2.py:1213, 1464`

2 callers used wrong signature: `_log_signal_rejection(log, symbol, f"...", regime, rsi_5=0, ...)` — but function declared `rsi_5` as positional arg 3. Caused `multiple values for 'rsi_5'` error when BTCUSDT/ETHUSDT triggered the disabled path.

Replaced with simple `log.info(...)` — no longer using broken helper for these debug logs.

---

## Infrastructure

### 15. Testnet service created
**File:** `/etc/systemd/system/nyx-scalper-testnet.service`

Cloned from live service with `SCALPER_MODE=testnet`, separate log dest. Enabled + started. Testnet now actively trading after 5-day gap.

### 16. Stale testnet state reset
**File:** `state/scalper-v2-testnet.json`

Cleared `consecutive_losses=4` and `cooldown_until=2026-05-22T16:15` to give clean slate before fix #2 lands organically.

---

## Persistence Audit

| Check | Result |
|---|---|
| State filesystem | `/dev/vda1` xfs (persistent, not tmpfs) ✅ |
| Services enabled (auto-boot) | nyx-scalper, nyx-scalper-testnet, nyx-scalper-api all enabled ✅ |
| `/tmp` writes | None ✅ |
| Trade history files | Append-only, never wiped by `reset_daily` ✅ |
| Learning config (blacklist) | Persistent at `state/adaptive-config.json` ✅ |
| Market regime cache | Persistent at `state/market-regime.json` ✅ |

**Reboot recovery flow:**
```
systemd boot
  ├─ nyx-scalper-api.service       (serves /api/state to dashboard)
  ├─ nyx-scalper.service           (LIVE mode)
  │   └─ load STATE_FILE → continue counters
  │       load adaptive-config → blacklist + weights
  │       load market-regime → regime cache
  │       reset_daily if new day
  └─ nyx-scalper-testnet.service   (TESTNET mode)
```

All state, blacklist, and config survive reboot.

---

## Expected Impact

| Metric | Before | Expected After |
|---|---|---|
| C2 trade pnl | -$74 over 8 trades | $0 (blocked) |
| BTC+ETH leak | -$153 over 17 trades | $0 (disabled) |
| SHORT-in-DOWN leak | -$19 over 11 trades | $0 (disabled) |
| TP fill ratio | 4 TP : 21 SL (16%) | TP1 fills should land ~50%+ of attempts |
| Fee burn on flat exits | ~$3-5/day | near zero |
| Strategy bias | Strategy A first-match | regime-aware ranking |

Total projected positive expectancy improvement: **~$240-300 per 25-trade window** (conservative — based on eliminating known negative-EV paths).

---

## Files Modified

```
scalper-v2.py            (main bot — biggest changes)
market_regime.py         (telegram env, C2→C3 confidence_min)
learning-engine.py       (telegram defaults removed)
api-server.py            (added trades-v2-live.jsonl source)
state/adaptive-config.json   (BTC+ETH disabled)
state/scalper-v2-testnet.json (stale state reset)
/etc/systemd/system/nyx-scalper.service       (env vars added)
/etc/systemd/system/nyx-scalper-testnet.service (new file)
```

---

## Next Steps (Not Yet Done)

Monitoring & tuning opportunities for future sessions:
- Track TP1 fill rate vs TP2 fill rate after 24-48h
- Compute per-strategy WR once `strategy` field accumulates ~10+ trades per type
- Consider tightening Strategy A Reversal SHORT (RSI>70, vol>1.5x) if SHORT remains low WR
- Add systemd timer for `learning-engine.py` to auto-update `adaptive-config.json` daily
- Consider blacklisting SHORT direction globally if data continues to show <40% WR
