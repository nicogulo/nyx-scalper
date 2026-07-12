#!/usr/bin/env python3
"""Close all open positions on testnet and live accounts."""
import os, sys, hmac, hashlib, time, requests

def signed_request(method, endpoint, params, api_key, api_secret, base_url):
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    r = getattr(requests, method.lower())(url, headers=headers, timeout=15)
    return r.json()

def get_positions(base_url, api_key, api_secret):
    d = signed_request("GET", "/fapi/v2/positionRisk", {}, api_key, api_secret, base_url)
    if isinstance(d, dict):
        d2 = d.get("data", d)
        if "positions" in d2:
            d = d2.get("positions", [])
    positions = []
    for p in (d if isinstance(d, list) else []):
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry": float(p.get("entryPrice", 0)),
                "pnl": float(p.get("unRealizedProfit", 0)),
            })
    return positions

def close_all(base_url, api_key, api_secret, label):
    print(f"\n{'='*50}")
    print(f"📦 {label} — Checking positions...")
    print(f"{'='*50}")
    positions = get_positions(base_url, api_key, api_secret)
    if not positions:
        print(f"  ✅ No open positions on {label}")
        return
    
    print(f"  Found {len(positions)} open position(s):")
    for pos in positions:
        print(f"    {pos['symbol']} {pos['side']} | Size: {pos['size']} | Entry: {pos['entry']} | PnL: {pos['pnl']:.4f}")
    
    # Close each position
    for pos in positions:
        sym = pos["symbol"]
        side = pos["side"]
        size = pos["size"]
        close_side = "SELL" if side == "LONG" else "BUY"
        
        # Cancel all orders first
        print(f"\n  🔄 Closing {sym} {side} ({size})...")
        try:
            signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym}, api_key, api_secret, base_url)
        except:
            pass
        
        # Cancel algo orders
        try:
            d = signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym}, api_key, api_secret, base_url)
            orders = d if isinstance(d, list) else d.get("orders", d.get("data", []))
            for o in orders:
                algo_id = o.get("algoId")
                if algo_id and o.get("algoStatus") == "NEW":
                    signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, api_key, api_secret, base_url)
                    print(f"    Cancelled algo order {algo_id}")
        except:
            pass
        
        # Close position with market order
        result = signed_request("POST", "/fapi/v1/order", {
            "symbol": sym, "side": close_side, "type": "MARKET",
            "quantity": size, "reduceOnly": "true", "newOrderRespType": "RESULT"
        }, api_key, api_secret, base_url)
        
        if "code" in result:
            print(f"    ❌ Error: {result}")
        else:
            print(f"    ✅ Closed! Avg price: {result.get('avgPrice', 'N/A')} | PnL: {pos['pnl']:.4f}")
    
    print(f"\n  🏁 {label} — All positions closed!")

# ── TESTNET ──
testnet_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
testnet_secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
testnet_url = "https://demo-fapi.binance.com"

if testnet_key:
    close_all(testnet_url, testnet_key, testnet_secret, "TESTNET")
else:
    print("⚠️ No TESTNET API keys found")

# ── LIVE ──
live_key = os.environ.get("BINANCE_API_KEY", "")
live_secret = os.environ.get("BINANCE_SECRET_KEY", "")
live_url = "https://fapi.binance.com"

if live_key:
    close_all(live_url, live_key, live_secret, "LIVE")
else:
    print("⚠️ No LIVE API keys found")

print("\n✅ Done — all accounts checked.")
