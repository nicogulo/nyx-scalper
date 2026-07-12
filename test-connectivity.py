#!/usr/bin/env python3
"""
Nyx Scalper — Full Connectivity Test
Tests ALL endpoints: REST API, WebSocket data flow, for both LIVE and TESTNET.
Run: python3 test-connectivity.py
"""
import asyncio, json, os, time, hmac, hashlib, requests, sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("❌ websockets not installed"); sys.exit(1)

# Load env from bashrc
try:
    with open("/root/.bashrc") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, v = line.replace("export ", "").split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except:
    pass

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0

def ok(msg):
    global passed; passed += 1
    print(f"  {GREEN}✅ {msg}{RESET}")

def fail(msg):
    global failed; failed += 1
    print(f"  {RED}❌ {msg}{RESET}")

def warn(msg):
    print(f"  {YELLOW}⚠️  {msg}{RESET}")

def section(title):
    print(f"\n{BOLD}{CYAN}{'='*60}\n  {title}\n{'='*60}{RESET}")

# ──────────────────────────────────────────────
#  REST API Tests
# ──────────────────────────────────────────────
def test_rest_live():
    section("📡 LIVE — REST API")
    base = "https://fapi.binance.com"
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_SECRET_KEY", "")
    
    if not key:
        fail("BINANCE_API_KEY not set"); return
    
    # 1. Ping
    try:
        r = requests.get(f"{base}/fapi/v1/ping", timeout=10)
        r.raise_for_status()
        ok(f"Ping: {r.json()}")
    except Exception as e:
        fail(f"Ping: {e}"); return

    # 2. Server time
    try:
        r = requests.get(f"{base}/fapi/v1/time", timeout=10)
        ts = r.json()["serverTime"]
        ok(f"Server time: {ts}")
    except Exception as e:
        fail(f"Server time: {e}")

    # 3. Exchange info
    try:
        r = requests.get(f"{base}/fapi/v1/exchangeInfo", timeout=10)
        symbols = [s["symbol"] for s in r.json()["symbols"][:5]]
        ok(f"Exchange info: {len(r.json()['symbols'])} symbols (first 5: {symbols})")
    except Exception as e:
        fail(f"Exchange info: {e}")

    # 4. Klines
    try:
        r = requests.get(f"{base}/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=2", timeout=10)
        data = r.json()
        ok(f"Klines 5m: {len(data)} candles, close={data[-1][4]}")
    except Exception as e:
        fail(f"Klines: {e}")

    # 5. Account balance (signed)
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{base}/fapi/v3/account?{q}&signature={sig}",
                         headers={"X-MBX-APIKEY": key}, timeout=15)
        d = r.json()
        if "code" in d:
            fail(f"Account: error code {d['code']} — {d['msg']}")
        else:
            ok(f"Account: wallet=${float(d.get('totalWalletBalance',0)):.2f} "
                f"available=${float(d.get('availableBalance',0)):.2f} "
                f"unrealized=${float(d.get('totalUnrealizedProfit',0)):.2f}")
    except Exception as e:
        fail(f"Account: {e}")

    # 6. Positions (signed)
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{base}/fapi/v3/positionRisk?{q}&signature={sig}",
                         headers={"X-MBX-APIKEY": key}, timeout=15)
        d = r.json()
        if isinstance(d, list):
            active = [p for p in d if float(p.get("positionAmt", 0)) != 0]
            ok(f"Positions: {len(d)} total, {len(active)} active")
        else:
            fail(f"Positions: {d.get('msg', d)}")
    except Exception as e:
        fail(f"Positions: {e}")

    # 7. Mark price
    try:
        r = requests.get(f"{base}/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=10)
        d = r.json()
        ok(f"Mark price: BTCUSDT mark={d['markPrice']} funding={d['lastFundingRate']}")
    except Exception as e:
        fail(f"Mark price: {e}")


def test_rest_testnet():
    section("📡 TESTNET — REST API")
    base = "https://demo-fapi.binance.com"
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
    
    if not key:
        warn("BINANCE_TESTNET_API_KEY not set — skipping testnet"); return
    
    # 1. Ping
    try:
        r = requests.get(f"{base}/fapi/v1/ping", timeout=10)
        r.raise_for_status()
        ok(f"Ping: {r.json()}")
    except Exception as e:
        fail(f"Ping: {e}"); return

    # 2. Klines
    try:
        r = requests.get(f"{base}/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=2", timeout=10)
        data = r.json()
        ok(f"Klines 5m: close={data[-1][4]}")
    except Exception as e:
        fail(f"Klines: {e}")

    # 3. Account balance
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{base}/fapi/v3/account?{q}&signature={sig}",
                         headers={"X-MBX-APIKEY": key}, timeout=15)
        d = r.json()
        if "code" in d:
            fail(f"Account: error {d['code']} — {d['msg']}")
        else:
            ok(f"Account: wallet=${float(d.get('totalWalletBalance',0)):.2f} "
                f"available=${float(d.get('availableBalance',0)):.2f}")
    except Exception as e:
        fail(f"Account: {e}")

    # 4. Positions
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{base}/fapi/v3/positionRisk?{q}&signature={sig}",
                         headers={"X-MBX-APIKEY": key}, timeout=15)
        d = r.json()
        if isinstance(d, list):
            active = [p for p in d if float(p.get("positionAmt", 0)) != 0]
            ok(f"Positions: {len(d)} total, {len(active)} active")
        else:
            fail(f"Positions: {d.get('msg', d)}")
    except Exception as e:
        fail(f"Positions: {e}")


# ──────────────────────────────────────────────
#  WebSocket Tests — verify ACTUAL DATA FLOW
# ──────────────────────────────────────────────
async def test_ws_live():
    section("🔌 LIVE — WebSocket (Data Flow Verification)")
    base = "wss://fstream.binance.com"
    
    # 1. Kline 5m (market endpoint)
    try:
        url = f"{base}/market/ws/btcusdt@kline_5m"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            k = d.get("k", {})
            ok(f"Kline 5m: {k.get('s')} close={k.get('c')} closed={k.get('x')} "
                f"(event={d.get('e')}, interval={k.get('i')})")
    except asyncio.TimeoutError:
        fail("Kline 5m: TIMEOUT — connected but NO DATA received (10s)")
    except Exception as e:
        fail(f"Kline 5m: {e}")

    # 2. Kline 15m
    try:
        url = f"{base}/market/ws/btcusdt@kline_15m"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            k = json.loads(msg).get("k", {})
            ok(f"Kline 15m: {k.get('s')} close={k.get('c')} closed={k.get('x')}")
    except asyncio.TimeoutError:
        fail("Kline 15m: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"Kline 15m: {e}")

    # 3. BookTicker (public endpoint)
    try:
        url = f"{base}/public/ws/btcusdt@bookTicker"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            ok(f"BookTicker: bid={d.get('b')} ask={d.get('a')}")
    except asyncio.TimeoutError:
        fail("BookTicker: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"BookTicker: {e}")

    # 4. Depth (public endpoint)
    try:
        url = f"{base}/public/ws/btcusdt@depth10@100ms"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            bids, asks = len(d.get("b", [])), len(d.get("a", []))
            ok(f"Depth: {bids} bids, {asks} asks")
    except asyncio.TimeoutError:
        fail("Depth: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"Depth: {e}")

    # 5. Mark Price (market endpoint)
    try:
        url = f"{base}/market/ws/btcusdt@markPrice"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            ok(f"MarkPrice: {d.get('s')} mark={d.get('p')} funding={d.get('r')}")
    except asyncio.TimeoutError:
        fail("MarkPrice: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"MarkPrice: {e}")

    # 6. AggTrade (market endpoint)
    try:
        url = f"{base}/market/ws/btcusdt@aggTrade"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            ok(f"AggTrade: {d.get('s')} price={d.get('p')} qty={d.get('q')}")
    except asyncio.TimeoutError:
        fail("AggTrade: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"AggTrade: {e}")

    # 7. User Stream — create listenKey + connect (private endpoint)
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_SECRET_KEY", "")
    if key:
        try:
            # Create listenKey via REST
            r = requests.post("https://fapi.binance.com/fapi/v1/listenKey",
                              headers={"X-MBX-APIKEY": key}, timeout=10)
            d = r.json()
            if "listenKey" not in d:
                fail(f"ListenKey: {d.get('msg', d)}")
            else:
                lk = d["listenKey"]
                # Connect to private WS
                url = f"{base}/private/ws?listenKey={lk}"
                async with websockets.connect(url, ping_interval=20) as ws:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    ok(f"User Stream: listenKey created, connected, received event")
                    # Delete listenKey (cleanup)
                    requests.delete("https://fapi.binance.com/fapi/v1/listenKey",
                                    headers={"X-MBX-APIKEY": key}, timeout=5)
        except asyncio.TimeoutError:
            warn("User Stream: connected but no event in 10s (normal if no orders)")
        except Exception as e:
            fail(f"User Stream: {e}")
    else:
        warn("User Stream: skipped (no API key)")

    # 8. Legacy URL — should FAIL (verify migration)
    try:
        url = f"{base}/ws/btcusdt@kline_5m"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            warn("Legacy URL still works — Binance hasn't fully decommissioned yet")
    except asyncio.TimeoutError:
        ok("Legacy URL /ws/ correctly returns NO data (decommissioned)")
    except Exception as e:
        ok(f"Legacy URL /ws/ rejected: {e}")

    # 9. Combined streams (market)
    try:
        url = f"{base}/market/stream?streams=btcusdt@kline_1m/ethusdt@kline_1m"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            d = json.loads(msg)
            stream = d.get("stream", "")
            data = d.get("data", {})
            k = data.get("k", {})
            ok(f"Combined streams: received from {stream} close={k.get('c')}")
    except asyncio.TimeoutError:
        fail("Combined streams: TIMEOUT — NO DATA")
    except Exception as e:
        fail(f"Combined streams: {e}")


async def test_ws_testnet():
    section("🔌 TESTNET — WebSocket (Data Flow Verification)")
    base = "wss://fstream.binancefuture.com"
    
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    if not key:
        warn("BINANCE_TESTNET_API_KEY not set — skipping testnet WS"); return

    # 1. Kline (market)
    try:
        url = f"{base}/market/ws/btcusdt@kline_5m"
        async with websockets.connect(url, ping_interval=20) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            k = json.loads(msg).get("k", {})
            ok(f"Kline 5m: {k.get('s')} close={k.get('c')}")
    except asyncio.TimeoutError:
        fail("Kline 5m: TIMEOUT — NO DATA (15s)")
    except Exception as e:
        fail(f"Kline 5m: {e}")

    # 2. BookTicker (public) — testnet public WS may be slow
    warn("BookTicker: skipped (testnet public WS often slow)")

    # 3. User Stream (private) — skip for testnet, usually idle
    warn("User Stream: skipped (testnet account usually idle)")


# ──────────────────────────────────────────────
#  Bot State & Systemd Tests
# ──────────────────────────────────────────────
def test_bot_state():
    section("🤖 Bot State & Services")
    state_dir = Path("/root/.openclaw/workspace/frontend/scalper/state")
    
    # 1. Systemd services
    import subprocess
    for svc in ["nyx-scalper", "nyx-scalper-api"]:
        try:
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
            status = r.stdout.strip()
            if status == "active":
                ok(f"systemd {svc}: {status}")
            else:
                fail(f"systemd {svc}: {status}")
        except Exception as e:
            fail(f"systemd {svc}: {e}")

    # 2. Regime file
    regime_file = state_dir / "market-regime.json"
    if regime_file.exists():
        try:
            data = json.loads(regime_file.read_text())
            pairs = list(data.keys())
            ok(f"Regime file: {len(pairs)} pairs ({', '.join(pairs[:5])}...)")
            # Check freshness
            for sym, d in data.items():
                ts = d.get("ts", "")
                if ts:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(ts)
                        age = (datetime.now(dt.tzinfo) - dt).total_seconds() / 60
                        if age < 30:
                            ok(f"  {sym}: fresh ({age:.0f} min ago)")
                        else:
                            warn(f"  {sym}: stale ({age:.0f} min ago)")
                    except:
                        pass
        except Exception as e:
            fail(f"Regime file: parse error — {e}")
    else:
        fail("Regime file: not found")

    # 3. API server responding
    try:
        r = requests.get("http://localhost:3778/api/regime", timeout=5)
        d = r.json()
        ok(f"API /api/regime: {len(d.get('regimes',{}))} pairs")
    except Exception as e:
        fail(f"API server: {e}")

    # 4. API /balance
    try:
        r = requests.get("http://localhost:3778/api/balance", timeout=10)
        d = r.json()
        live = d.get("live", {})
        ok(f"API /api/balance: live wallet=${live.get('wallet',0):.2f}")
    except Exception as e:
        fail(f"API /balance: {e}")

    # 5. API /positions
    try:
        r = requests.get("http://localhost:3778/api/positions", timeout=10)
        d = r.json()
        ok(f"API /api/positions: {len(d.get('positions',[]))} positions")
    except Exception as e:
        fail(f"API /positions: {e}")

    # 6. Log file active
    log_file = state_dir / "scalper-v2-live-analyze.log"
    if log_file.exists():
        mtime = log_file.stat().st_mtime
        age = time.time() - mtime
        if age < 300:
            ok(f"Bot log: active (last write {age:.0f}s ago)")
        else:
            warn(f"Bot log: stale (last write {age/60:.0f} min ago)")
    else:
        fail("Bot log: not found")


# ──────────────────────────────────────────────
#  Run All Tests
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{BOLD}{'🔧 NYX SCALPER — FULL CONNECTIVITY TEST':^60}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  websockets version: {websockets.__version__}")
    
    # REST tests
    test_rest_live()
    test_rest_testnet()
    
    # WebSocket tests (async)
    async def run_all_ws():
        await test_ws_live()
        try:
            await asyncio.wait_for(test_ws_testnet(), timeout=30)
        except asyncio.TimeoutError:
            warn("TESTNET WS: overall timeout (30s) — testnet may be slow/unavailable")
    asyncio.run(run_all_ws())
    
    # Bot state
    test_bot_state()
    
    # Summary
    total = passed + failed
    print(f"\n{BOLD}{'='*60}{RESET}")
    if failed == 0:
        print(f"{BOLD}{GREEN}  🎉 ALL {passed}/{total} TESTS PASSED{RESET}")
    else:
        print(f"{BOLD}{RED}  ⚠️  {passed}/{total} PASSED, {failed} FAILED{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")
    
    sys.exit(1 if failed > 0 else 0)
