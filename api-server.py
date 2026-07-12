#!/usr/bin/env python3
"""
Nyx Scalper API — Lightweight data server for Vercel dashboard.
Serves state files + live balance/positions from Binance. ~30MB RAM.
"""
import json, os, time, hmac, hashlib, requests, urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

STATE_DIR = Path("/root/.openclaw/workspace/frontend/scalper/state")
PORT = 3778

# Parse env from bashrc
try:
    with open("/root/.bashrc") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, v = line.replace("export ", "").split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except:
    pass



# Cache for symbol precision info
_symbol_precision_cache = {}
_symbol_precision_ts = 0

def _get_symbol_precision(symbol):
    """Fetch and cache qty/price precision from Binance exchangeInfo."""
    global _symbol_precision_cache, _symbol_precision_ts
    now = time.time()
    # Refresh cache every hour
    if not _symbol_precision_cache or (now - _symbol_precision_ts) > 3600:
        try:
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            for s in data.get("symbols", []):
                _symbol_precision_cache[s["symbol"]] = {
                    "qty_precision": s.get("quantityPrecision", 2),
                    "price_precision": s.get("pricePrecision", 2),
                }
            _symbol_precision_ts = now
        except Exception as e:
            print(f"Warning: failed to fetch exchangeInfo: {e}")
    return _symbol_precision_cache.get(symbol, {"qty_precision": 2, "price_precision": 2})


def get_balance(api_key, api_secret, base_url):
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(
            f"{base_url}/fapi/v3/account?{q}&signature={sig}",
            headers={"X-MBX-APIKEY": api_key}, timeout=15,
        )
        d = r.json()
        return {
            "wallet": float(d.get("totalWalletBalance", 0)),
            "available": float(d.get("availableBalance", 0)),
            "unrealized": float(d.get("totalUnrealizedProfit", 0)),
        }
    except Exception as e:
        return {"error": str(e), "wallet": 0, "available": 0, "unrealized": 0}


def get_positions(api_key, api_secret, base_url):
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=10000"
        sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = requests.get(
            f"{base_url}/fapi/v3/positionRisk?{q}&signature={sig}",
            headers={"X-MBX-APIKEY": api_key}, timeout=15,
        )
        positions = []
        for p in r.json():
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                positions.append({
                    "symbol": p.get("symbol", ""),
                    "side": "LONG" if amt > 0 else "SHORT",
                    "size": abs(amt),
                    "entry": float(p.get("entryPrice", 0)),
                    "markPrice": float(p.get("markPrice", 0)),
                    "unrealizedProfit": float(p.get("unRealizedProfit", 0)),
                    "leverage": p.get("leverage", ""),
                })
        return positions
    except Exception as e:
        return {"error": str(e)}


def signed_request_live(method, endpoint, params, api_key, api_secret, base_url):
    """Sign and send a request to Binance."""
    ts = int(time.time() * 1000)
    query = f"timestamp={ts}&recvWindow=10000"
    for k, v in params.items():
        query += f"&{k}={v}"
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    try:
        if method == "POST":
            r = requests.post(url, headers=headers, timeout=15)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=15)
        else:
            r = requests.get(url, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        return {"code": -9999, "msg": str(e)}


class NyxAPI(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        path = self.path.split("?")[0].rstrip("/")

        if path == "/api/state":
            self._serve_state()
        elif path == "/api/config":
            self._serve_config()
        elif path == "/api/balance":
            self._serve_balance()
        elif path == "/api/positions":
            self._serve_positions()
        elif path == "/api/errors":
            self._serve_errors()
        elif path == "/api/regime":
            self._serve_regime()
        elif path == "/api/signals":
            self._serve_signals()
        elif path == "/api/health":
            self._serve_health()
        else:
            self.wfile.write(json.dumps({"error": "not found"}).encode())

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

        path = self.path.split("?")[0].rstrip("/")

        if path == "/api/execute-signal":
            self._handle_execute_signal()
        else:
            self.wfile.write(json.dumps({"error": "not found"}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_execute_signal(self):
        """Execute a signal manually — place real order on Binance."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except Exception as e:
            self.wfile.write(json.dumps({"ok": False, "error": f"Invalid request: {e}"}).encode())
            return

        symbol = data.get("symbol", "")
        direction = data.get("direction", "").upper()
        price = float(data.get("price", 0))
        tp = data.get("tp")
        sl = data.get("sl")
        confidence = data.get("confidence", "C3")

        # Validate
        if not symbol or direction not in ("LONG", "SHORT") or price <= 0:
            self.wfile.write(json.dumps({"ok": False, "error": "Missing symbol, direction, or price"}).encode())
            return

        # Config
        leverage = int(os.environ.get("SCALPER_LEVERAGE", "15"))
        size_pct = float(os.environ.get("SCALPER_SIZE_PCT", "0.30"))

        # Confidence size multiplier
        c2_mult = {"C2": 0.5, "C3": 1.0, "C4": 1.5}.get(confidence, 1.0)

        # Get live balance
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_SECRET_KEY", "")
        base_url = "https://fapi.binance.com"

        if not api_key or not api_secret:
            self.wfile.write(json.dumps({"ok": False, "error": "Binance API keys not configured"}).encode())
            return

        # Get balance
        bal_data = get_balance(api_key, api_secret, base_url)
        available = bal_data.get("available", 0)
        if available <= 0:
            self.wfile.write(json.dumps({"ok": False, "error": f"No available balance: {bal_data}"}).encode())
            return

        # Fetch symbol precision from Binance exchangeInfo
        symbol_info = _get_symbol_precision(symbol)
        qty_precision = symbol_info["qty_precision"]
        price_precision = symbol_info["price_precision"]

        # Calculate position size
        trade_usd = available * size_pct * c2_mult
        raw_qty = trade_usd * leverage / price
        qty = max(round(raw_qty, qty_precision), 10**-qty_precision)
        qty_str = f"{qty:.{qty_precision}f}"
        notional = round(qty * price, 2)
        margin_used = round(notional / leverage, 2)

        if qty <= 0:
            self.wfile.write(json.dumps({"ok": False, "error": f"Calculated qty is 0 (avail=${available}, size={size_pct}, lev={leverage})"}).encode())
            return

        results = {"symbol": symbol, "direction": direction, "price": price}

        # 1. Set leverage
        lev_result = signed_request_live("POST", "/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage
        }, api_key, api_secret, base_url)
        results["leverage"] = lev_result

        # 2. Place market order
        side = "BUY" if direction == "LONG" else "SELL"
        order_result = signed_request_live("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": qty_str, "newOrderRespType": "RESULT",
        }, api_key, api_secret, base_url)
        results["order"] = order_result

        # Check if order succeeded
        if isinstance(order_result, dict) and order_result.get("code") and order_result["code"] < 0:
            self.wfile.write(json.dumps({"ok": False, "error": f"Order failed: {order_result}", "details": results}).encode())
            return

        executed_price = float(order_result.get("avgPrice", price))
        executed_qty = float(order_result.get("executedQty", qty))
        results["executed_price"] = executed_price
        results["executed_qty"] = executed_qty

        # 3. Place SL/TP
        close_side = "SELL" if direction == "LONG" else "BUY"
        if sl and float(sl) > 0:
            sl_result = signed_request_live("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": f"{float(sl):.{price_precision}f}", "quantity": f"{executed_qty:.{qty_precision}f}",
                "workingType": "CONTRACT_PRICE", "priceProtect": "true",
                "reduceOnly": "true",
            }, api_key, api_secret, base_url)
            results["sl_order"] = sl_result

        if tp and float(tp) > 0:
            tp_result = signed_request_live("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": f"{float(tp):.{price_precision}f}", "quantity": f"{executed_qty:.{qty_precision}f}",
                "workingType": "CONTRACT_PRICE", "priceProtect": "true",
                "reduceOnly": "true",
            }, api_key, api_secret, base_url)
            results["tp_order"] = tp_result

        # Log execution
        log_msg = f"MANUAL EXECUTE: {direction} {symbol} @ ${executed_price} qty={executed_qty} notional=${notional} margin=${margin_used} TP={tp} SL={sl}"
        log.info(log_msg) if hasattr(self, 'log') else print(log_msg)

        results["ok"] = True
        results["notional"] = notional
        results["margin_used"] = margin_used
        self.wfile.write(json.dumps(results).encode())

    def _serve_state(self):
        result = {"timestamp": time.time()}

        # Load LIVE state (primary — real money)
        live_state_file = STATE_DIR / "scalper-v2-live.json"
        if live_state_file.exists():
            state = json.loads(live_state_file.read_text())
            daily_gross = state.get("daily_pnl", 0)
            daily_fees = state.get("daily_fees", 0)
            state["daily_net"] = round(daily_gross - daily_fees, 2)
            result["live"] = state

        # Load TESTNET state (secondary — testing)
        testnet_state_file = STATE_DIR / "scalper-v2-testnet.json"
        if testnet_state_file.exists():
            state = json.loads(testnet_state_file.read_text())
            daily_gross = state.get("daily_pnl", 0)
            daily_fees = state.get("daily_fees", 0)
            state["daily_net"] = round(daily_gross - daily_fees, 2)
            result["testnet"] = state

        # Read trades from both live and testnet files
        trade_sources = [
            ("trades-v2-live.jsonl", "live"),
            ("trades-v2-live-analyze.jsonl", "live-analyze"),
            ("trades-v2-testnet.jsonl", "testnet"),
        ]
        all_trades = []
        # Per-mode and overall stats
        stats = {
            "live": {"pnl": 0.0, "fees": 0.0, "wins": 0, "losses": 0},
            "testnet": {"pnl": 0.0, "fees": 0.0, "wins": 0, "losses": 0},
            "total_pnl": 0.0, "total_fees": 0.0, "total_wins": 0, "total_losses": 0,
        }

        for filename, mode in trade_sources:
            trades_file = STATE_DIR / filename
            if not trades_file.exists():
                continue
            lines = trades_file.read_text().strip().split("\n")
            # Calculate stats from ALL trades
            for line in lines:
                try:
                    t = json.loads(line)
                    t["mode"] = mode
                    all_trades.append(t)
                    pnl_val = t.get("pnl", 0)
                    fee = t.get("fee", 0)
                    if isinstance(pnl_val, (int, float)):
                        stats["total_pnl"] += pnl_val
                        if pnl_val > 0:
                            stats["total_wins"] += 1
                            stats[mode]["wins"] += 1
                        elif pnl_val < 0:
                            stats["total_losses"] += 1
                            stats[mode]["losses"] += 1
                        stats[mode]["pnl"] += pnl_val
                    if isinstance(fee, (int, float)):
                        stats["total_fees"] += fee
                        stats[mode]["fees"] += fee
                except:
                    pass

        # Sort all trades by timestamp (newest first), keep last 100
        all_trades.sort(key=lambda t: t.get("ts", ""), reverse=True)
        result["trades"] = all_trades[:100]
        result["overall"] = {
            "total_pnl": round(stats["total_pnl"], 2),
            "total_fees": round(stats["total_fees"], 2),
            "total_net": round(stats["total_pnl"] - stats["total_fees"], 2),
            "total_wins": stats["total_wins"],
            "total_losses": stats["total_losses"],
            "total_trades": stats["total_wins"] + stats["total_losses"],
            "live": {
                "pnl": round(stats["live"]["pnl"], 2),
                "fees": round(stats["live"]["fees"], 2),
                "net": round(stats["live"]["pnl"] - stats["live"]["fees"], 2),
                "wins": stats["live"]["wins"],
                "losses": stats["live"]["losses"],
                "trades": stats["live"]["wins"] + stats["live"]["losses"],
            },
            "testnet": {
                "pnl": round(stats["testnet"]["pnl"], 2),
                "fees": round(stats["testnet"]["fees"], 2),
                "net": round(stats["testnet"]["pnl"] - stats["testnet"]["fees"], 2),
                "wins": stats["testnet"]["wins"],
                "losses": stats["testnet"]["losses"],
                "trades": stats["testnet"]["wins"] + stats["testnet"]["losses"],
            },
        }

        live_log = STATE_DIR / "scalper-v2-live.log"
        live_signals = 0
        if live_log.exists():
            for line in live_log.read_text().split("\n"):
                if "SIGNAL" in line: live_signals += 1
        result["liveSignals"] = live_signals

        # Count errors from all log files
        total_errors = 0
        last_activity = ""
        for log_path in [
            STATE_DIR / "scalper-v2-live.log",
            STATE_DIR / "scalper-v2-testnet.log",
        ]:
            if log_path.exists():
                lines = log_path.read_text().strip().split("\n")
                total_errors += sum(
                    1 for l in lines
                    if "[ERROR]" in l and "timed out during opening" not in l
                )
                if lines and lines[-1].strip():
                    last_activity = lines[-1][:19]
        result["errors"] = total_errors
        result["lastActivity"] = last_activity

        self.wfile.write(json.dumps(result).encode())

    def _serve_balance(self):
        result = {}
        result["testnet"] = get_balance(
            os.environ.get("BINANCE_TESTNET_API_KEY", ""),
            os.environ.get("BINANCE_TESTNET_SECRET_KEY", ""),
            "https://demo-fapi.binance.com",
        )
        result["live"] = get_balance(
            os.environ.get("BINANCE_API_KEY", ""),
            os.environ.get("BINANCE_SECRET_KEY", ""),
            "https://fapi.binance.com",
        )
        result["timestamp"] = time.time()
        self.wfile.write(json.dumps(result).encode())

    def _serve_positions(self):
        result = {}
        result["testnet"] = get_positions(
            os.environ.get("BINANCE_TESTNET_API_KEY", ""),
            os.environ.get("BINANCE_TESTNET_SECRET_KEY", ""),
            "https://demo-fapi.binance.com",
        )
        result["live"] = get_positions(
            os.environ.get("BINANCE_API_KEY", ""),
            os.environ.get("BINANCE_SECRET_KEY", ""),
            "https://fapi.binance.com",
        )
        result["timestamp"] = time.time()
        self.wfile.write(json.dumps(result).encode())

    def _serve_regime(self):
        """Return current market regime data."""
        result = {"timestamp": time.time()}
        regime_file = STATE_DIR / "market-regime.json"
        if regime_file.exists():
            try:
                result["regimes"] = json.loads(regime_file.read_text())
            except:
                result["regimes"] = {}
        else:
            result["regimes"] = {}
        # Also include learning report summary
        report_file = STATE_DIR / "learning-report.json"
        if report_file.exists():
            try:
                report = json.loads(report_file.read_text())
                result["learning"] = {
                    "summary": report.get("summary", {}),
                    "suggestions": report.get("suggestions", []),
                    "indicators": report.get("indicators", {}),
                }
            except:
                pass
        self.wfile.write(json.dumps(result).encode())

    def _serve_config(self):
        """Return active bot configuration."""
        import subprocess
        result = {"timestamp": time.time()}

        # Read service config
        try:
            service_file = "/etc/systemd/system/nyx-scalper.service"
            with open(service_file) as f:
                svc = f.read()

            # Parse mode
            mode = "unknown"
            for line in svc.split("\n"):
                if "Environment=SCALPER_MODE=" in line:
                    mode = line.split("=")[-1].strip()

            # Parse dry run
            dry_run = False
            for line in svc.split("\n"):
                if "Environment=SCALPER_DRY_RUN=true" in line:
                    dry_run = True

            result["mode"] = mode
            result["dryRun"] = dry_run
            result["isTestnet"] = mode == "testnet"
            result["isLive"] = mode == "live"
            result["canTrade"] = not dry_run

        except Exception as e:
            result["error"] = str(e)

        # Read active pairs from the bot
        result["pairs"] = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT",
            "XRPUSDT", "DOGEUSDT", "BNBUSDT",
            "SUIUSDT", "XAGUSDT", "ZECUSDT", "CLUSDT",
        ]

        # Service uptime
        try:
            uptime_out = subprocess.check_output(
                ["systemctl", "show", "nyx-scalper.service", "--property=ActiveEnterTimestamp"],
                text=True
            ).strip()
            result["serviceStarted"] = uptime_out.split("=", 1)[-1].strip()
        except:
            result["serviceStarted"] = "unknown"

        self.wfile.write(json.dumps(result).encode())

    def _serve_errors(self):
        """Return recent error log entries from ALL log files."""
        result = {"errors": [], "total": 0, "timestamp": time.time()}

        # Scan all log files
        log_files = [
            STATE_DIR / "scalper-v2-live.log",
            STATE_DIR / "scalper-v2-live-analyze.log",
            STATE_DIR / "scalper-v2-testnet.log",
        ]

        all_error_lines = []
        for log_file in log_files:
            if log_file.exists():
                lines = log_file.read_text().strip().split("\n")
                for line in lines:
                    if "[ERROR]" in line and "timed out during opening" not in line:
                        all_error_lines.append(line)

        result["total"] = len(all_error_lines)
        # Return last 50 errors, parsed
        for line in all_error_lines[-50:]:
            try:
                parts = line.split(" [ERROR] ", 1)
                ts = parts[0].strip() if parts else ""
                msg = parts[1].strip() if len(parts) > 1 else line
                result["errors"].append({"ts": ts, "msg": msg})
            except:
                result["errors"].append({"ts": "", "msg": line})

        self.wfile.write(json.dumps(result).encode())

    def _serve_signals(self):
        """Return recent signals and signal rejections from all log files."""
        import re as _re
        result = {"signals": [], "rejections": [], "timestamp": time.time()}

        log_files = [
            STATE_DIR / "scalper-v2-live.log",
            STATE_DIR / "scalper-v2-live-analyze.log",
            STATE_DIR / "scalper-v2-testnet.log",
        ]

        for log_file in log_files:
            if not log_file.exists():
                continue
            mode = "live-analyze" if "live-analyze" in log_file.name else "testnet"
            try:
                lines = log_file.read_text().strip().split("\n")
                recent_lines = lines[-1000:] if len(lines) > 1000 else lines

                # Parse signal blocks — a signal spans multiple lines
                i = 0
                while i < len(recent_lines):
                    line = recent_lines[i]

                    if "🎯 SIGNAL:" in line:
                        try:
                            # Extract timestamp: "2026-05-21 13:30:03,651 [INFO] ..."
                            ts_match = _re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                            ts = ts_match.group(1) if ts_match else ""

                            # Parse: 🎯 SIGNAL: SHORT XRPUSDT @ $1.3737 (C2)
                            sig_match = _re.search(
                                r'(LONG|SHORT)\s+([A-Z]+USDT)\s+@\s+\$([\d,.]+)\s+\((C[234])\)',
                                line
                            )
                            if sig_match:
                                direction = sig_match.group(1)
                                symbol = sig_match.group(2)
                                price = float(sig_match.group(3).replace(',', ''))
                                confidence = sig_match.group(4)
                            else:
                                direction = ""
                                symbol = ""
                                price = 0
                                confidence = ""

                            # Read context lines (next 5 lines max)
                            tp = None
                            sl = None
                            reason = ""
                            strategy = ""
                            rsi_5 = None
                            vol_ratio = None
                            trend = ""
                            is_dry_run = False

                            for j in range(i + 1, min(i + 8, len(recent_lines))):
                                ctx = recent_lines[j]
                                # Stop at next log entry that's not a continuation
                                if _re.match(r'\d{4}-\d{2}-\d{2}.*\[INFO\] [^ ]', ctx) and "SL=" not in ctx and "Reason:" not in ctx and "Trend:" not in ctx and "Adaptive:" not in ctx:
                                    break

                                # SL=$1.38 TP=$1.36
                                if "SL=" in ctx:
                                    sl_m = _re.search(r'SL=\$([\d,.]+)', ctx)
                                    if sl_m: sl = float(sl_m.group(1).replace(',', ''))
                                    tp_m = _re.search(r'TP=\$([\d,.]+)', ctx)
                                    if tp_m: tp = float(tp_m.group(1).replace(',', ''))

                                # Trend: RANGE RSI=33.54 Vol=0.83x
                                if "Trend:" in ctx:
                                    rsi_m = _re.search(r'RSI=([\d.]+)', ctx)
                                    if rsi_m: rsi_5 = float(rsi_m.group(1))
                                    vol_m = _re.search(r'Vol=([\d.]+)x', ctx)
                                    if vol_m: vol_ratio = float(vol_m.group(1))
                                    trend_m = _re.search(r'Trend:\s*(\w+)', ctx)
                                    if trend_m: trend = trend_m.group(1)

                                # Reason: Momentum: ...
                                if "Reason:" in ctx:
                                    reason = ctx.split("Reason:")[1].strip() if "Reason:" in ctx else ""
                                    # Detect strategy
                                    if "Reversal" in reason:
                                        strategy = "A: Reversal"
                                    elif "Trend Follow" in reason or "trend follow" in reason.lower():
                                        strategy = "B: Trend Follow"
                                    elif "Mean Revert" in reason or "Mean Reversion" in reason:
                                        strategy = "C: Mean Revert"
                                    elif "Breakout" in reason:
                                        strategy = "D: Breakout"
                                    elif "Momentum" in reason:
                                        strategy = "E: Momentum"
                                    else:
                                        strategy = "Unknown"

                                if "DRY RUN" in ctx:
                                    is_dry_run = True

                            # Format timestamp as ISO (already has seconds)
                            iso_ts = ts if ts else ""

                            result["signals"].append({
                                "ts": iso_ts,
                                "symbol": symbol,
                                "direction": direction,
                                "price": price,
                                "confidence": confidence,
                                "reason": reason,
                                "strategy": strategy,
                                "tp": tp,
                                "sl": sl,
                                "rsi_5": rsi_5,
                                "vol_ratio": vol_ratio,
                                "dry_run": is_dry_run,
                                "mode": mode,
                                "trend": trend,
                                "event_type": "SIGNAL",
                            })
                        except Exception as e:
                            pass

                    i += 1

            except Exception as e:
                pass

        # Parse rejection events from JSONL files
        for events_file in STATE_DIR.glob("events-v2-*.jsonl"):
            try:
                elines = events_file.read_text().strip().split("\n")
                for eline in elines[-200:]:
                    try:
                        evt = json.loads(eline)
                        if evt.get("event") == "SIGNAL_REJECTION":
                            d = evt.get("data", {})
                            result["rejections"].append({
                                "ts": evt.get("ts", ""),
                                "symbol": d.get("symbol", ""),
                                "trend_15m": d.get("trend_15m", ""),
                                "rsi_5": d.get("rsi_5", 0),
                                "rsi_15": d.get("rsi_15", 0),
                                "vol_ratio": d.get("vol_ratio", 0),
                                "reasons": d.get("reasons", []),
                                "event_type": "SIGNAL_REJECTION",
                            })
                    except:
                        pass
            except:
                pass

        # Sort signals newest first, dedupe by ts+symbol
        seen = set()
        unique_signals = []
        for s in sorted(result["signals"], key=lambda x: x["ts"], reverse=True):
            key = f"{s['ts']}_{s['symbol']}_{s['direction']}"
            if key not in seen:
                seen.add(key)
                unique_signals.append(s)
        result["signals"] = unique_signals[:50]
        result["rejections"].sort(key=lambda x: x.get("ts", ""), reverse=True)
        result["rejections"] = result["rejections"][:100]

        self.wfile.write(json.dumps(result).encode())

    def _serve_health(self):
        """Run connectivity tests and return results."""
        result = {"timestamp": time.time(), "tests": []}
        
        def add_test(name, status, detail=""):
            result["tests"].append({"name": name, "status": status, "detail": detail})
        
        # 1. LIVE REST — Ping
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/ping", timeout=5)
            r.raise_for_status()
            add_test("LIVE REST Ping", "ok")
        except Exception as e:
            add_test("LIVE REST Ping", "fail", str(e))
        
        # 2. LIVE REST — Account
        try:
            key = os.environ.get("BINANCE_API_KEY", "")
            secret = os.environ.get("BINANCE_SECRET_KEY", "")
            ts = int(time.time() * 1000)
            q = f"timestamp={ts}&recvWindow=10000"
            sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f"https://fapi.binance.com/fapi/v3/account?{q}&signature={sig}",
                             headers={"X-MBX-APIKEY": key}, timeout=10)
            d = r.json()
            if "code" in d:
                add_test("LIVE REST Account", "fail", f"error {d['code']}: {d['msg']}")
            else:
                add_test("LIVE REST Account", "ok", f"wallet=${float(d.get('totalWalletBalance',0)):.2f}")
        except Exception as e:
            add_test("LIVE REST Account", "fail", str(e))
        
        # 3. TESTNET REST — Ping
        try:
            r = requests.get("https://demo-fapi.binance.com/fapi/v1/ping", timeout=5)
            r.raise_for_status()
            add_test("TESTNET REST Ping", "ok")
        except Exception as e:
            add_test("TESTNET REST Ping", "fail", str(e))
        
        # 4. TESTNET REST — Account
        try:
            key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
            secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
            ts = int(time.time() * 1000)
            q = f"timestamp={ts}&recvWindow=10000"
            sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f"https://demo-fapi.binance.com/fapi/v3/account?{q}&signature={sig}",
                             headers={"X-MBX-APIKEY": key}, timeout=10)
            d = r.json()
            if "code" in d:
                add_test("TESTNET REST Account", "fail", f"error {d['code']}: {d['msg']}")
            else:
                add_test("TESTNET REST Account", "ok", f"wallet=${float(d.get('totalWalletBalance',0)):.2f}")
        except Exception as e:
            add_test("TESTNET REST Account", "fail", str(e))
        
        # 5. Regime data freshness
        regime_file = STATE_DIR / "market-regime.json"
        if regime_file.exists():
            try:
                regimes = json.loads(regime_file.read_text())
                pairs = list(regimes.keys())
                # Check youngest timestamp
                newest = max(
                    (datetime.fromisoformat(d["ts"]) for d in regimes.values() if "ts" in d),
                    default=None
                )
                if newest:
                    from datetime import datetime as dt, timezone
                    age = (dt.now(timezone.utc) - newest).total_seconds() / 60
                    if age < 30:
                        add_test("Regime Data", "ok", f"{len(pairs)} pairs, {age:.0f} min ago")
                    else:
                        add_test("Regime Data", "warn", f"{len(pairs)} pairs, stale ({age:.0f} min ago)")
                else:
                    add_test("Regime Data", "warn", f"{len(pairs)} pairs, no timestamps")
            except Exception as e:
                add_test("Regime Data", "fail", str(e))
        else:
            add_test("Regime Data", "fail", "file not found")
        
        # 6. Bot log freshness
        log_file = STATE_DIR / "scalper-v2-live.log"
        if log_file.exists():
            mtime = log_file.stat().st_mtime
            age = time.time() - mtime
            if age < 600:
                add_test("Bot Log", "ok", f"last write {age:.0f}s ago")
            else:
                add_test("Bot Log", "warn", f"stale ({age/60:.0f} min ago)")
        else:
            add_test("Bot Log", "fail", "not found")
        
        # 7. Systemd services
        import subprocess
        for svc in ["nyx-scalper", "nyx-scalper-api"]:
            try:
                r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
                status = r.stdout.strip()
                add_test(f"Service {svc}", "ok" if status == "active" else "fail", status)
            except Exception as e:
                add_test(f"Service {svc}", "fail", str(e))
        
        # Summary
        passed = sum(1 for t in result["tests"] if t["status"] == "ok")
        total = len(result["tests"])
        result["summary"] = {"passed": passed, "total": total, "all_ok": passed == total}
        
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), NyxAPI)
    print(f"Nyx API running on :{PORT}")
    server.serve_forever()
