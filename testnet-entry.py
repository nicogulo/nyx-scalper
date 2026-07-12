#!/usr/bin/env python3
"""Manual testnet entry based on market regime analysis."""
import os, sys, json, hmac, hashlib, time, requests
from datetime import datetime, timezone, timedelta

# ── Credentials ──
API_KEY = "lxexwUACIb4iUwkR7gu05P9EhpheuoT3IiirdEuPNl58FERGaoTF0NKuZaaYxNem"
API_SECRET = "dqi6C6UzKqzOmRWaI0l9QszX699kh6BfjPKpKJxZ2q9UZWAZZ4te0ZwosEukxmMW"
REST_BASE = "https://demo-fapi.binance.com"
TZ = timezone(timedelta(hours=7))

def signed_request(method, endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}
    resp = requests.request(method, url, headers=headers, timeout=10)
    return resp.json()

def public_get(endpoint, params=None):
    url = f"{REST_BASE}{endpoint}"
    resp = requests.get(url, params=params, timeout=10)
    return resp.json()

# ── 1. Check Balance ──
print("=" * 60)
print(f"⏰ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} WIB")
print("=" * 60)

account = signed_request("GET", "/fapi/v2/account")
if "code" in account:
    print(f"❌ API Error: {account}")
    sys.exit(1)

balance = [b for b in account.get("assets", []) if b["asset"] == "USDT"]
if balance:
    bal = float(balance[0]["availableBalance"])
    total = float(balance[0]["walletBalance"])
    print(f"💰 Balance: ${total:.2f} | Available: ${bal:.2f}")
else:
    print("❌ No USDT balance found")
    sys.exit(1)

# ── 2. Check Existing Positions ──
positions = signed_request("GET", "/fapi/v2/positionRisk")
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
if open_pos:
    print(f"\n📊 Open Positions:")
    for p in open_pos:
        print(f"  {p['symbol']}: {p['positionAmt']} @ ${float(p['entryPrice']):.4f} | PnL: ${float(p['unRealizedProfit']):.2f}")
else:
    print("\n📊 No open positions")

# ── 3. Analyze Regime & Pick Best Entry ──
with open("/root/.openclaw/workspace/frontend/scalper/state/market-regime.json") as f:
    regimes = json.load(f)

print("\n" + "=" * 60)
print("📋 MARKET REGIME ANALYSIS")
print("=" * 60)

# Filter for SHORT candidates (all in downtrend)
candidates = []
for sym, r in regimes.items():
    if sym == "ETHUSDT":  # Blacklisted
        continue
    vr = r["vol_ratio"]
    trend = r["trend"]
    ts = r["trend_strength"]
    rsi5 = r["rsi_5m"]
    rsi15 = r["rsi_15m"]
    vol = r["volatility"]
    mode = r["params"].get("scalp_mode", "?")
    allow_short = r["params"].get("allow_short", False)
    
    score = 0
    if allow_short and trend in ["DOWN", "STRONG_DOWN"]:
        score += ts
        if vr >= 1.5:
            score += 20
        elif vr >= 1.0:
            score += 10
        if rsi5 < 40:
            score += 5
        if vol in ["MEDIUM", "HIGH"]:
            score += 10
    
    candidates.append({
        "symbol": sym,
        "score": score,
        "trend": trend,
        "trend_str": ts,
        "vol_ratio": vr,
        "rsi_5m": rsi5,
        "rsi_15m": rsi15,
        "volatility": vol,
        "mode": mode,
        "params": r["params"],
    })
    
    status = "✅ SHORT" if (allow_short and trend in ["DOWN", "STRONG_DOWN"]) else "❌ SKIP"
    vol_pass = "✓" if vr >= 1.5 else "✗"
    print(f"  {sym}: {status} | Vol={vol} | Trend={trend}({ts}%) | VR={vr}({vol_pass}) | RSI={rsi5}/{rsi15} | Score={score}")

candidates.sort(key=lambda x: x["score"], reverse=True)
best = candidates[0]

print(f"\n🏆 Best Candidate: {best['symbol']}")
print(f"   Trend: {best['trend']} ({best['trend_str']}%)")
print(f"   Vol Ratio: {best['vol_ratio']}x")
print(f"   RSI: {best['rsi_5m']}/{best['rsi_15m']}")
print(f"   Volatility: {best['volatility']}")
print(f"   Mode: {best['mode']}")

# ── 4. Get Current Price ──
symbol = best["symbol"]
ticker = public_get("/fapi/v1/ticker/price", {"symbol": symbol})
current_price = float(ticker["price"])
print(f"\n💲 Current {symbol} Price: ${current_price}")

# ── 5. Calculate Position Size ──
LEVERAGE = 15
SIZE_PCT = 0.30
notional = bal * SIZE_PCT * LEVERAGE

exchange_info = public_get("/fapi/v1/exchangeInfo")
step_size = 1
min_qty = 1
for s in exchange_info.get("symbols", []):
    if s["symbol"] == symbol:
        for f in s["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                min_qty = float(f["minQty"])
            if f["filterType"] == "MARKET_LOT_SIZE":
                step_size = float(f["stepSize"])
        break

qty = round(notional / current_price / step_size) * step_size
qty = max(qty, min_qty)
if step_size >= 1:
    qty = int(qty)
else:
    precision = len(str(step_size).rstrip('0').split('.')[-1])
    qty = round(qty, precision)

print(f"\n📐 Position Sizing:")
print(f"   Available: ${bal:.2f}")
print(f"   Size %: {SIZE_PCT*100}%")
print(f"   Leverage: {LEVERAGE}x")
print(f"   Notional: ${notional:.2f}")
print(f"   Quantity: {qty} {symbol}")

# ── 6. Calculate TP/SL from regime params ──
params = best["params"]
tp_pct = params.get("tp_pct", 0.008)
sl_pct = params.get("sl_pct", 0.004)

entry_price = current_price
tp_price = round(entry_price * (1 - tp_pct), 6)
sl_price = round(entry_price * (1 + sl_pct), 6)

print(f"\n🎯 Entry Plan:")
print(f"   Direction: SHORT")
print(f"   Entry: ~${entry_price}")
print(f"   TP: ${tp_price} (+{tp_pct*100:.2f}%)")
print(f"   SL: ${sl_price} (-{sl_pct*100:.2f}%)")
print(f"   Risk:Reward = 1:{tp_pct/sl_pct:.1f}")

# ── 7. Set Leverage ──
lev_result = signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
print(f"\n⚙️  Leverage set: {lev_result.get('leverage', 'N/A')}x")

# ── 8. Place Market SHORT Order ──
print(f"\n🚀 Placing MARKET SHORT {qty} {symbol}...")
order = signed_request("POST", "/fapi/v1/order", {
    "symbol": symbol,
    "side": "SELL",
    "type": "MARKET",
    "quantity": qty,
    "newOrderRespType": "RESULT",
})

if "code" in order:
    print(f"❌ Order failed: {order.get('msg', order)}")
    sys.exit(1)

fill_price = float(order.get("avgPrice", order.get("price", current_price)))
fill_qty = order.get("executedQty", qty)
order_id = order.get("orderId", "?")
status = order.get("status", "?")

print(f"✅ Order FILLED!")
print(f"   Order ID: {order_id}")
print(f"   Fill Price: ${fill_price}")
print(f"   Fill Qty: {fill_qty}")
print(f"   Status: {status}")

# Recalculate TP/SL based on actual fill
tp_price = round(fill_price * (1 - tp_pct), 6)
sl_price = round(fill_price * (1 + sl_pct), 6)

# ── 9. Place TP/SL ──
print(f"\n🎯 Setting TP/SL based on fill @ ${fill_price}...")

# Stop Loss (BUY above entry)
sl_order = signed_request("POST", "/fapi/v1/order", {
    "symbol": symbol,
    "side": "BUY",
    "type": "STOP_MARKET",
    "stopPrice": sl_price,
    "closePosition": "true",
    "workingType": "CONTRACT_PRICE",
    "priceProtect": "true",
})
if "code" in sl_order:
    print(f"   SL Error: {sl_order.get('msg', sl_order)}")
else:
    print(f"   ✅ SL set @ ${sl_price} ({sl_pct*100:.2f}% above entry)")

# Take Profit (BUY below entry)
tp_order = signed_request("POST", "/fapi/v1/order", {
    "symbol": symbol,
    "side": "BUY",
    "type": "TAKE_PROFIT_MARKET",
    "stopPrice": tp_price,
    "closePosition": "true",
    "workingType": "CONTRACT_PRICE",
    "priceProtect": "true",
})
if "code" in tp_order:
    print(f"   TP Error: {tp_order.get('msg', tp_order)}")
else:
    print(f"   ✅ TP set @ ${tp_price} ({tp_pct*100:.2f}% below entry)")

# ── 10. Summary ──
print("\n" + "=" * 60)
print("📋 TRADE SUMMARY")
print("=" * 60)
print(f"  Symbol:    {symbol}")
print(f"  Direction: SHORT 📉")
print(f"  Entry:     ${fill_price}")
print(f"  TP:        ${tp_price} (+${fill_price - tp_price:.6f})")
print(f"  SL:        ${sl_price} (-${sl_price - fill_price:.6f})")
print(f"  Size:      {qty} (${fill_price * qty:.2f} notional)")
print(f"  Leverage:  {LEVERAGE}x")
print(f"  Regime:    {best['mode']} | Vol={best['volatility']} | Trend={best['trend']}({best['trend_str']}%)")
print(f"  Reason:    Market regime TREND_SHORT, vol_ratio={best['vol_ratio']}x, RSI={best['rsi_5m']}/{best['rsi_15m']}")
print("=" * 60)
