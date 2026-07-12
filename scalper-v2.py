#!/usr/bin/env python3
"""
Binance Futures Scalper V2 — WebSocket Real-Time
Resources used:
  - WebSocket: kline streams (5m, 15m), bookTicker, depth, user data stream
  - REST: account balance, order placement, leverage, listenKey management
  - Smart Money API: on-chain signal overlay
  - All 5 Binance skills combined

Modes: testnet | live
"""

import os, sys, json, time, math, hmac, hashlib, asyncio, logging, signal
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import websockets

# Market regime engine
from market_regime import update_regime, get_regime, get_params, get_all_regimes, init_regimes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scalper-v2")

VENV_PYTHON = "/root/.openclaw/workspace/frontend/polymarket-trader/venv/bin/python3"

# ── Config ──────────────────────────────────────────────────────────────────
MODE = os.environ.get("SCALPER_MODE", "testnet")
# DRY_RUN=True → detect signals & alert, but NO trade execution
DRY_RUN = os.environ.get("SCALPER_DRY_RUN", "false").lower() == "true"

# ── Shared Config (from env with sensible defaults) ─────────────────
# Pairs: comma-separated in SCALPER_PAIRS env var, or default list
_default_pairs = "BTCUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,SUIUSDT,ZECUSDT,AVAXUSDT,LINKUSDT,ADAUSDT"
PAIRS = [p.strip().upper() for p in os.environ.get("SCALPER_PAIRS", _default_pairs).split(",") if p.strip()]
assert len(PAIRS) <= 10, f"Max 10 pairs for analysis, got {len(PAIRS)}"
assert all(p.endswith("USDT") for p in PAIRS), f"All pairs must end with USDT: {PAIRS}"

LEVERAGE = int(os.environ.get("SCALPER_LEVERAGE", "15"))
SIZE_PCT = float(os.environ.get("SCALPER_SIZE_PCT", "0.30"))
MAX_TRADES_DAY = int(os.environ.get("SCALPER_MAX_TRADES_DAY", "10"))
TP_PCT = float(os.environ.get("SCALPER_TP_PCT", "0.008"))
SL_PCT = float(os.environ.get("SCALPER_SL_PCT", "0.008"))
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("SCALPER_DAILY_LOSS_LIMIT_PCT", "0.03"))
TRAILING_ACTIVATE = float(os.environ.get("SCALPER_TRAILING_ACTIVATE", "0.003"))
TRAILING_DISTANCE = float(os.environ.get("SCALPER_TRAILING_DISTANCE", "0.002"))

# ── Mode-specific: only API endpoints differ ────────────────────────────
# Hybrid architecture: LIVE market data + mode-specific execution
# This fixes testnet volume being artificial/garbage — signal detection
# uses real volume from live, while orders go to testnet or live.
LIVE_REST = "https://fapi.binance.com"
LIVE_WS_BASE = "wss://fstream.binance.com"
LIVE_WS_PUBLIC = f"{LIVE_WS_BASE}/public/ws"
LIVE_WS_MARKET = f"{LIVE_WS_BASE}/market/ws"

if MODE == "live":
    API_KEY = os.environ["BINANCE_API_KEY"]
    API_SECRET = os.environ["BINANCE_SECRET_KEY"]
    REST_BASE = "https://fapi.binance.com"
    WS_BASE = "wss://fstream.binance.com"
    WS_PUBLIC = f"{WS_BASE}/public/ws"
    WS_MARKET = f"{WS_BASE}/market/ws"
    WS_PRIVATE = f"{WS_BASE}/private/ws"
else:
    API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    API_SECRET = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
    # Execution: testnet endpoints (orders, account, user stream)
    REST_BASE = "https://demo-fapi.binance.com"
    WS_BASE = "wss://fstream.binancefuture.com"
    WS_PUBLIC = f"{WS_BASE}/public/ws"
    WS_MARKET = f"{WS_BASE}/market/ws"
    WS_PRIVATE = f"{WS_BASE}/private/ws"

# Market data override: ALWAYS use live endpoints for candles/volume/OI/funding
# Testnet volume is artificial and breaks vol_ratio calculations.
DATA_REST = LIVE_REST        # REST for klines, funding, OI
DATA_WS_PUBLIC = LIVE_WS_PUBLIC   # WS for bookticker, depth
DATA_WS_MARKET = LIVE_WS_MARKET   # WS for kline streams

# ── Fee Config ──────────────────────────────────────────────────────────────
TAKER_FEE = float(os.environ.get("SCALPER_TAKER_FEE", "0.0005"))   # 0.05% per trade
TOTAL_FEE_PCT = TAKER_FEE * 2  # 0.10% of notional per round-trip
MIN_NET_TP_MARGIN_PCT = float(os.environ.get("SCALPER_MIN_NET_TP_PCT", "0.01"))  # 1% of margin minimum (scalping: smaller targets)

# ── Risk & Timing (env-tunable) ─────────────────────────────────────────────
SIGNAL_COOLDOWN_SEC = int(os.environ.get("SCALPER_SIGNAL_COOLDOWN_SEC", "900"))      # 15min between same-symbol signals
CONSEC_LOSS_LIMIT = int(os.environ.get("SCALPER_CONSEC_LOSS_LIMIT", "3"))            # losses before cooldown
LOSS_COOLDOWN_HOURS = float(os.environ.get("SCALPER_LOSS_COOLDOWN_HOURS", "1"))      # cooldown after CONSEC_LOSS_LIMIT
SOFT_TIMEOUT_SEC = int(os.environ.get("SCALPER_SOFT_TIMEOUT_SEC", "1800"))           # 30min: close if flat
HARD_TIMEOUT_SEC = int(os.environ.get("SCALPER_HARD_TIMEOUT_SEC", "3600"))           # 60min: hard cap
BREAKEVEN_BAND_PCT = float(os.environ.get("SCALPER_BREAKEVEN_BAND_PCT", "0.001"))    # 0.1% pnl band → "flat"
CLOSE_GRACE_SEC = int(os.environ.get("SCALPER_CLOSE_GRACE_SEC", "30"))               # grace buffer for close matching
MONITOR_INTERVAL_SEC = int(os.environ.get("SCALPER_MONITOR_INTERVAL_SEC", "10"))     # position monitor poll
MAX_SPREAD_BPS = float(os.environ.get("SCALPER_MAX_SPREAD_BPS", "5"))                # max spread to enter (bps)
MIN_NET_RR = float(os.environ.get("SCALPER_MIN_NET_RR", "0.8"))                       # min net R:R after fees (scalping: relaxed)
FUNDING_MAX = float(os.environ.get("SCALPER_FUNDING_MAX", "0.001"))                  # skip if abs funding > this
C2_SIZE_MULT = float(os.environ.get("SCALPER_C2_SIZE_MULT", "0.5"))                  # C2 signals get half size
SM_SIZE_BONUS = float(os.environ.get("SCALPER_SM_SIZE_BONUS", "1.2"))                # smart money aligned bonus

# ── Notifications (env-only, no defaults — required for alerts) ────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
if not TELEGRAM_BOT_TOKEN:
    log.warning("TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled")

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / f"scalper-v2-{MODE}.json"
TRADES_FILE = STATE_DIR / f"trades-v2-{MODE}.jsonl"
EVENT_LOG_FILE = STATE_DIR / f"events-v2-{MODE}.jsonl"

# ── In-memory state ─────────────────────────────────────────────────────────
candle_buffer = {}  # {symbol_interval: [candles...]}
bookticker = {}     # {symbol: {bid, ask, bidQty, askQty}}
depth_cache = {}    # {symbol: {bids: [], asks: []}}
active_positions = {}  # {symbol: {side, entry, size, sl, tp, sl_algo_id, tp_algo_id, highest_pnl_pct, entry_time}}
recently_closed = {}   # {symbol: {ap_data, close_ts}} — grace period buffer for race condition
signal_cooldown = {}   # {symbol: timestamp} — prevent duplicate signals
_learning_config_cache = {"data": None, "mtime": 0}


def load_learning_config():
    """Load adaptive config from learning engine, with file-based cache."""
    global _learning_config_cache
    cfg_path = STATE_DIR / "adaptive-config.json"
    if not cfg_path.exists():
        return None
    try:
        mtime = cfg_path.stat().st_mtime
        if _learning_config_cache["data"] and _learning_config_cache["mtime"] == mtime:
            return _learning_config_cache["data"]
        with open(cfg_path) as f:
            cfg = json.load(f)
        _learning_config_cache["data"] = cfg
        _learning_config_cache["mtime"] = mtime
        log.info(f"📚 Loaded learning config v{cfg.get('version','?')} — {len(cfg.get('pairs',{}))} pairs, {len(cfg.get('disabled',[]))} disabled")
        return cfg
    except Exception as e:
        log.warning(f"Failed to load learning config: {e}")
        return None


CLOSE_GRACE_SECONDS = CLOSE_GRACE_SEC  # backward compat alias

# ── REST API ────────────────────────────────────────────────────────────────

def signed_request(method, endpoint, params=None):
    ts = int(time.time() * 1000)
    query = f"timestamp={ts}&recvWindow=10000"
    if params:
        for k, v in params.items():
            query += f"&{k}={v}"
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    # Retry with exponential backoff for 503/-1008
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=15)
            elif method == "DELETE":
                r = requests.delete(url, headers=headers, timeout=15)
            else:
                r = requests.post(url, headers=headers, timeout=15)

            data = r.json()

            # Handle rate limits
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                log.warning(f"Rate limited (429) — waiting {wait}s")
                time.sleep(wait)
                continue

            # Handle server overload — retry
            if isinstance(data, dict) and data.get("code") in [-1008, -1001]:
                if attempt < max_retries - 1:
                    wait = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s
                    log.warning(f"Server overload ({data.get('code')}) — retry {attempt+1}/{max_retries} in {wait}s")
                    time.sleep(wait)
                    continue

            # HTTP 503 special handling
            if r.status_code == 503:
                if attempt < max_retries - 1:
                    wait = 0.5 * (2 ** attempt)
                    log.warning(f"HTTP 503 — retry {attempt+1}/{max_retries} in {wait}s")
                    time.sleep(wait)
                    continue

            return data

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                log.warning(f"Request timeout — retry {attempt+1}/{max_retries} in {wait}s")
                time.sleep(wait)
                continue
            return {"code": -9999, "msg": "Timeout after retries"}
        except Exception as e:
            log.error(f"REST error: {e}")
            return {"code": -9999, "msg": str(e)}

    return data

def get_balance():
    d = signed_request("GET", "/fapi/v3/account")
    if "code" in d and d["code"] < 0:
        return 0, 0
    return float(d.get("totalWalletBalance", 0)), float(d.get("availableBalance", 0))

def get_positions():
    """Get current open positions via positionRisk endpoint."""
    d = signed_request("GET", "/fapi/v3/positionRisk")
    if isinstance(d, dict) and "code" in d and d["code"] < 0:
        # Fallback to account endpoint
        d2 = signed_request("GET", "/fapi/v3/account")
        if "positions" not in d2:
            return []
        return [
            {"symbol": p["symbol"], "side": "LONG" if float(p["positionAmt"]) > 0 else "SHORT",
             "size": abs(float(p["positionAmt"])), "entry": float(p.get("entryPrice", 0)),
             "pnl": float(p.get("unrealizedProfit", 0)), "leverage": int(p.get("leverage", 1))}
            for p in d2.get("positions", []) if float(p.get("positionAmt", 0)) != 0
        ]
    if not isinstance(d, list):
        return []
    return [
        {"symbol": p["symbol"], "side": "LONG" if float(p["positionAmt"]) > 0 else "SHORT",
         "size": abs(float(p["positionAmt"])), "entry": float(p.get("entryPrice", 0)),
         "pnl": float(p.get("unRealizedProfit", 0)), "leverage": int(p.get("leverage", 1))}
        for p in d if float(p.get("positionAmt", 0)) != 0
    ]

def set_leverage(symbol, lev):
    return signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

def place_order(symbol, side, qty, order_type="MARKET", price=None, reduce_only=False):
    params = {"symbol": symbol, "side": side, "type": order_type, "quantity": qty,
              "newOrderRespType": "RESULT"}
    if order_type == "LIMIT" and price:
        params["price"] = price
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    return signed_request("POST", "/fapi/v1/order", params)

def place_sl_tp(symbol, side, qty, sl_price, tp_price, tp_qty=None):
    """Place stop-loss and take-profit via new Algo Order API.

    If tp_qty given and != qty, only places TP for tp_qty (rest left for TP2 caller).
    """
    close_side = "SELL" if side == "LONG" else "BUY"
    results = {}
    # Stop-loss (full qty)
    if sl_price:
        sl_params = {
            "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "triggerPrice": sl_price, "quantity": qty,
            "workingType": "CONTRACT_PRICE", "priceProtect": "true",
            "algoType": "CONDITIONAL",
        }
        results["sl"] = signed_request("POST", "/fapi/v1/algoOrder", sl_params)
    # Take-profit
    if tp_price:
        tp_params = {
            "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": tp_price, "quantity": tp_qty if tp_qty is not None else qty,
            "workingType": "CONTRACT_PRICE", "priceProtect": "true",
            "algoType": "CONDITIONAL",
        }
        results["tp"] = signed_request("POST", "/fapi/v1/algoOrder", tp_params)
    return results


def place_partial_tp(symbol, side, tp_price, tp_qty):
    """Place a standalone TP order for partial qty (used for TP2 in 50/50 split)."""
    close_side = "SELL" if side == "LONG" else "BUY"
    tp_params = {
        "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
        "triggerPrice": tp_price, "quantity": tp_qty,
        "workingType": "CONTRACT_PRICE", "priceProtect": "true",
        "algoType": "CONDITIONAL",
    }
    return signed_request("POST", "/fapi/v1/algoOrder", tp_params)

def cancel_open_orders(symbol):
    """Cancel all open regular orders for symbol."""
    return signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

def cancel_algo_orders(symbol):
    """Cancel all open algo (conditional) orders for a symbol."""
    # Query open algo orders
    d = signed_request("GET", "/fapi/v1/openAlgoOrders", {
        "symbol": symbol,
    })
    cancelled = []
    orders = d if isinstance(d, list) else d.get("orders", d.get("data", []))
    for o in orders:
        algo_id = o.get("algoId")
        if algo_id and o.get("algoStatus") == "NEW":
            res = signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
            cancelled.append(res)
            log.info(f"Cancelled algo order {algo_id}: {res}")
    return cancelled

def cancel_all_for_symbol(symbol):
    """Cancel ALL orders (regular + algo) for a symbol. Use for cleanup."""
    r1 = cancel_open_orders(symbol)
    r2 = cancel_algo_orders(symbol)
    return {"regular": r1, "algo": r2}

def create_listen_key():
    """Create user data stream listen key."""
    return signed_request("POST", "/fapi/v1/listenKey")

def keepalive_listen_key():
    """Keep listen key alive."""
    return signed_request("PUT", "/fapi/v1/listenKey")

def emergency_kill_all():
    """EMERGENCY: Close all positions and cancel all orders."""
    log.error("🚨 EMERGENCY KILL — closing all positions!")
    positions = get_positions()
    for pos in positions:
        sym = pos["symbol"]
        side = pos["side"]
        size = pos["size"]
        close_side = "SELL" if side == "LONG" else "BUY"
        cancel_all_for_symbol(sym)
        result = place_order(sym, close_side, size, reduce_only=True)
        log.error(f"🚨 Emergency close {sym}: {result}")
    log.error("🚨 All positions closed")

def set_countdown_cancel(symbol, countdown_ms=60000):
    """Set countdown auto-cancel as safety net (heartbeat)."""
    return signed_request("POST", "/fapi/v1/countdownCancelAll", {
        "symbol": symbol, "countdownTime": str(countdown_ms),
    })

def get_exchange_info(symbol):
    """Get symbol trading rules."""
    try:
        r = requests.get(f"{REST_BASE}/fapi/v1/exchangeInfo", timeout=15)
        data = r.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                max_qty = float(filters.get("LOT_SIZE", {}).get("maxQty", 1000000))
                return {
                    "pricePrecision": s.get("pricePrecision", 2),
                    "quantityPrecision": s.get("quantityPrecision", 3),
                    "minQty": float(filters.get("LOT_SIZE", {}).get("minQty", 0.001)),
                    "maxQty": max_qty,
                    "stepSize": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.001)),
                    "minNotional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 5)),
                    "status": s.get("status", "TRADING"),
                    "found": True,
                }
        # Symbol not found on exchange
        return {"pricePrecision": 2, "quantityPrecision": 3, "minQty": 0.001, "maxQty": 1000000,
                "stepSize": 0.001, "minNotional": 5, "status": "NOT_LISTED", "found": False}
    except:
        pass
    return {"pricePrecision": 2, "quantityPrecision": 3, "minQty": 0.001, "maxQty": 1000000,
            "stepSize": 0.001, "minNotional": 5, "status": "UNKNOWN", "found": False}

def get_funding_rate(symbol):
    """Get current funding rate — uses LIVE data."""
    try:
        r = requests.get(f"{DATA_REST}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
        d = r.json()
        return {
            "fundingRate": float(d.get("lastFundingRate", 0)),
            "markPrice": float(d.get("markPrice", 0)),
            "indexPrice": float(d.get("indexPrice", 0)),
        }
    except:
        return {"fundingRate": 0, "markPrice": 0, "indexPrice": 0}

def get_open_interest(symbol):
    """Get open interest — uses LIVE data."""
    try:
        r = requests.get(f"{DATA_REST}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=10)
        d = r.json()
        return float(d.get("openInterest", 0))
    except:
        return 0

# ── Smart Money ─────────────────────────────────────────────────────────────

def get_smart_money():
    try:
        r = requests.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            json={"smartSignalType": "", "page": 1, "pageSize": 20, "chainId": "CT_501"},
            timeout=10,
        )
        d = r.json()
        return d.get("data", []) if d.get("success") else []
    except:
        return []

# ── Indicators ──────────────────────────────────────────────────────────────

def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return round(ema, 4)

def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    return round(100 - 100 / (1 + ag / al), 2)

def calc_atr(candles, period=14):
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        trs.append(tr)
    period = min(period, len(trs))
    return round(sum(trs[-period:]) / period, 4) if period > 0 else 0

def calc_volume_ratio(candles, period=20):
    if len(candles) < 2:
        return 1.0
    period = min(period, len(candles) - 1)
    avg = sum(c["volume"] for c in candles[-period-1:-1]) / period
    return round(candles[-1]["volume"] / avg, 2) if avg > 0 else 1.0

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0, 0, 0
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return 0, 0, 0
    macd_line = ema_fast - ema_slow
    # Simplified signal line
    return round(macd_line, 4), 0, 0

# ── State Management ────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"trades_today": 0, "daily_pnl": 0.0, "daily_fees": 0.0, "consecutive_losses": 0,
            "cooldown_until": None, "last_reset_date": None, "balance_start": 0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def log_event(event_type, data):
    """Log structured event to events JSONL file for traceability."""
    try:
        entry = {
            "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
            "event": event_type,
            "data": data,
        }
        with open(EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.error(f"Event log write failed: {e}")

def log_trade(data):
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(data, default=str) + "\n")
    log_event("TRADE_LOG", data)
    # Send Telegram alert
    send_telegram_alert(data)

def send_telegram_alert(trade):
    """Send trade alert directly to Telegram group via Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    CHAT_ID = TELEGRAM_CHAT_ID


    status = trade.get("status", "?")
    symbol = trade.get("symbol", "?")
    direction = trade.get("direction", "?")
    confidence = trade.get("confidence", "?")
    entry = trade.get("entry", 0)
    sl = trade.get("sl", 0)
    tp = trade.get("tp", 0)
    rr = trade.get("rr", 0)
    reason = trade.get("reason", "?")
    mode = trade.get("mode", "?").upper()
    leverage = trade.get("leverage", "?")
    qty = trade.get("qty", "?")
    notional = trade.get("notional", 0)
    net_tp = trade.get("net_tp", 0)
    fee = trade.get("fee_cost", 0)

    # Mode tag
    if MODE == "live":
        mode_tag = "💰 LIVE"
    else:
        mode_tag = "🧪 TESTNET"

    if status == "PLACED":
        emoji = "🟢" if direction == "LONG" else "🔴"
        text = (
            f"{emoji} <b>{direction} {symbol}</b> {mode_tag}\n"
            f"\n"
            f"💰 Entry: <code>${entry:,.2f}</code>\n"
            f"📊 Size: {qty} × {leverage}x (<code>${notional:,.2f}</code> notional)\n"
            f"🛑 SL: <code>${sl:,.2f}</code> | 🎯 TP: <code>${tp:,.2f}</code>\n"
            f"📐 R:R: {rr} | Confidence: {confidence}\n"
            f"💸 Fee: ${fee:.2f} | Net TP: ${net_tp:.2f}\n"
            f"\n"
            f"📝 {reason}\n"
            f"\n"
            f"🏷️ {mode_tag}"
        )
    elif status == "DRY_RUN":
        emoji = "🔍"
        text = (
            f"{emoji} <b>DRY RUN SIGNAL</b> {direction} {symbol} {mode_tag}\n"
            f"\n"
            f"💰 Price: <code>${entry:,.2f}</code>\n"
            f"📊 Size: {qty} × {leverage}x (<code>${notional:,.2f}</code> notional)\n"
            f"🛑 SL: <code>${sl:,.2f}</code> | 🎯 TP: <code>${tp:,.2f}</code>\n"
            f"📐 R:R: {rr} | Confidence: {confidence}\n"
            f"\n"
            f"📝 {reason}\n"
            f"\n"
            f"🏷️ {mode_tag} (NO EXECUTION)"
        )
    elif status == "FAILED":
        err = trade.get("entry_result", {}).get("msg", "Unknown error")
        text = (
            f"❌ <b>ORDER FAILED: {direction} {symbol}</b> {mode_tag}\n"
            f"\n"
            f"Error: {err}\n"
            f"🏷️ {mode_tag}"
        )
    else:
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            log.info(f"Alert sent: {direction} {symbol}")
        else:
            log.error(f"Telegram error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

def send_telegram_close_alert(symbol, close_type, pnl, pnl_pct, daily_pnl, entry_px,
                               exit_px=None, side=None, strategy=None, entry_time=None, qty=None):
    """Send position close alert directly to Telegram.
    Always shows NET PnL (after fees)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    CHAT_ID = TELEGRAM_CHAT_ID

    # Mode tag
    if MODE == "live":
        mode_tag = "💰 LIVE"
    else:
        mode_tag = "🧪 TESTNET"

    result_emoji = "📈" if pnl >= 0 else "📉"
    side_tag = f"{side} " if side else ""

    # Calculate hold duration
    hold_str = ""
    if entry_time:
        try:
            if isinstance(entry_time, str):
                et = datetime.fromisoformat(entry_time)
            else:
                et = datetime.fromtimestamp(entry_time, tz=timezone.utc)
            held_sec = (datetime.now(timezone.utc) - et).total_seconds()
            if held_sec >= 3600:
                hold_str = f"{int(held_sec // 3600)}h {int((held_sec % 3600) // 60)}m"
            else:
                hold_str = f"{int(held_sec // 60)}m"
        except:
            hold_str = "?"

    # Get NET PnL
    state = load_state()
    daily_fees = state.get("daily_fees", 0)
    daily_net = daily_pnl - daily_fees
    consecutive = state.get("consecutive_losses", 0)
    trades_today = state.get("trades_today", 0)

    # Build message (HTML — underscore-safe)
    text = f"{result_emoji} <b>{side_tag}{symbol}</b> {mode_tag}\n"
    text += f"\n"
    text += f"📋 {close_type}\n"

    # Entry → Exit
    if exit_px and entry_px:
        text += f"💰 Entry → Exit: <code>${entry_px:,.4f}</code> → <code>${exit_px:,.4f}</code>\n"
    elif entry_px:
        text += f"💰 Entry: <code>${entry_px:,.4f}</code>\n"

    # PnL
    text += f"📊 PnL: <code>${pnl:+.2f}</code> ({pnl_pct:+.2f}%)\n"

    # Strategy
    if strategy:
        text += f"🏷️ Strategy: {strategy}\n"

    # Hold duration
    if hold_str:
        text += f"⏱️ Held: {hold_str}\n"

    # Size
    if qty:
        text += f"📏 Qty: {qty}\n"

    text += f"\n"
    text += f"📉 Daily NET: <code>${daily_net:+.2f}</code> (fees: -${daily_fees:.2f})\n"
    text += f"📊 Trades today: {trades_today} | Consecutive losses: {consecutive}"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        log.info(f"Close alert sent: {symbol} {close_type} | status={r.status_code}")
    except Exception as e:
        log.error(f"Close alert failed: {e}")

# ── WebSocket Handlers ──────────────────────────────────────────────────────

async def ws_kline_stream(symbol, interval):
    """Subscribe to kline stream — uses LIVE WS for accurate volume."""
    stream = f"{symbol.lower()}@kline_{interval}"
    url = f"{DATA_WS_MARKET}/{stream}"
    log.info(f"WS connecting: {stream}")
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info(f"WS connected: {stream}")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        k = data["k"]
                        candle = {
                            "ts": k["t"], "open": float(k["o"]), "high": float(k["h"]),
                            "low": float(k["l"]), "close": float(k["c"]),
                            "volume": float(k["v"]), "closed": k["x"],
                        }
                        key = f"{symbol}_{interval}"
                        if key not in candle_buffer:
                            # Preload history via REST
                            candle_buffer[key] = preload_klines(symbol, interval)
                        # Update last candle or append
                        if candle_buffer[key]:
                            if not candle["closed"]:
                                candle_buffer[key][-1] = candle  # update current
                            else:
                                candle_buffer[key].append(candle)
                                if len(candle_buffer[key]) > 200:
                                    candle_buffer[key] = candle_buffer[key][-200:]
                                # Trigger analysis on candle close
                                await on_candle_close(symbol, interval)
                    except Exception as e:
                        log.error(f"WS parse error ({stream}): {e}")
        except Exception as e:
            log.error(f"WS disconnected ({stream}): {e} — reconnecting in 5s")
            await asyncio.sleep(5)

async def ws_bookticker_stream(symbol):
    """Subscribe to real-time best bid/ask — uses LIVE WS."""
    stream = f"{symbol.lower()}@bookTicker"
    url = f"{DATA_WS_PUBLIC}/{stream}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    bookticker[symbol] = {
                        "bid": float(data["b"]), "ask": float(data["a"]),
                        "bidQty": float(data["B"]), "askQty": float(data["A"]),
                        "ts": data["T"],
                    }
        except Exception as e:
            log.error(f"WS bookTicker ({symbol}): {e}")
            await asyncio.sleep(5)

async def ws_depth_stream(symbol, level=10):
    """Subscribe to partial depth for spread/slippage — uses LIVE WS."""
    stream = f"{symbol.lower()}@depth{level}@100ms"
    url = f"{DATA_WS_PUBLIC}/{stream}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    depth_cache[symbol] = {
                        "bids": [(float(b[0]), float(b[1])) for b in data.get("b", [])],
                        "asks": [(float(a[0]), float(a[1])) for a in data.get("a", [])],
                    }
        except Exception as e:
            log.error(f"WS depth ({symbol}): {e}")
            await asyncio.sleep(5)

async def ws_user_stream():
    """Subscribe to user data stream for order fills, position updates."""
    while True:
        try:
            lk = create_listen_key()
            listen_key = lk.get("listenKey")
            if not listen_key:
                log.error("Failed to create listenKey")
                await asyncio.sleep(30)
                continue

            url = f"{WS_PRIVATE}?listenKey={listen_key}"
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("WS user stream connected")
                # Keepalive every 30 min
                asyncio.create_task(keepalive_loop())

                async for msg in ws:
                    data = json.loads(msg)
                    evt = data.get("e")
                    if evt == "ORDER_TRADE_UPDATE":
                        await on_order_update(data["o"])
                    elif evt == "ACCOUNT_UPDATE":
                        await on_account_update(data["a"])
        except Exception as e:
            log.error(f"WS user stream: {e}")
            await asyncio.sleep(10)

async def keepalive_loop():
    while True:
        await asyncio.sleep(1800)  # 30 min
        keepalive_listen_key()

def preload_klines(symbol, interval, limit=100):
    """Load historical klines via REST — uses LIVE data for accurate volume."""
    try:
        r = requests.get(f"{DATA_REST}/fapi/v1/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
        return [
            {"ts": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]), "closed": True}
            for k in r.json()
        ]
    except:
        return []

# ── Event Handlers ──────────────────────────────────────────────────────────

async def on_candle_close(symbol, interval):
    """Called when a candle closes — trigger analysis."""
    key = f"{symbol}_{interval}"

    # Update market regime on every 5m candle close
    if interval == "5m":
        candles_5m = candle_buffer.get(key, [])
        candles_15m = candle_buffer.get(f"{symbol}_15m", [])
        update_regime(symbol, candles_5m, candles_15m)

    if interval == "5m":
        await check_scalp_entry(symbol)
    elif interval == "15m":
        # Update trend bias
        pass

async def on_order_update(order):
    """Handle order fill events.
    
    IMPORTANT: Binance may send account updates BEFORE order updates.
    So we check both `active_positions` AND `recently_closed` (grace buffer)
    to detect position closes and send alerts.
    """
    status = order.get("X")
    symbol = order.get("s")
    side = order.get("S")
    price = float(order.get("ap", 0))  # avg price
    qty = float(order.get("q", 0))
    order_id = order.get("i")
    order_type = order.get("o")
    pnl = float(order.get("rp", 0))
    reduce_only = order.get("R", False)  # reduceOnly field

    log.info(f"ORDER UPDATE: {symbol} {side} {qty} @ {price} status={status} type={order_type} pnl={pnl} reduce={reduce_only}")
    log_event("ORDER_UPDATE", {"symbol": symbol, "side": side, "price": price, "qty": qty, "status": status, "type": order_type, "pnl": pnl, "order_id": order_id, "reduce_only": reduce_only})

    if status == "FILLED":
        state = load_state()
        state = reset_daily(state)

        # Track fee on EVERY fill (entry + close)
        fill_notional = price * qty
        fill_fee = fill_notional * TAKER_FEE  # one-way fee per fill
        state["daily_fees"] = state.get("daily_fees", 0) + fill_fee
        log.info(f"Fee tracked: ${fill_fee:.4f} (notional=${fill_notional:.2f}) | Total fees today: ${state['daily_fees']:.2f}")

        # Look up position data — check both active and recently closed (race condition safe)
        ap = None
        ap_source = None
        if symbol in active_positions:
            ap = active_positions[symbol]
            ap_source = "active"
        elif symbol in recently_closed:
            ap = recently_closed[symbol]["ap_data"]
            ap_source = "grace_buffer"
            log.info(f"Matched close from grace buffer: {symbol}")

        # Detect if this is a closing order
        is_closing = (
            order_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"] or
            reduce_only or
            (ap and (
                (ap["side"] == "LONG" and side == "SELL") or
                (ap["side"] == "SHORT" and side == "BUY")
            ))
        )

        # Partial TP1 fill: move SL to breakeven, keep position open for TP2
        if (ap and ap_source == "active" and order_type == "TAKE_PROFIT_MARKET"
                and not ap.get("tp1_filled")
                and ap.get("tp1_qty", 0) > 0
                and order_id == ap.get("tp_algo_id")):
            state["daily_pnl"] += pnl
            entry_px = ap.get("entry", price)
            pnl_pct = (pnl / (entry_px * qty) * 100) if entry_px * qty > 0 else 0
            log.info(f"🎯 TP1 PARTIAL: {symbol} qty={qty} @ ${price} PnL=${pnl:+.4f} ({pnl_pct:+.2f}%) — moving SL to breakeven")
            log_event("TP1_PARTIAL", {"symbol": symbol, "qty": qty, "price": price, "pnl": pnl, "entry": entry_px})

            # Move SL to breakeven (entry price)
            try:
                old_sl_id = ap.get("sl_algo_id")
                if old_sl_id:
                    signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": old_sl_id})
                info = get_exchange_info(symbol)
                tick_size = info.get("tickSize", 0.01)
                be_sl = round(round(entry_px / tick_size) * tick_size, info["pricePrecision"])
                sl_result = place_sl_tp(symbol, ap["side"], ap.get("tp2_qty", qty), be_sl, None)
                new_sl_id = sl_result.get("sl", {}).get("algoId")
                ap["sl"] = be_sl
                ap["sl_algo_id"] = new_sl_id
                log.info(f"SL moved to breakeven: {symbol} @ ${be_sl}")
            except Exception as e:
                log.error(f"Failed to move SL to breakeven: {e}")

            ap["tp1_filled"] = True
            ap["size"] = ap.get("tp2_qty", 0)
            send_telegram_close_alert(symbol, "TP1 Hit 🎯 (50%)", pnl, pnl_pct, state["daily_pnl"], entry_px,
                                       exit_px=price, side=ap.get("side"), strategy=ap.get("strategy"),
                                       entry_time=ap.get("entry_time"), qty=qty)
            save_state(state)
            return

        if is_closing and ap:
            if pnl != 0:
                state["daily_pnl"] += pnl
                if pnl < 0:
                    state["consecutive_losses"] += 1
                else:
                    state["consecutive_losses"] = 0
                log.info(f"PnL realized: ${pnl:.4f} | Daily: ${state['daily_pnl']:.4f}")

            # Determine close type
            if order_type == "STOP_MARKET":
                close_type = "SL Hit 🛑"
            elif order_type == "TAKE_PROFIT_MARKET":
                close_type = "TP Hit 🎯"
            else:
                # MARKET close — check if price near SL or TP
                if ap.get("sl") and ap.get("tp"):
                    dist_sl = abs(price - ap["sl"])
                    dist_tp = abs(price - ap["tp"])
                    close_type = "SL Hit 🛑 (market)" if dist_sl < dist_tp else "TP Hit 🎯 (market)"
                else:
                    close_type = "Closed (market)"

            entry_px = ap.get("entry", price)
            pnl_pct = (pnl / (entry_px * qty) * 100) if entry_px * qty > 0 else 0
            
            log.info(f"🎯 CLOSE DETECTED: {symbol} | {close_type} | PnL=${pnl:+.4f} ({pnl_pct:+.2f}%) | source={ap_source}")
            log_event("CLOSE_DETECTED", {"symbol": symbol, "close_type": close_type, "pnl": pnl, "pnl_pct": pnl_pct, "daily_pnl": state["daily_pnl"], "source": ap_source, "order_type": order_type, "entry": entry_px, "close_price": price})
            
            send_telegram_close_alert(symbol, close_type, pnl, pnl_pct, state["daily_pnl"], entry_px,
                                       exit_px=price, side=ap.get("side"), strategy=ap.get("strategy"),
                                       entry_time=ap.get("entry_time"), qty=qty)

            # Cancel sibling algo orders (SL/TP pair)
            try:
                cancel_algo_orders(symbol)
                log.info(f"Cancelled remaining algo orders for {symbol}")
            except Exception as e:
                log.error(f"Failed to cancel sibling orders: {e}")

            # Clean up from whichever dict it's in
            active_positions.pop(symbol, None)
            recently_closed.pop(symbol, None)

        elif is_closing and not ap:
            # Close order detected but position data missing — still track PnL
            if pnl != 0:
                state["daily_pnl"] += pnl
                if pnl < 0:
                    state["consecutive_losses"] += 1
                else:
                    state["consecutive_losses"] = 0
                log.warning(f"⚠️ UNMATCHED CLOSE: {symbol} | PnL=${pnl:+.4f} | type={order_type} reduce={reduce_only} — position data missing, alert SKIPPED")
                log_event("UNMATCHED_CLOSE", {"symbol": symbol, "pnl": pnl, "order_type": order_type, "reduce_only": reduce_only, "side": side, "price": price})
            else:
                log.info(f"Unmatched close (zero PnL): {symbol} type={order_type} reduce={reduce_only}")

        save_state(state)

        # Log
        log_trade({
            "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
            "event": "ORDER_FILL", "symbol": symbol, "side": side,
            "price": price, "qty": qty, "status": status,
            "type": order_type, "pnl": pnl, "order_id": order_id,
            "reduce_only": reduce_only,
        })

async def on_account_update(account):
    """Handle account/position updates.
    
    IMPORTANT: Binance sends account updates BEFORE order updates in the same WS message.
    We must NOT overwrite active_positions fields here — only update size/entry.
    When position goes to 0, move to `recently_closed` grace buffer so on_order_update
    can still match the close event and send alert.
    """
    for pos in account.get("P", []):
        symbol = pos["s"]
        amt = float(pos["pa"])
        if amt != 0:
            # Position exists — preserve all tracking fields, only update live data
            existing = active_positions.get(symbol, {})
            active_positions[symbol] = {
                **existing,  # preserve sl, tp, sl_algo_id, tp_algo_id, highest_pnl_pct, entry_time
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry": float(pos["ep"]),
            }
            log.info(f"Position update: {symbol} {active_positions[symbol]['side']} size={abs(amt)} entry={float(pos['ep'])}")
            log_event("POSITION_UPDATE", {"symbol": symbol, "side": active_positions[symbol]["side"], "size": abs(amt), "entry": float(pos["ep"])})
        elif symbol in active_positions:
            # Position closed (amt=0) — move to grace buffer, DON'T delete yet
            ap = active_positions.pop(symbol)
            recently_closed[symbol] = {
                "ap_data": ap,
                "close_ts": time.time(),
            }
            log.info(f"Position closed (account update): {symbol} — moved to grace buffer for {CLOSE_GRACE_SECONDS}s")
            log_event("POSITION_CLOSED_ACCOUNT", {"symbol": symbol, "ap_data": ap})

# ── Scalping Logic ──────────────────────────────────────────────────────────

# ── Strategy E: EMA Momentum ───────────────────────────────────────────────

def check_ema_momentum(symbol, candles_5m, closes_5m, ema9, ema21, rsi_5, vol_ratio,
                        trend_15m, current_price, allow_long, allow_short):
    """Strategy E: EMA Momentum — catches trending markets that Strategies A-D miss.
    
    Trigger conditions (STRICT, not for forcing trades):
    LONG:
      1. EMA9 just crossed above EMA21 (or was above for last 3 candles)
      2. 3 consecutive higher closes (or at least 2 of 3 bullish candles)
      3. RSI rising consistently (current RSI > prev RSI > prev-prev RSI) OR RSI in 45-65 range trending up
      4. Price above both EMAs
      5. 15m trend is UP or RANGE (not counter-trend)
    
    SHORT: Mirror logic
    
    Confidence: C2 (lower conviction, smaller position size)
    """
    if len(candles_5m) < 25:
        return None
    
    # ── LONG Momentum ──
    if allow_long and trend_15m in ["UP", "RANGE"]:
        # Check 1: EMA9 > EMA21 (bullish alignment)
        if ema9 is not None and ema21 is not None and ema9 > ema21:
            # Check 2: Price above both EMAs
            if current_price > ema9 and current_price > ema21:
                # Check 3: Recent EMA crossover detection (within last 5 candles) or sustained alignment
                recent_closes = closes_5m[-5:]
                ema9_recent = []
                ema21_recent = []
                
                # Calculate EMA9 and EMA21 for last 5 closes
                k9 = 2 / (9 + 1)
                k21 = 2 / (21 + 1)
                if len(closes_5m) >= 30:
                    # Calculate historical EMAs
                    e9 = sum(closes_5m[:9]) / 9
                    e21 = sum(closes_5m[:21]) / 21
                    ema9_series = [e9]
                    ema21_series = [e21]
                    for c in closes_5m[9:]:
                        e9 = (c - e9) * k9 + e9
                        ema9_series.append(e9)
                    for c in closes_5m[21:]:
                        e21 = (c - e21) * k21 + e21
                        ema21_series.append(e21)
                    
                    # Align series — both should have same length relative to closes_5m
                    offset = len(ema9_series) - len(ema21_series)
                    ema9_aligned = ema9_series[offset:]
                    
                    # Check for recent crossover (EMA9 crossed above EMA21 in last 5 candles)
                    if len(ema9_aligned) >= 5 and len(ema21_series) >= 5:
                        crossover_detected = False
                        for i in range(-5, 0):
                            if i - 1 >= -len(ema9_aligned) and i - 1 >= -len(ema21_series):
                                was_below = ema9_aligned[i-1] <= ema21_series[i-1]
                                is_above = ema9_aligned[i] > ema21_series[i]
                                if was_below and is_above:
                                    crossover_detected = True
                                    break
                        
                        # Also accept sustained bullish alignment (EMA9 > EMA21 for 3+ candles)
                        sustained_bullish = all(
                            ema9_aligned[i] > ema21_series[i]
                            for i in range(-3, 0)
                            if abs(i) <= len(ema9_aligned) and abs(i) <= len(ema21_series)
                        )
                        
                        if crossover_detected or sustained_bullish:
                            # Check 4: 3-candle confirmation — at least 2 bullish candles out of last 3
                            last3 = candles_5m[-3:]
                            bullish_count = sum(1 for c in last3 if c["close"] > c["open"])
                            
                            if bullish_count >= 2:
                                # Check 5: RSI trending up (not overbought)
                                rsi_vals = []
                                closes_for_rsi = closes_5m[-20:]  # enough for RSI calc
                                for offset_rsi in range(3):
                                    subset = closes_for_rsi[:len(closes_for_rsi)-offset_rsi] if offset_rsi > 0 else closes_for_rsi
                                    rsi_vals.append(calc_rsi(subset))
                                
                                rsi_rising = len(rsi_vals) >= 3 and rsi_vals[0] > rsi_vals[1] > rsi_vals[2]
                                rsi_favorable = 40 <= rsi_5 <= 70  # Not overbought, not oversold
                                
                                if (rsi_rising or rsi_favorable) and rsi_5 < 70:
                                    # Determine crossover type for reason
                                    if crossover_detected:
                                        cross_reason = "EMA9 crossed above EMA21"
                                    else:
                                        cross_reason = "EMA9 sustained above EMA21 (3+ candles)"
                                    
                                    return {
                                        "dir": "LONG",
                                        "confidence": "C2",
                                        "strategy": "ema_momentum",
                                        "reason": (
                                            f"Momentum: {cross_reason} + "
                                            f"{bullish_count}/3 bullish candles + "
                                            f"RSI={rsi_5} trending up + "
                                            f"price above EMAs + 15m {trend_15m}"
                                        ),
                                    }
    
    # ── SHORT Momentum (mirror) ──
    if allow_short and trend_15m in ["DOWN", "RANGE"]:
        if ema9 is not None and ema21 is not None and ema9 < ema21:
            if current_price < ema9 and current_price < ema21:
                recent_closes = closes_5m[-5:]
                
                if len(closes_5m) >= 30:
                    k9 = 2 / (9 + 1)
                    k21 = 2 / (21 + 1)
                    e9 = sum(closes_5m[:9]) / 9
                    e21 = sum(closes_5m[:21]) / 21
                    ema9_series = [e9]
                    ema21_series = [e21]
                    for c in closes_5m[9:]:
                        e9 = (c - e9) * k9 + e9
                        ema9_series.append(e9)
                    for c in closes_5m[21:]:
                        e21 = (c - e21) * k21 + e21
                        ema21_series.append(e21)
                    
                    offset = len(ema9_series) - len(ema21_series)
                    ema9_aligned = ema9_series[offset:]
                    
                    if len(ema9_aligned) >= 5 and len(ema21_series) >= 5:
                        crossover_detected = False
                        for i in range(-5, 0):
                            if i - 1 >= -len(ema9_aligned) and i - 1 >= -len(ema21_series):
                                was_above = ema9_aligned[i-1] >= ema21_series[i-1]
                                is_below = ema9_aligned[i] < ema21_series[i]
                                if was_above and is_below:
                                    crossover_detected = True
                                    break
                        
                        sustained_bearish = all(
                            ema9_aligned[i] < ema21_series[i]
                            for i in range(-3, 0)
                            if abs(i) <= len(ema9_aligned) and abs(i) <= len(ema21_series)
                        )
                        
                        if crossover_detected or sustained_bearish:
                            last3 = candles_5m[-3:]
                            bearish_count = sum(1 for c in last3 if c["close"] < c["open"])
                            
                            if bearish_count >= 2:
                                rsi_vals = []
                                closes_for_rsi = closes_5m[-20:]
                                for offset_rsi in range(3):
                                    subset = closes_for_rsi[:len(closes_for_rsi)-offset_rsi] if offset_rsi > 0 else closes_for_rsi
                                    rsi_vals.append(calc_rsi(subset))
                                
                                rsi_falling = len(rsi_vals) >= 3 and rsi_vals[0] < rsi_vals[1] < rsi_vals[2]
                                rsi_favorable = 30 <= rsi_5 <= 60
                                
                                if (rsi_falling or rsi_favorable) and rsi_5 > 30:
                                    if crossover_detected:
                                        cross_reason = "EMA9 crossed below EMA21"
                                    else:
                                        cross_reason = "EMA9 sustained below EMA21 (3+ candles)"
                                    
                                    return {
                                        "dir": "SHORT",
                                        "confidence": "C2",
                                        "strategy": "ema_momentum",
                                        "reason": (
                                            f"Momentum: {cross_reason} + "
                                            f"{bearish_count}/3 bearish candles + "
                                            f"RSI={rsi_5} trending down + "
                                            f"price below EMAs + 15m {trend_15m}"
                                        ),
                                    }
    
    return None


def _log_signal_rejection(symbol, trend_15m, rsi_5, rsi_15, vol_ratio,
                           ema9_5, ema21_5, current_price, atr_5,
                           allow_long, allow_short, adaptive_params):
    """Log detailed reasons WHY no signal was generated for a symbol.
    This helps debug filter tuning and identify missed opportunities."""
    
    reasons = []
    
    # Strategy A: Reversal
    if allow_long:
        if rsi_5 >= 35:
            reasons.append(f"A-LONG: RSI={rsi_5} not oversold (<35 needed)")
        else:
            reasons.append(f"A-LONG: RSI OK but candle/pattern/vol failed (vol={vol_ratio}x need>{adaptive_params.get('vol_ratio_min', 1.3)}x)")
    if allow_short:
        if rsi_5 <= 65:
            reasons.append(f"A-SHORT: RSI={rsi_5} not overbought (>65 needed)")
        else:
            reasons.append(f"A-SHORT: RSI OK but candle/pattern/vol failed")
    
    # Strategy B: Trend Follow
    if allow_long and trend_15m == "UP":
        rsi_max = adaptive_params.get('rsi_long_max', 45)
        if rsi_5 > rsi_max:
            reasons.append(f"B-LONG: RSI={rsi_5} no pullback (<{rsi_max} needed)")
        elif vol_ratio < adaptive_params.get('vol_ratio_min', 1.3):
            reasons.append(f"B-LONG: RSI pulled back but vol={vol_ratio}x too low")
        else:
            reasons.append(f"B-LONG: candle/close conditions not met")
    if allow_short and trend_15m == "DOWN":
        rsi_min = adaptive_params.get('rsi_short_min', 55)
        if rsi_5 < rsi_min:
            reasons.append(f"B-SHORT: RSI={rsi_5} no bounce (>{rsi_min} needed)")
        else:
            reasons.append(f"B-SHORT: conditions not met")
    
    # Strategy C: Mean Reversion
    if trend_15m == "RANGE":
        reasons.append(f"C: Not in RANGE (trend={trend_15m})")
    else:
        reasons.append(f"C: Skipped (trend={trend_15m}, need RANGE)")
    
    # Strategy D: Breakout
    if vol_ratio < 2.0:
        reasons.append(f"D: Vol={vol_ratio}x too low (need >2.0x)")
    
    # Strategy E: EMA Momentum
    if allow_long:
        if ema9_5 and ema21_5:
            if ema9_5 <= ema21_5:
                reasons.append(f"E-LONG: EMA9({ema9_5}) <= EMA21({ema21_5}) no bullish alignment")
            elif current_price <= ema9_5:
                reasons.append(f"E-LONG: Price({current_price}) below EMA9({ema9_5})")
            elif rsi_5 >= 70:
                reasons.append(f"E-LONG: RSI={rsi_5} overbought")
            else:
                reasons.append(f"E-LONG: EMA aligned+price above but candle confirm or RSI trend failed")
        else:
            reasons.append(f"E-LONG: Insufficient data for EMAs")
    if not allow_long and not allow_short:
        reasons.append(f"E: Both directions blocked by regime")
    
    rejection_summary = " | ".join(reasons)
    log.info(
        f"❌ NO SIGNAL {symbol}: {trend_15m} trend | "
        f"RSI={rsi_5}/{rsi_15} | Vol={vol_ratio}x | ATR={atr_5} | "
        f"EMA9={ema9_5} EMA21={ema21_5} | Px={current_price} | "
        f"Long={'✅' if allow_long else '❌'} Short={'✅' if allow_short else '❌'}\n"
        f"   REJECT: {rejection_summary}"
    )
    
    log_event("SIGNAL_REJECTION", {
        "symbol": symbol,
        "trend_15m": trend_15m,
        "rsi_5": rsi_5,
        "rsi_15": rsi_15,
        "vol_ratio": vol_ratio,
        "atr": atr_5,
        "ema9": ema9_5,
        "ema21": ema21_5,
        "price": current_price,
        "allow_long": allow_long,
        "allow_short": allow_short,
        "reasons": reasons,
    })


# ── State Management ────────────────────────────────────────────────────────

def reset_daily(state):
    today = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today:
        state["trades_today"] = 0
        state["daily_pnl"] = 0.0
        state["daily_fees"] = 0.0
        state["consecutive_losses"] = 0
        state["cooldown_until"] = None
        state["last_reset_date"] = today
        bal, _ = get_balance()
        state["balance_start"] = bal
    return state

async def check_scalp_entry(symbol):
    """Main scalp entry check — called on 5m candle close."""
    state = load_state()
    state = reset_daily(state)

    # Cooldown check
    if state.get("cooldown_until"):
        ct = datetime.fromisoformat(state["cooldown_until"])
        if datetime.now(timezone.utc) < ct:
            save_state(state)
            return

    # Daily loss check
    bal, avail = get_balance()
    if state["daily_pnl"] < -(bal * DAILY_LOSS_LIMIT_PCT):
        log.warning("Daily loss limit")
        return

    # ── Get adaptive parameters from regime engine ──
    ap = get_params(symbol)
    regime = get_regime(symbol)

    # ── Apply learning engine adaptive config (pair-level only) ──
    # NOTE: direction-level filtering moved to post-signal section below
    learning_config = load_learning_config()
    if learning_config:
        pair_cfg = learning_config.get("pairs", {}).get(symbol, {})
        pair_weight = pair_cfg.get("weight", 1.0)
        
        # Skip disabled pairs
        if pair_weight == 0.0 or symbol in learning_config.get("disabled", []):
            log.info(f"{symbol}: DISABLED by learning engine (WR={pair_cfg.get('win_rate', 0):.0f}%, pnl=${pair_cfg.get('pnl', 0):.2f})")
            return
        
        # Override vol threshold if learning config is higher
        learn_vol = pair_cfg.get("vol_threshold", learning_config.get("global", {}).get("vol_threshold", 0))
        if learn_vol > ap.get("vol_ratio_min", 2.0):
            ap["vol_ratio_min"] = learn_vol

    if regime:
        log.info(f"📊 {symbol} Regime: {regime.get('regime_summary', '?')}")
        log_event("REGIME_CHECK", {"symbol": symbol, "volatility": regime.get("volatility"), "trend": regime.get("trend"), "params": ap})

    # Adaptive max positions
    max_pos = ap.get("max_positions", 1)

    # Max trades
    if state["trades_today"] >= MAX_TRADES_DAY:
        return

    # Consecutive losses
    if state["consecutive_losses"] >= CONSEC_LOSS_LIMIT:
        state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(hours=LOSS_COOLDOWN_HOURS)).isoformat()
        save_state(state)
        return

    # Already in position?
    positions = get_positions()
    # Global max active positions (max 2 concurrent trades across all pairs)
    MAX_GLOBAL_POSITIONS = int(os.environ.get("SCALPER_MAX_GLOBAL_POSITIONS", "1"))
    if len(positions) >= MAX_GLOBAL_POSITIONS:
        log.info(f"❌ {symbol}: Max global positions reached ({len(positions)}/{MAX_GLOBAL_POSITIONS})")
        return
    if len(positions) >= max_pos:
        return
    if any(p["symbol"] == symbol for p in positions):
        return

    # Signal cooldown (avoid duplicate within 15 min)
    now = time.time()
    if symbol in signal_cooldown and now - signal_cooldown[symbol] < SIGNAL_COOLDOWN_SEC:
        return

    # ── Get data ──
    key_5m = f"{symbol}_5m"
    key_15m = f"{symbol}_15m"
    candles_5m = candle_buffer.get(key_5m, [])
    candles_15m = candle_buffer.get(key_15m, [])

    if len(candles_5m) < 30 or len(candles_15m) < 50:
        log.info(f"{symbol}: Not enough candle data yet")
        return

    # ── 15m Trend ──
    closes_15m = [c["close"] for c in candles_15m]
    ema9_15 = calc_ema(closes_15m, 9)
    ema21_15 = calc_ema(closes_15m, 21)
    ema50_15 = calc_ema(closes_15m, 50)
    rsi_15 = calc_rsi(closes_15m)

    if ema9_15 > ema21_15 > ema50_15:
        trend_15m = "UP"
    elif ema9_15 < ema21_15 < ema50_15:
        trend_15m = "DOWN"
    else:
        trend_15m = "RANGE"

    # ── 5m Entry Signal ──
    closes_5m = [c["close"] for c in candles_5m]
    ema9_5 = calc_ema(closes_5m, 9)
    ema21_5 = calc_ema(closes_5m, 21)
    rsi_5 = calc_rsi(closes_5m)
    atr_5 = calc_atr(candles_5m)
    vol_ratio = calc_volume_ratio(candles_5m)

    prev = candles_5m[-2]
    curr = candles_5m[-1]
    current_price = curr["close"]

    signal = None

    # ── Adaptive thresholds from regime (scalping v3) ──
    vol_min = ap.get("vol_ratio_min", 0.6)      # Scalping: much lower threshold
    rsi_long_max = ap.get("rsi_long_max", 55)    # Scalping: wider window
    rsi_short_min = ap.get("rsi_short_min", 45)  # Scalping: wider window
    allow_long = ap.get("allow_long", True)
    allow_short = ap.get("allow_short", True)
    conf_min = ap.get("confidence_min", "C2")     # Scalping: allow C2
    risk_mult = ap.get("risk_multiplier", 1.0)

    # Pair blacklist now sourced from adaptive-config.json `disabled` list
    # (handled at load_learning_config check above).

    # ── Collect all matching strategy candidates, rank later ──
    # Previously: first-match-wins (A→B→C→D→E), biased to Strategy A.
    # Now: gather every matching signal, pick highest confidence (regime-weighted tiebreak).
    candidates = []

    # ══════════════════════════════════════════════════════════════
    # ── Strategy A: Reversal Scalp (works in ALL trends) ──
    # ══════════════════════════════════════════════════════════════
    if allow_long:
        if (rsi_5 < 35 and
            prev["close"] < prev["open"] and       # Prev bearish
            curr["close"] > curr["open"] and       # Curr bullish (reversal candle)
            curr["close"] > ema9_5 and
            vol_ratio > vol_min):
            conf = "C3" if trend_15m in ["UP", "RANGE"] else "C2"
            candidates.append({
                "dir": "LONG", "confidence": conf,
                "strategy": "reversal",
                "reason": f"Reversal LONG: RSI={rsi_5} oversold + bullish candle + above EMA9 + vol={vol_ratio}x",
            })

    if allow_short:
        if (rsi_5 > 65 and
            prev["close"] > prev["open"] and       # Prev bullish
            curr["close"] < curr["open"] and       # Curr bearish (reversal candle)
            curr["close"] < ema9_5 and
            vol_ratio > vol_min):
            conf = "C3" if trend_15m in ["DOWN", "RANGE"] else "C2"
            candidates.append({
                "dir": "SHORT", "confidence": conf,
                "strategy": "reversal",
                "reason": f"Reversal SHORT: RSI={rsi_5} overbought + bearish candle + below EMA9 + vol={vol_ratio}x",
            })

    # ── Strategy B: Trend Following ──
    if allow_long and trend_15m == "UP":
        if (rsi_5 < rsi_long_max and
            curr["close"] > ema21_5 and
            curr["close"] > curr["open"] and
            vol_ratio > vol_min):
            candidates.append({
                "dir": "LONG", "confidence": "C3",
                "strategy": "trend_follow",
                "reason": f"TrendFollow: 15m UP + 5m pullback RSI={rsi_5} + bounce above EMA21({ema21_5}) + vol={vol_ratio}x",
            })
    # SHORT-in-DOWN trend-follow DISABLED (33% WR historical).

    # ── Strategy C: Mean Reversion ──
    if trend_15m == "RANGE":
        mean_price = ema21_5
        deviation = (current_price - mean_price) / mean_price * 100

        if (rsi_5 < 30 and deviation < -0.3 and
            curr["close"] > curr["open"] and
            curr["close"] > prev["close"]):
            candidates.append({
                "dir": "LONG", "confidence": "C3",
                "strategy": "mean_revert",
                "reason": f"MeanRevert: RANGE oversold RSI={rsi_5} + {deviation:.2f}% below mean + bounce + vol={vol_ratio}x",
            })

        if (rsi_5 > 70 and deviation > 0.3 and
            curr["close"] < curr["open"] and
            curr["close"] < prev["close"]):
            candidates.append({
                "dir": "SHORT", "confidence": "C3",
                "strategy": "mean_revert",
                "reason": f"MeanRevert: RANGE overbought RSI={rsi_5} + {deviation:.2f}% above mean + rejection + vol={vol_ratio}x",
            })

    # ── Strategy D: Breakout ──
    if vol_ratio > vol_min and vol_ratio > 1.5:
        if (curr["close"] > prev["high"] and
            curr["close"] > curr["open"] and
            curr["close"] > ema9_5):
            candidates.append({
                "dir": "LONG", "confidence": "C2",
                "strategy": "breakout",
                "reason": f"Breakout: Vol spike {vol_ratio}x + broke prev high + strong close above EMA9",
            })
        elif (curr["close"] < prev["low"] and
              curr["close"] < curr["open"] and
              curr["close"] < ema9_5):
            candidates.append({
                "dir": "SHORT", "confidence": "C2",
                "strategy": "breakout",
                "reason": f"Breakout: Vol spike {vol_ratio}x + broke prev low + strong close below EMA9",
            })

    # ── Strategy E: EMA Momentum ──
    ema_signal = check_ema_momentum(
        symbol, candles_5m, closes_5m,
        ema9_5, ema21_5, rsi_5, vol_ratio,
        trend_15m, current_price, allow_long, allow_short
    )
    if ema_signal:
        candidates.append(ema_signal)

    # ══════════════════════════════════════════════════════════════
    # ── Strategy F: Momentum Scalp (NEW — v3.0 scalping) ──
    # ══════════════════════════════════════════════════════════════
    # This is the KEY scalping strategy: enter in the direction of momentum
    # regardless of RSI extremes. In scalping, oversold + downtrend = SHORT!
    # Philosophy: "The trend is your friend" — ride momentum, tight TP/SL.
    #
    # Conditions are LOOSER than other strategies — this is intentional.
    # Scalping = high frequency, small profit, strict risk management.
    
    # SHORT momentum: price below both EMAs + bearish candle + any volume
    if allow_short and trend_15m in ["DOWN", "STRONG_DOWN"]:
        if (curr["close"] < ema9_5 and
            curr["close"] < ema21_5 and
            curr["close"] < curr["open"] and       # Bearish candle
            vol_ratio > max(0.3, vol_min * 0.5)):   # Very low vol requirement
            candidates.append({
                "dir": "SHORT", "confidence": "C3",
                "strategy": "momentum_scalp",
                "reason": f"Momentum SHORT: DOWN trend + price < EMA9/21 + bearish candle + vol={vol_ratio}x",
            })
    
    # LONG momentum: price above both EMAs + bullish candle + any volume
    if allow_long and trend_15m in ["UP", "STRONG_UP"]:
        if (curr["close"] > ema9_5 and
            curr["close"] > ema21_5 and
            curr["close"] > curr["open"] and       # Bullish candle
            vol_ratio > max(0.3, vol_min * 0.5)):   # Very low vol requirement
            candidates.append({
                "dir": "LONG", "confidence": "C3",
                "strategy": "momentum_scalp",
                "reason": f"Momentum LONG: UP trend + price > EMA9/21 + bullish candle + vol={vol_ratio}x",
            })
    
    # ══════════════════════════════════════════════════════════════
    # ── Strategy G: Quick Bounce Scalp (oversold/overbought bounce) ──
    # ══════════════════════════════════════════════════════════════
    # For counter-trend scalps on extreme RSI readings
    # Works even in strong trends — looking for quick 0.3-0.5% bounce
    
    # Oversold bounce LONG (even in downtrend!)
    if allow_long and rsi_5 < 20:
        if (curr["close"] > curr["open"] and       # Bullish candle forming
            curr["close"] > prev["close"]):        # Price turning up
            candidates.append({
                "dir": "LONG", "confidence": "C2",
                "strategy": "bounce_scalp",
                "reason": f"Bounce LONG: RSI={rsi_5} extremely oversold + bullish candle + turning up",
            })
    
    # Overbought bounce SHORT (even in uptrend!)
    if allow_short and rsi_5 > 80:
        if (curr["close"] < curr["open"] and       # Bearish candle forming
            curr["close"] < prev["close"]):        # Price turning down
            candidates.append({
                "dir": "SHORT", "confidence": "C2",
                "strategy": "bounce_scalp",
                "reason": f"Bounce SHORT: RSI={rsi_5} extremely overbought + bearish candle + turning down",
            })

    # ══════════════════════════════════════════════════════════════
    # ── Strategy H: Exhaustion / Recovery Scalp (NEW — v3.1) ──
    # ══════════════════════════════════════════════════════════════
    # Philosophy: "Even the strongest trend needs to breathe"
    # When price has been falling hard (EMA gap wide) but starts showing
    # signs of recovery, take a quick LONG scalp. Vice versa for SHORT.
    # This is NOT a reversal bet — it's a mean-reversion scalp.
    #
    # Key insight: The bigger the EMA gap, the stronger the snapback
    # potential. We don't fight the trend — we surf the relief bounce.

    # Calculate EMA gap (% distance between EMA9 and EMA21)
    ema_gap_pct = 0
    if ema21_5 > 0:
        ema_gap_pct = abs(ema9_5 - ema21_5) / ema21_5 * 100

    # LONG exhaustion bounce in downtrend
    # Conditions: price fell hard (EMA9 far below EMA21) BUT
    # current candle is bullish = sellers exhausting, bounce coming
    if allow_long and trend_15m in ["DOWN", "STRONG_DOWN"]:
        if (ema9_5 < ema21_5 and                       # Bearish EMA alignment (downtrend)
            ema_gap_pct > 0.3 and                      # Significant gap = oversold
            curr["close"] > curr["open"] and           # Bullish candle (recovery attempt)
            curr["close"] > prev["close"]):            # Price turning up
            conf = "C3" if ema_gap_pct > 0.8 else "C2"  # Bigger gap = higher confidence
            candidates.append({
                "dir": "LONG", "confidence": conf,
                "strategy": "exhaustion_scalp",
                "reason": f"Exhaustion LONG: DOWN trend but EMA gap={ema_gap_pct:.1f}% (oversold) + bullish recovery candle",
            })

    # SHORT exhaustion bounce in uptrend
    # Conditions: price rallied hard (EMA9 far above EMA21) BUT
    # current candle is bearish = buyers exhausting, pullback coming
    if allow_short and trend_15m in ["UP", "STRONG_UP"]:
        if (ema9_5 > ema21_5 and                       # Bullish EMA alignment (uptrend)
            ema_gap_pct > 0.3 and                      # Significant gap = overbought
            curr["close"] < curr["open"] and           # Bearish candle (pullback attempt)
            curr["close"] < prev["close"]):            # Price turning down
            conf = "C3" if ema_gap_pct > 0.8 else "C2"  # Bigger gap = higher confidence
            candidates.append({
                "dir": "SHORT", "confidence": conf,
                "strategy": "exhaustion_scalp",
                "reason": f"Exhaustion SHORT: UP trend but EMA gap={ema_gap_pct:.1f}% (overbought) + bearish pullback candle",
            })

    # ── Rank candidates ──
    # Primary key: confidence (C4>C3>C2). Tiebreak: regime preference.
    # Preferred strategy per regime (best fit to market structure):
    #   UP/STRONG_UP:   trend_follow > ema_momentum > breakout > reversal
    #   DOWN/STRONG_DOWN: reversal > breakout > ema_momentum
    #   RANGE:          mean_revert > reversal > breakout
    REGIME_PREF = {
        "UP":          {"momentum_scalp": 4, "trend_follow": 3, "ema_momentum": 2, "breakout": 1, "reversal": 0, "mean_revert": 0, "bounce_scalp": 0, "exhaustion_scalp": 3},
        "STRONG_UP":   {"momentum_scalp": 4, "trend_follow": 3, "ema_momentum": 2, "breakout": 1, "reversal": 0, "mean_revert": 0, "bounce_scalp": 0, "exhaustion_scalp": 3},
        "DOWN":        {"momentum_scalp": 4, "reversal": 3, "breakout": 2, "ema_momentum": 1, "trend_follow": 0, "mean_revert": 0, "bounce_scalp": 2, "exhaustion_scalp": 3},
        "STRONG_DOWN": {"momentum_scalp": 4, "reversal": 3, "breakout": 2, "ema_momentum": 1, "trend_follow": 0, "mean_revert": 0, "bounce_scalp": 2, "exhaustion_scalp": 3},
        "RANGE":       {"mean_revert": 3, "reversal": 2, "breakout": 1, "trend_follow": 0, "ema_momentum": 0, "momentum_scalp": 0, "bounce_scalp": 2, "exhaustion_scalp": 2},
    }
    CONF_RANK = {"C4": 4, "C3": 3, "C2": 2}
    regime_pref = REGIME_PREF.get(trend_15m, {})

    if candidates:
        candidates.sort(
            key=lambda c: (
                CONF_RANK.get(c.get("confidence"), 0),
                regime_pref.get(c.get("strategy"), 0),
            ),
            reverse=True,
        )
        signal = candidates[0]
        if len(candidates) > 1:
            log.info(f"{symbol}: {len(candidates)} candidates → picked {signal['strategy']}/{signal['confidence']} "
                     f"(others: {[(c['strategy'], c['confidence']) for c in candidates[1:]]})")

    if not signal:
        # ── Signal Rejection Log ──
        # Log WHY no signal — for debugging and optimization
        _log_signal_rejection(
            symbol, trend_15m, rsi_5, rsi_15, vol_ratio,
            ema9_5, ema21_5, current_price, atr_5,
            allow_long, allow_short, ap
        )
        return

    # ── Counter-trend Protection (scalping v3: reduced, not blocking) ──
    # OLD: Blocked counter-trend trades entirely — too conservative for scalping.
    # NEW: Only reduce size for counter-trend in extreme conditions.
    # Scalpers CAN trade against trend (bounce scalps), just smaller size.
    if regime:
        regime_vol = regime.get("volatility", "MEDIUM")
        regime_trend = regime.get("trend", "RANGE")

        # Reduce size (50%) for counter-trend in EXTREME vol — don't block entirely
        counter_trend = False
        if signal["dir"] == "LONG" and regime_trend in ["DOWN", "STRONG_DOWN"] and regime_vol in ["EXTREME"]:
            counter_trend = True
            log.info(f"{symbol}: LONG counter-trend in {regime_vol} {regime_trend} → size -50%")
        if signal["dir"] == "SHORT" and regime_trend in ["UP", "STRONG_UP"] and regime_vol in ["EXTREME"]:
            counter_trend = True
            log.info(f"{symbol}: SHORT counter-trend in {regime_vol} {regime_trend} → size -50%")
        
        if counter_trend:
            risk_mult *= 0.5

    # ── Post-signal Learning Filters ──
    # Check direction-level learning engine (now safe — signal direction is known)
    if learning_config:
        direction = signal["dir"]
        direction_cfg = learning_config.get("directions", {}).get(direction, {})
        dir_weight = direction_cfg.get("weight", 1.0)
        if dir_weight == 0.0:
            log.info(f"{symbol}: DISABLED {direction} by learning engine (WR={direction_cfg.get('win_rate', 0):.0f}%)")
            return

    # C2 risk sizing: reduce position for low-confidence signals (env-tunable)
    c2_size_mult = C2_SIZE_MULT if signal["confidence"] == "C2" else 1.0

    # Confidence filter — adaptive based on regime
    conf_order = {"C2": 2, "C3": 3, "C4": 4}
    if conf_order.get(signal["confidence"], 0) < conf_order.get(conf_min, 3):
        log.info(f"{symbol}: {signal['confidence']} signal skipped — regime requires {conf_min}")
        return

    # Live mode: allow C2 signals (was: skip C2)
    # 2026-05-15: relaxed to allow more trades in low-vol market

    # ── Smart Money overlay ──
    sm_bullish = False
    sm_bearish = False
    try:
        sm_signals = get_smart_money()
        base = symbol.replace("USDT", "")
        matching = [s for s in sm_signals if s.get("ticker", "").upper().startswith(base)]
        buys = sum(1 for s in matching if s.get("direction") == "buy" and s.get("status") == "active")
        sells = sum(1 for s in matching if s.get("direction") == "sell" and s.get("status") == "active")
        sm_bullish = buys > sells * 2
        sm_bearish = sells > buys * 2
        log.info(f"{symbol} Smart Money: {buys} buy vs {sells} sell")
    except:
        pass

    # Smart money boost: when whale flow aligns with signal direction, bump risk_mult
    # by 20% (real size effect). Previous behavior boosted confidence to C4 — dead
    # code since no regime required C4. Confidence stays as-is for filter logic.
    sm_size_bonus = 1.0
    if signal["dir"] == "LONG" and sm_bullish:
        sm_size_bonus = SM_SIZE_BONUS
        log.info(f"{symbol}: Smart money LONG-aligned → size x{SM_SIZE_BONUS}")
    elif signal["dir"] == "SHORT" and sm_bearish:
        sm_size_bonus = SM_SIZE_BONUS
        log.info(f"{symbol}: Smart money SHORT-aligned → size x{SM_SIZE_BONUS}")
    # Apply to risk multiplier (used in sizing below)
    risk_mult = risk_mult * sm_size_bonus

    # ── Funding rate check ──
    funding = get_funding_rate(symbol)
    funding_cost = funding["fundingRate"]
    if signal["dir"] == "LONG" and funding_cost > FUNDING_MAX:
        log.info(f"{symbol}: High funding cost {funding_cost} — skipping LONG")
        return
    if signal["dir"] == "SHORT" and funding_cost < -FUNDING_MAX:
        log.info(f"{symbol}: High funding cost {funding_cost} — skipping SHORT")
        return

    # ── Spread check ──
    bt = bookticker.get(symbol, {})
    if bt:
        spread = (bt["ask"] - bt["bid"]) / bt["bid"] * 10000
        if spread > MAX_SPREAD_BPS:
            log.info(f"{symbol}: Spread too wide {spread:.1f} bps — skipping")
            return

    # ── Position sizing (adaptive) ──
    info = get_exchange_info(symbol)

    # Validate symbol exists on exchange
    if not info.get("found", False):
        log.warning(f"{symbol}: Symbol not found on {MODE} exchange — skipping")
        return
    if info.get("status") not in ("TRADING",):
        log.warning(f"{symbol}: Symbol status={info.get('status')} — skipping")
        return

    trade_usd = avail * SIZE_PCT * risk_mult * c2_size_mult  # Scale by regime + confidence
    raw_qty = trade_usd * LEVERAGE / current_price
    # Respect stepSize (must be multiple of stepSize)
    step = info["stepSize"]
    qty = round(raw_qty / step) * step
    # Round to quantityPrecision
    qty = round(qty, info["quantityPrecision"])
    notional = qty * current_price

    if notional < info["minNotional"]:
        qty = math.ceil(info["minNotional"] / current_price / step) * step
        qty = round(qty, info["quantityPrecision"])
        notional = qty * current_price

    # Final check: qty must be >= minQty
    if qty < info["minQty"]:
        log.warning(f"{symbol}: Qty {qty} < minQty {info['minQty']} — skipping")
        return

    # Final check: qty must be <= maxQty (prevent -2027 max position exceeded)
    max_qty = info.get("maxQty", 1000000)
    if qty > max_qty:
        log.warning(f"{symbol}: Qty {qty} > maxQty {max_qty} — clamping")
        qty = math.floor(max_qty / step) * step
        qty = round(qty, info["quantityPrecision"])
        notional = qty * current_price

    # ── TP/SL (adaptive from regime) ──
    adaptive_tp = ap.get("tp_pct", TP_PCT)
    adaptive_sl = ap.get("sl_pct", SL_PCT)
    tick_size = info.get("tickSize", 0.01)
    def round_price(p):
        """Round price to nearest tick."""
        return round(round(p / tick_size) * tick_size, info["pricePrecision"])

    if signal["dir"] == "LONG":
        sl = round_price(current_price * (1 - adaptive_sl))
        tp = round_price(current_price * (1 + adaptive_tp))
    else:
        sl = round_price(current_price * (1 + adaptive_sl))
        tp = round_price(current_price * (1 - adaptive_tp))

    # ── Fee-aware R:R calculation ──
    # Fees eat into both TP and SL
    # Fee cost = TAKER_FEE × notional × 2 (entry + exit)
    fee_cost = TOTAL_FEE_PCT * notional  # total round-trip fee in $
    # Funding fee estimate (max hold 30min = 1/16 of 8h period)
    funding = get_funding_rate(symbol)
    funding_cost_8h = abs(funding["fundingRate"]) * notional
    funding_cost_30m = funding_cost_8h / 16
    total_cost = fee_cost + funding_cost_30m

    # Gross PnL
    gross_tp_pnl = abs(tp - current_price) * qty
    gross_sl_pnl = abs(sl - current_price) * qty

    # Net PnL (after fees)
    net_tp_pnl = gross_tp_pnl - total_cost
    net_sl_pnl = gross_sl_pnl + total_cost  # loss is worse with fees

    # Net R:R
    net_rr = round(net_tp_pnl / net_sl_pnl, 2) if net_sl_pnl > 0 else 0
    gross_rr = round(gross_tp_pnl / gross_sl_pnl, 2) if gross_sl_pnl > 0 else 0

    # Net TP as % of margin
    margin_used = notional / LEVERAGE
    net_tp_margin_pct = net_tp_pnl / margin_used if margin_used > 0 else 0

    log.info(f"📊 Fee Analysis: fee=${fee_cost:.2f} funding=${funding_cost_30m:.4f}")
    log.info(f"   Gross TP: ${gross_tp_pnl:.2f} | Net TP: ${net_tp_pnl:.2f} ({net_tp_margin_pct*100:.1f}% of margin)")
    log.info(f"   Gross SL: ${gross_sl_pnl:.2f} | Net SL: ${net_sl_pnl:.2f}")
    log.info(f"   Gross R:R: {gross_rr} → Net R:R: {net_rr}")

    # Reject if net TP too small
    if net_tp_pnl <= 0:
        log.warning(f"{symbol}: Net TP negative after fees — skipping")
        return
    if net_tp_margin_pct < MIN_NET_TP_MARGIN_PCT:
        log.warning(f"{symbol}: Net TP {net_tp_margin_pct*100:.1f}% of margin < min {MIN_NET_TP_MARGIN_PCT*100}% — skipping")
        return

    # Use NET R:R for decision (not gross)
    if net_rr < MIN_NET_RR:
        log.info(f"{symbol}: Net R:R {net_rr} < min {MIN_NET_RR} (gross was {gross_rr})")
        return

    # ── Execute ──
    log.info(f"🎯 SIGNAL: {signal['dir']} {symbol} @ ${current_price} ({signal['confidence']})")
    log.info(f"   SL=${sl} TP=${tp} Net R:R={net_rr} (gross={gross_rr}) Size={qty} Notional=${notional:.2f}")
    log.info(f"   Trend: {trend_15m} RSI={rsi_5} Vol={vol_ratio}x ATR={atr_5}")
    log.info(f"   Adaptive: TP={adaptive_tp*100:.2f}% SL={adaptive_sl*100:.2f}% Risk={risk_mult}x SizeMult={c2_size_mult} VolMin={vol_min}")
    log.info(f"   Reason: {signal['reason']}")

    # ── DRY RUN: Alert signal but skip execution ──
    if DRY_RUN:
        log.info(f"🔍 DRY RUN — signal detected but NOT executing")
        # Send directly via Telegram
        send_telegram_alert({
            "status": "DRY_RUN", "symbol": symbol, "direction": signal["dir"],
            "entry": current_price, "qty": qty, "leverage": LEVERAGE,
            "notional": notional, "sl": sl, "tp": tp,
            "rr": net_rr, "gross_rr": gross_rr,
            "confidence": signal["confidence"],
            "reason": signal["reason"], "mode": MODE,
        })

        # Still log & update state
        signal_cooldown[symbol] = time.time()
        state["trades_today"] += 1
        log_trade({
            "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
            "mode": f"{MODE}-dryrun", "symbol": symbol, "direction": signal["dir"],
            "confidence": signal["confidence"],
            "strategy": signal.get("strategy", "unknown"),
            "entry": current_price,
            "qty": qty, "notional": round(notional, 2), "leverage": LEVERAGE,
            "sl": sl, "tp": tp, "rr": net_rr, "gross_rr": gross_rr,
            "reason": signal["reason"], "status": "DRY_RUN",
        })
        save_state(state)
        return

    # Set leverage
    set_leverage(symbol, LEVERAGE)

    # Cancel existing orders (both regular + algo)
    cancel_all_for_symbol(symbol)

    # Place market entry
    side = "BUY" if signal["dir"] == "LONG" else "SELL"
    entry_result = place_order(symbol, side, qty)

    trade_data = {
        "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
        "mode": MODE, "symbol": symbol, "direction": signal["dir"],
        "confidence": signal["confidence"],
        "strategy": signal.get("strategy", "unknown"),
        "entry": current_price,
        "qty": qty, "notional": round(notional, 2), "leverage": LEVERAGE,
        "sl": sl, "tp": tp, "rr": net_rr, "gross_rr": gross_rr,
        "fee_cost": round(total_cost, 4), "net_tp": round(net_tp_pnl, 4),
        "atr": atr_5, "rsi": rsi_5,
        "rsi_15m": rsi_15, "vol_ratio": vol_ratio, "trend_15m": trend_15m,
        "funding_rate": funding_cost, "smart_money_bull": sm_bullish, "smart_money_bear": sm_bearish,
        "reason": signal["reason"], "entry_result": entry_result,
        "regime": {"volatility": regime.get("volatility") if regime else None,
                   "trend": regime.get("trend") if regime else None,
                   "adaptive_tp": adaptive_tp, "adaptive_sl": adaptive_sl,
                   "risk_multiplier": risk_mult},
    }

    if "code" in entry_result and entry_result["code"] < 0:
        trade_data["status"] = "FAILED"
        log.error(f"Entry FAILED: {entry_result}")
        log.error(f"Entry debug: symbol={symbol} side={side} qty={qty} REST_BASE={REST_BASE} API_KEY={API_KEY[:8]}...")
        log_trade(trade_data)
        save_state(state)
        return  # DON'T place SL/TP if entry failed

    # Verify entry fill — use actual fill price
    fill_price = float(entry_result.get("avgPrice", 0)) or current_price
    fill_qty = float(entry_result.get("executedQty", 0)) or qty
    fill_status = entry_result.get("status", "")

    if fill_status not in ["FILLED", "PARTIALLY_FILLED"] and "orderId" not in entry_result:
        trade_data["status"] = "FAILED"
        trade_data["entry_result"] = entry_result
        log.error(f"Entry not filled: {entry_result}")
        log_trade(trade_data)
        save_state(state)
        return

    trade_data["status"] = "PLACED"
    trade_data["order_id"] = entry_result.get("orderId", "?")
    trade_data["fill_price"] = fill_price
    trade_data["fill_qty"] = fill_qty

    # Recalculate SL/TP based on actual fill price
    # Partial TP: TP1 at half distance (50% qty), TP2 at full distance (50% qty)
    if signal["dir"] == "LONG":
        sl = round_price(fill_price * (1 - adaptive_sl))
        tp1 = round_price(fill_price * (1 + adaptive_tp * 0.5))
        tp2 = round_price(fill_price * (1 + adaptive_tp))
    else:
        sl = round_price(fill_price * (1 + adaptive_sl))
        tp1 = round_price(fill_price * (1 - adaptive_tp * 0.5))
        tp2 = round_price(fill_price * (1 - adaptive_tp))

    # Split qty 50/50, respecting stepSize
    step = info["stepSize"]
    tp1_qty = round(round((fill_qty / 2) / step) * step, info["quantityPrecision"])
    tp2_qty = round(fill_qty - tp1_qty, info["quantityPrecision"])
    # If split too small (below minQty), fall back to single TP at tp2
    if tp1_qty < info["minQty"] or tp2_qty < info["minQty"]:
        log.info(f"{symbol}: qty {fill_qty} too small to split TP — using single TP")
        tp1_qty = 0
        tp2_qty = fill_qty

    trade_data["sl"] = sl
    trade_data["tp"] = tp2  # legacy field — main TP
    trade_data["tp1"] = tp1 if tp1_qty > 0 else None
    trade_data["tp2"] = tp2
    trade_data["tp1_qty"] = tp1_qty
    trade_data["tp2_qty"] = tp2_qty

    # Place SL (full qty) + TP1 (half qty) via algo API
    if tp1_qty > 0:
        sl_tp = place_sl_tp(symbol, signal["dir"], fill_qty, sl, tp1, tp_qty=tp1_qty)
        # Place TP2 separately
        tp2_result = place_partial_tp(symbol, signal["dir"], tp2, tp2_qty)
        sl_tp["tp2"] = tp2_result
    else:
        sl_tp = place_sl_tp(symbol, signal["dir"], fill_qty, sl, tp2)
    trade_data["sl_tp_result"] = sl_tp

    # Check if SL + at least one TP succeeded
    sl_ok = "algoId" in sl_tp.get("sl", {})
    tp_ok = "algoId" in sl_tp.get("tp", {})
    tp2_ok = "algoId" in sl_tp.get("tp2", {}) if tp1_qty > 0 else True
    if not sl_ok or not tp_ok or not tp2_ok:
        log.error(f"SL/TP placement FAILED: sl_ok={sl_ok} tp1_ok={tp_ok} tp2_ok={tp2_ok} — {sl_tp}")
        # Emergency: close position if we can't set SL
        if not sl_ok:
            log.error(f"🚨 EMERGENCY: No SL set for {symbol} — closing position!")
            close_side = "SELL" if signal["dir"] == "LONG" else "BUY"
            place_order(symbol, close_side, fill_qty, reduce_only=True)
            trade_data["status"] = "EMERGENCY_CLOSED"

    # Track position
    active_positions[symbol] = {
        "side": signal["dir"], "entry": fill_price,
        "size": fill_qty, "sl": sl, "tp": tp2,
        "tp1": tp1 if tp1_qty > 0 else None, "tp2": tp2,
        "tp1_qty": tp1_qty, "tp2_qty": tp2_qty,
        "tp1_filled": False,
        "sl_algo_id": sl_tp.get("sl", {}).get("algoId"),
        "tp_algo_id": sl_tp.get("tp", {}).get("algoId"),
        "tp2_algo_id": sl_tp.get("tp2", {}).get("algoId") if tp1_qty > 0 else None,
        "highest_pnl_pct": 0, "entry_time": datetime.now(timezone.utc).isoformat(),
        "trailing_activate": ap.get("trailing_activate", TRAILING_ACTIVATE),
        "trailing_distance": ap.get("trailing_distance", TRAILING_DISTANCE),
        "strategy": signal.get("strategy", "unknown"),
    }

    signal_cooldown[symbol] = time.time()
    state["trades_today"] += 1
    state["last_trade_time"] = datetime.now(timezone(timedelta(hours=7))).isoformat()

    log_trade(trade_data)
    save_state(state)

# ── Position Monitor ────────────────────────────────────────────────────────

async def position_monitor():
    """Monitor positions every MONITOR_INTERVAL_SEC — trailing stop, timeout, PnL check."""
    while True:
        await asyncio.sleep(MONITOR_INTERVAL_SEC)
        try:
            positions = get_positions()
            for pos in positions:
                sym = pos["symbol"]
                if sym not in active_positions:
                    continue

                ap = active_positions[sym]
                entry = pos["entry"]
                size = pos["size"]
                side = pos["side"]
                pnl = pos["pnl"]
                pnl_pct = pnl / (entry * size) if entry * size > 0 else 0

                # Update highest PnL
                if pnl_pct > ap.get("highest_pnl_pct", 0):
                    ap["highest_pnl_pct"] = pnl_pct

                # Trailing stop — only update SL, keep TP intact
                # Use adaptive trailing from position snapshot (set at entry from regime params)
                trail_activate = ap.get("trailing_activate", TRAILING_ACTIVATE)
                trail_distance = ap.get("trailing_distance", TRAILING_DISTANCE)
                if ap.get("highest_pnl_pct", 0) > trail_activate:
                    trail_from = entry * (1 + ap.get("highest_pnl_pct", 0)) if side == "LONG" else entry * (1 - ap.get("highest_pnl_pct", 0))
                    trail_sl = trail_from * (1 - trail_distance) if side == "LONG" else trail_from * (1 + trail_distance)

                    # Get tick size for proper rounding
                    info = get_exchange_info(sym)
                    tick_size = info.get("tickSize", 0.01)
                    new_sl = round(round(trail_sl / tick_size) * tick_size, info["pricePrecision"])

                    # Only update if new SL is better than current
                    old_sl = ap.get("sl", 0)
                    should_update = False
                    if side == "LONG" and new_sl > old_sl:
                        should_update = True
                    elif side == "SHORT" and new_sl < old_sl:
                        should_update = True

                    if should_update:
                        # Cancel ONLY the old SL algo order (keep TP)
                        old_sl_id = ap.get("sl_algo_id")
                        if old_sl_id:
                            signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": old_sl_id})

                        # Place new SL (TP stays)
                        sl_result = place_sl_tp(sym, side, size, new_sl, None)
                        new_sl_id = sl_result.get("sl", {}).get("algoId")

                        ap["sl"] = new_sl
                        ap["sl_algo_id"] = new_sl_id
                        log.info(f"🔄 Trailing SL updated: {sym} ${old_sl} → ${new_sl}")

                # Tiered timeout (all env-tunable):
                #   ≥SOFT_TIMEOUT_SEC + |pnl| < BREAKEVEN_BAND_PCT → close to free margin
                #   ≥HARD_TIMEOUT_SEC → close regardless (SL/TP haven't fired in time)
                # Losing positions between SL and break-even at soft timeout are LEFT to run —
                # SL handles worst case; closing at -0.5% just locks loss + fees.
                if ap.get("entry_time"):
                    held = (datetime.now(timezone.utc) - datetime.fromisoformat(ap["entry_time"])).seconds

                    should_timeout = False
                    timeout_reason = ""
                    if held > SOFT_TIMEOUT_SEC and abs(pnl_pct) < BREAKEVEN_BAND_PCT:
                        should_timeout = True
                        timeout_reason = "BREAKEVEN_FLAT"
                    elif held > HARD_TIMEOUT_SEC:
                        should_timeout = True
                        timeout_reason = "HARD_CAP"

                    if should_timeout:
                        log.info(f"⏰ Position timeout: {sym} — closing ({timeout_reason}, held={held}s, pnl_pct={pnl_pct:.4f})")
                        log_event("POSITION_TIMEOUT", {"symbol": sym, "held_seconds": held, "pnl_pct": pnl_pct, "reason": timeout_reason})
                        close_side = "SELL" if side == "LONG" else "BUY"
                        # Cancel all algo orders first
                        cancel_algo_orders(sym)
                        place_order(sym, close_side, size, reduce_only=True)
                        # Move to grace buffer so on_order_update can still send close alert
                        recently_closed[sym] = {
                            "ap_data": ap,
                            "close_ts": time.time(),
                        }
                        del active_positions[sym]
                        log.info(f"Moved {sym} to grace buffer after timeout close")

            # Clean up expired grace buffer entries
            now = time.time()
            expired = [sym for sym, rc in recently_closed.items() if now - rc["close_ts"] > CLOSE_GRACE_SECONDS]
            for sym in expired:
                log.info(f"Grace buffer expired for {sym} — no close order matched")
                log_event("GRACE_EXPIRED", {"symbol": sym})
                del recently_closed[sym]
        except Exception as e:
            log.error(f"Monitor error: {e}")

# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"{'='*60}")
    log.info(f"SCALPER V2 STARTING — MODE: {MODE.upper()} | DRY_RUN: {DRY_RUN}")
    log.info(f"Pairs: {PAIRS}")
    log.info(f"Leverage: {LEVERAGE}x | Size: {SIZE_PCT*100}% | TP: {TP_PCT*100}% | SL: {SL_PCT*100}%")
    if MODE != "live":
        log.info(f"📡 HYBRID MODE: Market data = LIVE | Execution = TESTNET")
        log.info(f"   Data REST: {DATA_REST}")
        log.info(f"   Data WS:   {DATA_WS_MARKET}")
        log.info(f"   Exec REST: {REST_BASE}")
        log.info(f"   Exec WS:   {WS_PRIVATE}")
    log.info(f"{'='*60}")

    # Load cached market regimes
    init_regimes(PAIRS)

    # Initial balance
    bal, avail = get_balance()
    log.info(f"Balance: ${bal:.2f} (available: ${avail:.2f})")

    state = load_state()
    state = reset_daily(state)
    state["balance_start"] = bal
    save_state(state)

    # Preload candle history
    for sym in PAIRS:
        for interval in ["5m", "15m"]:
            key = f"{sym}_{interval}"
            candle_buffer[key] = preload_klines(sym, interval)
            log.info(f"Preloaded {len(candle_buffer[key])} candles: {key}")

    # Build WebSocket tasks
    tasks = []

    # Kline streams (5m + 15m per pair)
    for sym in PAIRS:
        tasks.append(asyncio.create_task(ws_kline_stream(sym, "5m")))
        tasks.append(asyncio.create_task(ws_kline_stream(sym, "15m")))
        tasks.append(asyncio.create_task(ws_bookticker_stream(sym)))
        tasks.append(asyncio.create_task(ws_depth_stream(sym)))

    # User stream
    tasks.append(asyncio.create_task(ws_user_stream()))

    # Position monitor
    tasks.append(asyncio.create_task(position_monitor()))

    log.info(f"Started {len(tasks)} async tasks")

    # Run forever
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Scalper stopped by user")
    except Exception as e:
        log.error(f"Fatal: {e}")
        raise
