#!/usr/bin/env python3
"""
Binance Futures Scalping Bot
Combines: cryptoprice, futures-alpha-radar, binance-skill-export, trading-signal
Modes: testnet | live
"""

import os, sys, json, time, hmac, hashlib, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scalper")

# ── Config ──────────────────────────────────────────────────────────────────
MODE = os.environ.get("SCALPER_MODE", "testnet")  # testnet | live

if MODE == "live":
    API_KEY = os.environ["BINANCE_API_KEY"]
    API_SECRET = os.environ["BINANCE_SECRET_KEY"]
    BASE_URL = "https://fapi.binance.com"
    PAIRS = ["BTCUSDT"]  # 1 pair only for live
    LEVERAGE = 20
    SIZE_PCT = 0.25  # 25% of balance
    MAX_TRADES_DAY = 5
    TP_PCT = 0.008  # 0.8% TP
    SL_PCT = 0.004  # 0.4% SL
    DAILY_LOSS_LIMIT_PCT = 0.05  # 5% daily loss limit
else:
    API_KEY = os.environ["BINANCE_TESTNET_API_KEY"]
    API_SECRET = os.environ["BINANCE_TESTNET_SECRET_KEY"]
    BASE_URL = "https://testnet.binancefuture.com"
    PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    LEVERAGE = 15
    SIZE_PCT = 0.30
    MAX_TRADES_DAY = 10
    TP_PCT = 0.010  # 1.0% TP
    SL_PCT = 0.005  # 0.5% SL
    DAILY_LOSS_LIMIT_PCT = 0.03

# ── State file ──────────────────────────────────────────────────────────────
STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / f"scalper-{MODE}.json"
TRADES_FILE = STATE_DIR / f"trades-{MODE}.jsonl"
LOG_FILE = STATE_DIR / f"scalper-{MODE}.log"

# Add file handler
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)

# ── API Helpers ─────────────────────────────────────────────────────────────

def signed_request(method, endpoint, params=None):
    """Signed request to Binance Futures API."""
    ts = int(time.time() * 1000)
    query = f"timestamp={ts}"
    if params:
        for k, v in params.items():
            query += f"&{k}={v}"
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=15)
        else:
            resp = requests.post(url, headers=headers, timeout=15)
        data = resp.json()
        if "code" in data and data["code"] < 0:
            log.error(f"API error: {data}")
        return data
    except Exception as e:
        log.error(f"Request failed: {e}")
        return {"code": -9999, "msg": str(e)}

def get_balance():
    """Get futures account balance."""
    d = signed_request("GET", "/fapi/v3/account")
    if "code" in d:
        return 0, 0, []
    total = float(d.get("totalWalletBalance", 0))
    available = float(d.get("availableBalance", 0))
    positions = []
    for p in d.get("positions", []):
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry": float(p["entryPrice"]),
                "pnl": float(p.get("unrealizedProfit", 0)),
                "leverage": int(p.get("leverage", 1)),
            })
    return total, available, positions

def set_leverage(symbol, leverage):
    """Set leverage for symbol."""
    return signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

def get_klines(symbol, interval, limit=100):
    """Get candlestick data."""
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        candles = []
        for k in data:
            candles.append({
                "ts": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
        return candles
    except Exception as e:
        log.error(f"Klines failed: {e}")
        return []

def get_ticker(symbol):
    """Get 24h ticker."""
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    try:
        resp = requests.get(url, params={"symbol": symbol}, timeout=10)
        return resp.json()
    except:
        return {}

def place_order(symbol, side, quantity, order_type="MARKET", price=None, stop_price=None):
    """Place futures order."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT" and price:
        params["price"] = price
        params["timeInForce"] = "GTC"
    if stop_price:
        params["stopPrice"] = stop_price
    return signed_request("POST", "/fapi/v1/order", params)

def close_position(symbol, side, quantity):
    """Close a position."""
    close_side = "SELL" if side == "LONG" else "BUY"
    return signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": quantity,
        "reduceOnly": "true",
    })

def get_smart_money_signals(chain_id="CT_501"):
    """Get smart money signals from Binance Web3."""
    try:
        resp = requests.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            json={"smartSignalType": "", "page": 1, "pageSize": 20, "chainId": chain_id},
            timeout=15,
        )
        data = resp.json()
        if data.get("success"):
            return data.get("data", [])
    except Exception as e:
        log.warning(f"Smart money fetch failed: {e}")
    return []

# ── Indicators ──────────────────────────────────────────────────────────────

def calc_ema(values, period):
    """Calculate EMA."""
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return round(ema, 2)

def calc_rsi(values, period=14):
    """Calculate RSI."""
    if len(values) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_atr(candles, period=14):
    """Calculate ATR."""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 2)

def calc_volume_ratio(candles, period=20):
    """Current volume vs average."""
    if len(candles) < period + 1:
        return 1.0
    avg = sum(c["volume"] for c in candles[-period-1:-1]) / period
    if avg == 0:
        return 1.0
    return round(candles[-1]["volume"] / avg, 2)

# ── State Management ────────────────────────────────────────────────────────

def load_state():
    """Load bot state."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return {
        "trades_today": 0,
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "last_trade_time": None,
        "last_reset_date": None,
        "open_orders": [],
    }

def save_state(state):
    """Save bot state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))

def reset_daily(state):
    """Reset daily counters."""
    today = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today:
        state["trades_today"] = 0
        state["daily_pnl"] = 0.0
        state["last_reset_date"] = today
        log.info(f"Daily reset for {today}")
    return state

def log_trade(trade_data):
    """Log trade to file."""
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(trade_data) + "\n")

# ── Analysis ────────────────────────────────────────────────────────────────

def analyze_15m_trend(candles_15m):
    """Determine 15m trend bias."""
    if len(candles_15m) < 50:
        return "RANGE", "insufficient data"
    
    closes = [c["close"] for c in candles_15m]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    rsi = calc_rsi(closes, 14)
    
    # Trend classification
    if ema9 > ema21 > ema50:
        trend = "UP"
    elif ema9 < ema21 < ema50:
        trend = "DOWN"
    else:
        trend = "RANGE"
    
    reason = f"EMA9={ema9} EMA21={ema21} EMA50={ema50} RSI={rsi}"
    return trend, reason

def analyze_5m_entry(candles_5m, trend_15m):
    """Check 5m entry conditions for scalping."""
    if len(candles_5m) < 30:
        return None
    
    closes = [c["close"] for c in candles_5m]
    current = closes[-1]
    
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    rsi = calc_rsi(closes, 14)
    atr = calc_atr(candles_5m, 14)
    vol_ratio = calc_volume_ratio(candles_5m, 20)
    
    # Previous candle
    prev = candles_5m[-2]
    curr = candles_5m[-1]
    
    signal = None
    
    # LONG conditions
    if trend_15m in ["UP", "RANGE"]:
        if (
            rsi < 35 and  # Oversold
            prev["close"] < prev["open"] and  # Previous red candle
            curr["close"] > curr["open"] and  # Current green (reversal)
            curr["close"] > ema9 and  # Above EMA9
            vol_ratio > 1.3 and  # Volume spike
            ema9 > ema21  # EMA cross up
        ):
            signal = {
                "direction": "LONG",
                "confidence": "C3" if trend_15m == "UP" else "C2",
                "entry": current,
                "rsi": rsi,
                "atr": atr,
                "vol_ratio": vol_ratio,
                "reason": f"5m RSI oversold({rsi}) + reversal candle + EMA9({ema9})>EMA21({ema21}) + vol spike({vol_ratio}x)"
            }
    
    # SHORT conditions
    if trend_15m in ["DOWN", "RANGE"]:
        if (
            rsi > 65 and  # Overbought
            prev["close"] > prev["open"] and  # Previous green
            curr["close"] < curr["open"] and  # Current red (reversal)
            curr["close"] < ema9 and  # Below EMA9
            vol_ratio > 1.3 and  # Volume spike
            ema9 < ema21  # EMA cross down
        ):
            signal = {
                "direction": "SHORT",
                "confidence": "C3" if trend_15m == "DOWN" else "C2",
                "entry": current,
                "rsi": rsi,
                "atr": atr,
                "vol_ratio": vol_ratio,
                "reason": f"5m RSI overbought({rsi}) + reversal candle + EMA9({ema9})<EMA21({ema21}) + vol spike({vol_ratio}x)"
            }
    
    return signal

def check_smart_money_alignment(symbol):
    """Check if smart money aligns with our direction."""
    signals = get_smart_money_signals()
    if not signals:
        return "NEUTRAL", "No smart money data"
    
    base = symbol.replace("USDT", "")
    matching = [s for s in signals if s.get("ticker", "").upper().startswith(base)]
    
    if not matching:
        return "NEUTRAL", f"No {base} signals found"
    
    buys = sum(1 for s in matching if s.get("direction") == "buy")
    sells = sum(1 for s in matching if s.get("direction") == "sell")
    
    if buys > sells * 2:
        return "BULLISH", f"Smart money buying ({buys} buy vs {sells} sell)"
    elif sells > buys * 2:
        return "BEARISH", f"Smart money selling ({sells} sell vs {buys} buy)"
    return "NEUTRAL", f"Mixed signals ({buys} buy vs {sells} sell)"

# ── Main Scalper Logic ─────────────────────────────────────────────────────

def run_scalper():
    """Main scalper execution."""
    log.info(f"{'='*50}")
    log.info(f"Scalper started — MODE: {MODE.upper()}")
    
    state = load_state()
    state = reset_daily(state)
    
    # Check cooldown
    if state.get("cooldown_until"):
        cooldown_time = datetime.fromisoformat(state["cooldown_until"])
        if datetime.now(timezone.utc) < cooldown_time:
            remaining = (cooldown_time - datetime.now(timezone.utc)).seconds // 60
            log.info(f"Cooldown active — {remaining} min remaining")
            save_state(state)
            return {"status": "cooldown", "remaining_min": remaining}
    
    # Get balance
    total_balance, available, open_positions = get_balance()
    if total_balance == 0:
        log.error("Cannot get balance")
        return {"status": "error", "msg": "Cannot get balance"}
    
    log.info(f"Balance: ${total_balance:.2f} total, ${available:.2f} available")
    log.info(f"Open positions: {len(open_positions)}")
    
    # Check daily loss limit
    if state["daily_pnl"] < -(total_balance * DAILY_LOSS_LIMIT_PCT):
        log.warning("Daily loss limit hit!")
        return {"status": "daily_limit", "pnl": state["daily_pnl"]}
    
    # Check max trades
    if state["trades_today"] >= MAX_TRADES_DAY:
        log.info(f"Max trades reached: {state['trades_today']}/{MAX_TRADES_DAY}")
        return {"status": "max_trades"}
    
    # Check consecutive losses
    if state["consecutive_losses"] >= 3:
        cooldown_end = datetime.now(timezone.utc) + timedelta(hours=1)
        state["cooldown_until"] = cooldown_end.isoformat()
        save_state(state)
        log.warning("3 consecutive losses — cooldown 1 hour")
        return {"status": "cooldown_set", "until": cooldown_end.isoformat()}
    
    # Manage existing positions
    results = []
    for pos in open_positions:
        entry = pos["entry"]
        current_pnl_pct = pos["pnl"] / (entry * pos["size"])
        
        # Trailing stop after 0.3% profit
        if current_pnl_pct > 0.003:
            log.info(f"Trailing stop triggered for {pos['symbol']} — PnL: {current_pnl_pct*100:.2f}%")
            # Could implement trailing stop here
            pass
        
        # Timeout check (30 min)
        if pos.get("entry_time"):
            held_time = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["entry_time"])).seconds
            if held_time > 1800:
                log.info(f"Position timeout — closing {pos['symbol']}")
                close_result = close_position(pos["symbol"], pos["side"], pos["size"])
                results.append({"action": "close_timeout", "symbol": pos["symbol"], "result": close_result})
    
    # If max positions reached, skip new entries
    if len(open_positions) >= 2:
        log.info("Max concurrent positions (2) — skipping new entries")
        save_state(state)
        return {"status": "max_positions", "positions": open_positions}
    
    # ── Scan for new entries ──
    for symbol in PAIRS:
        log.info(f"Scanning {symbol}...")
        
        # 1. Get 15m trend
        candles_15m = get_klines(symbol, "15m", 100)
        trend_15m, trend_reason = analyze_15m_trend(candles_15m)
        log.info(f"  15m trend: {trend_15m} ({trend_reason})")
        
        if trend_15m == "RANGE" and MODE == "live":
            log.info(f"  Skipping {symbol} — 15m RANGE in live mode")
            continue
        
        # 2. Get 5m entry signal
        candles_5m = get_klines(symbol, "5m", 50)
        signal = analyze_5m_entry(candles_5m, trend_15m)
        
        if not signal:
            log.info(f"  No entry signal for {symbol}")
            continue
        
        if signal["confidence"] == "C2" and MODE == "live":
            log.info(f"  Skipping {signal['direction']} — C2 confidence in live mode")
            continue
        
        log.info(f"  SIGNAL: {signal['direction']} {symbol} @ ${signal['entry']} ({signal['confidence']})")
        log.info(f"  Reason: {signal['reason']}")
        
        # 3. Smart money check
        sm_dir, sm_reason = check_smart_money_alignment(symbol)
        log.info(f"  Smart money: {sm_dir} ({sm_reason})")
        
        # 4. Calculate position size
        trade_size_usd = available * SIZE_PCT
        price = signal["entry"]
        
        # Get symbol info for quantity precision
        ticker = get_ticker(symbol)
        step_size = 0.001  # default
        min_qty = 0.001
        if "symbols" in str(type(ticker)):
            pass  # use defaults
        
        quantity = round(trade_size_usd * LEVERAGE / price, 3)
        
        # Ensure minimum notional ($5)
        notional = quantity * price
        if notional < 5:
            quantity = round(5 / price, 3)
        
        # 5. Calculate TP/SL
        atr = signal["atr"]
        if signal["direction"] == "LONG":
            sl = round(price * (1 - SL_PCT), 2)
            tp = round(price * (1 + TP_PCT), 2)
        else:
            sl = round(price * (1 + SL_PCT), 2)
            tp = round(price * (1 - TP_PCT), 2)
        
        rr = abs(tp - price) / abs(sl - price)
        
        if rr < 1.5:
            log.info(f"  Skipping — R:R {rr:.1f} too low (min 1.5)")
            continue
        
        # 6. Execute
        log.info(f"  EXECUTING: {signal['direction']} {quantity} {symbol} @ ~${price:.2f}")
        log.info(f"  SL: ${sl} | TP: ${tp} | R:R: {rr:.1f}")
        
        # Set leverage
        set_result = set_leverage(symbol, LEVERAGE)
        log.info(f"  Leverage set: {set_result.get('leverage', '?')}x")
        
        # Place order
        side = "BUY" if signal["direction"] == "LONG" else "SELL"
        order_result = place_order(symbol, side, quantity)
        
        trade_data = {
            "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
            "mode": MODE,
            "symbol": symbol,
            "direction": signal["direction"],
            "confidence": signal["confidence"],
            "entry": price,
            "quantity": quantity,
            "notional": round(notional, 2),
            "leverage": LEVERAGE,
            "sl": sl,
            "tp": tp,
            "rr": round(rr, 2),
            "atr": atr,
            "rsi": signal["rsi"],
            "vol_ratio": signal["vol_ratio"],
            "trend_15m": trend_15m,
            "smart_money": sm_dir,
            "reason": signal["reason"],
            "order_result": order_result,
        }
        
        if "code" in order_result and order_result["code"] < 0:
            trade_data["status"] = "FAILED"
            log.error(f"  Order FAILED: {order_result}")
        else:
            trade_data["status"] = "PLACED"
            trade_data["order_id"] = order_result.get("orderId", "?")
            state["trades_today"] += 1
            state["last_trade_time"] = datetime.now(timezone(timedelta(hours=7))).isoformat()
            log.info(f"  Order PLACED: {order_result.get('orderId', '?')}")
        
        log_trade(trade_data)
        results.append(trade_data)
        
        # Only 1 new trade per cycle
        break
    
    save_state(state)
    return {"status": "done", "results": results}

# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = run_scalper()
    print(json.dumps(result, indent=2, default=str))
