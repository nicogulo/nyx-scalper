#!/usr/bin/env python3
"""
Scalper Learning Engine V2
- Matches trade entries with closes to determine win/loss per trade
- Analyzes patterns in winning vs losing trades
- Generates actionable suggestions
- Writes learnings to learning-report.json
"""

import json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

STATE_DIR = Path(__file__).resolve().parent / "state"
TRADES_FILE = STATE_DIR / "trades-v2-testnet.jsonl"
REPORT_FILE = STATE_DIR / "learning-report.json"

def load_trades():
    if not TRADES_FILE.exists():
        return []
    trades = []
    for line in TRADES_FILE.read_text().strip().split("\n"):
        if line.strip():
            try:
                trades.append(json.loads(line))
            except:
                pass
    return trades

def match_trades(trades):
    """Match PLACED entries with their close ORDER_FILL to get complete trade PnL."""
    # Build list of trade entries
    entries = []
    for t in trades:
        if t.get("status") == "PLACED" and t.get("direction"):
            entries.append({
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "entry": t.get("entry", 0),
                "sl": t.get("sl", 0),
                "tp": t.get("tp", 0),
                "rr": t.get("rr", 0),
                "gross_rr": t.get("gross_rr", 0),
                "rsi": t.get("rsi", 0),
                "rsi_15m": t.get("rsi_15m", 0),
                "vol_ratio": t.get("vol_ratio", 0),
                "trend_15m": t.get("trend_15m", ""),
                "confidence": t.get("confidence", ""),
                "reason": t.get("reason", ""),
                "atr": t.get("atr", 0),
                "leverage": t.get("leverage", 0),
                "qty": t.get("qty", 0),
                "notional": t.get("notional", 0),
                "fee_cost": t.get("fee_cost", 0),
                "net_tp": t.get("net_tp", 0),
                "funding_rate": t.get("funding_rate", 0),
                "smart_money_bull": t.get("smart_money_bull", False),
                "smart_money_bear": t.get("smart_money_bear", False),
                "ts_placed": t.get("ts", ""),
                "close_pnl": None,
                "close_price": None,
                "close_type": None,
                "close_ts": None,
                "result": None,
            })

    # Match with close events (ORDER_FILL with non-zero pnl and opposite side)
    fills = []
    for t in trades:
        event = t.get("event")
        if event == "ORDER_FILL" and t.get("pnl", 0) != 0:
            fills.append(t)

    # Simple matching: pair entries with fills by symbol (FIFO)
    entry_queue = {}  # symbol -> [entries...]
    for e in entries:
        sym = e["symbol"]
        if sym not in entry_queue:
            entry_queue[sym] = []
        entry_queue[sym].append(e)

    for f in fills:
        sym = f.get("symbol", "")
        pnl = f.get("pnl", 0)
        side = f.get("side", "")
        price = f.get("price", 0)
        order_type = f.get("type", "")
        ts = f.get("ts", "")

        if sym in entry_queue and entry_queue[sym]:
            e = entry_queue[sym].pop(0)
            e["close_pnl"] = pnl
            e["close_price"] = price
            e["close_ts"] = ts

            # Determine close type
            if order_type == "STOP_MARKET":
                e["close_type"] = "SL"
            elif order_type == "TAKE_PROFIT_MARKET":
                e["close_type"] = "TP"
            else:
                # Market close — check proximity to SL vs TP
                if e["sl"] and e["tp"]:
                    dist_sl = abs(price - e["sl"])
                    dist_tp = abs(price - e["tp"])
                    e["close_type"] = "SL (market)" if dist_sl < dist_tp else "TP (market)"
                else:
                    e["close_type"] = "Market"

            e["result"] = "WIN" if pnl > 0 else "LOSS"

    return entries

def analyze():
    trades = load_trades()
    if not trades:
        report = {"status": "no_data", "msg": "No trades yet", "generated_at": datetime.now(timezone(timedelta(hours=7))).isoformat()}
        REPORT_FILE.write_text(json.dumps(report, indent=2))
        return report

    matched = match_trades(trades)
    completed = [t for t in matched if t["result"] is not None]
    open_trades = [t for t in matched if t["result"] is None]
    wins = [t for t in completed if t["result"] == "WIN"]
    losses = [t for t in completed if t["result"] == "LOSS"]

    total_pnl = sum(t["close_pnl"] for t in completed)
    win_pnl = sum(t["close_pnl"] for t in wins)
    loss_pnl = sum(t["close_pnl"] for t in losses)
    win_rate = len(wins) / max(len(completed), 1) * 100

    # ── Pattern Analysis ──
    
    # By direction
    longs = [t for t in completed if t["direction"] == "LONG"]
    short_longs = [t for t in completed if t["direction"] == "SHORT"]
    long_wins = [t for t in longs if t["result"] == "WIN"]
    short_wins = [t for t in short_longs if t["result"] == "WIN"]
    
    # By trend
    by_trend = {}
    for t in completed:
        trend = t.get("trend_15m", "UNKNOWN")
        if trend not in by_trend:
            by_trend[trend] = {"total": 0, "wins": 0, "pnl": 0}
        by_trend[trend]["total"] += 1
        if t["result"] == "WIN":
            by_trend[trend]["wins"] += 1
        by_trend[trend]["pnl"] += t["close_pnl"]

    # By confidence
    by_conf = {}
    for t in completed:
        conf = t.get("confidence", "?")
        if conf not in by_conf:
            by_conf[conf] = {"total": 0, "wins": 0, "pnl": 0}
        by_conf[conf]["total"] += 1
        if t["result"] == "WIN":
            by_conf[conf]["wins"] += 1
        by_conf[conf]["pnl"] += t["close_pnl"]

    # By symbol
    by_symbol = {}
    for t in completed:
        sym = t.get("symbol", "?")
        if sym not in by_symbol:
            by_symbol[sym] = {"total": 0, "wins": 0, "pnl": 0}
        by_symbol[sym]["total"] += 1
        if t["result"] == "WIN":
            by_symbol[sym]["wins"] += 1
        by_symbol[sym]["pnl"] += t["close_pnl"]

    # By close type
    by_close = {}
    for t in completed:
        ct = t.get("close_type", "?")
        if ct not in by_close:
            by_close[ct] = {"count": 0, "pnl": 0}
        by_close[ct]["count"] += 1
        by_close[ct]["pnl"] += t["close_pnl"]

    # RSI analysis (win vs loss)
    win_rsi = [t["rsi"] for t in wins if t["rsi"]]
    loss_rsi = [t["rsi"] for t in losses if t["rsi"]]
    win_vol = [t["vol_ratio"] for t in wins if t["vol_ratio"]]
    loss_vol = [t["vol_ratio"] for t in losses if t["vol_ratio"]]

    # ── Suggestions ──
    suggestions = []

    if len(completed) < 5:
        suggestions.append(f"⚠️ Only {len(completed)} completed trades — need 5+ for meaningful analysis")

    # Win rate analysis
    if win_rate < 40 and len(completed) >= 5:
        suggestions.append(f"📉 Win rate {win_rate:.0f}% — below 40%. Consider tightening entry filters (higher vol_ratio, stricter RSI)")
    elif win_rate > 70:
        suggestions.append(f"📈 Win rate {win_rate:.0f}% — solid! Focus on maintaining R:R consistency")

    # R:R actual vs expected
    if wins and losses:
        avg_win = sum(t["close_pnl"] for t in wins) / len(wins)
        avg_loss = abs(sum(t["close_pnl"] for t in losses) / len(losses))
        actual_rr = avg_win / max(avg_loss, 0.01)
        if actual_rr < 1.5:
            suggestions.append(f"📊 Actual R:R = {actual_rr:.1f} (avg win ${avg_win:.0f} / avg loss ${avg_loss:.0f}) — below 1.5 target. TP too tight or SL too wide?")
        else:
            suggestions.append(f"✅ Actual R:R = {actual_rr:.1f} — healthy edge")

    # Direction bias
    if len(longs) > 0 and len(short_longs) > 0:
        long_wr = len(long_wins) / len(longs) * 100
        short_wr = len(short_wins) / len(short_longs) * 100
        if long_wr > short_wr + 20:
            suggestions.append(f"🟢 LONG win rate {long_wr:.0f}% vs SHORT {short_wr:.0f}% — consider filtering weak SHORT signals")
        elif short_wr > long_wr + 20:
            suggestions.append(f"🔴 SHORT win rate {short_wr:.0f}% vs LONG {long_wr:.0f}% — consider filtering weak LONG signals")

    # Trend analysis
    for trend, data in by_trend.items():
        wr = data["wins"] / max(data["total"], 1) * 100
        if data["total"] >= 2 and wr < 40:
            suggestions.append(f"⚠️ {trend} trend trades: {data['wins']}/{data['total']} wins ({wr:.0f}%) PnL=${data['pnl']:+.2f} — consider skipping {trend} signals")

    # Volume analysis
    if win_vol and loss_vol:
        avg_win_vol = sum(win_vol) / len(win_vol)
        avg_loss_vol = sum(loss_vol) / len(loss_vol)
        if avg_loss_vol < avg_win_vol * 0.8:
            suggestions.append(f"📉 Losing trades avg vol={avg_loss_vol:.2f}x vs winning {avg_win_vol:.2f}x — raise vol_ratio threshold?")
    
    # RSI analysis
    if win_rsi and loss_rsi:
        avg_win_rsi = sum(win_rsi) / len(win_rsi)
        avg_loss_rsi = sum(loss_rsi) / len(loss_rsi)
        if abs(avg_loss_rsi - avg_win_rsi) > 10:
            suggestions.append(f"🔍 Win RSI avg={avg_win_rsi:.1f} vs Loss RSI avg={avg_loss_rsi:.1f} — RSI range matters for entry quality")

    # Fee impact
    if completed:
        total_fees = sum(t.get("fee_cost", 0) for t in matched if t.get("fee_cost"))
        if total_fees > 0:
            fee_pct = total_fees / max(abs(total_pnl), 0.01) * 100
            if fee_pct > 20:
                suggestions.append(f"💸 Fees = ${total_fees:.2f} ({fee_pct:.0f}% of PnL) — significant drag. Consider fewer trades or longer holds")

    # SL/TP analysis
    sl_closes = [t for t in completed if "SL" in (t.get("close_type") or "")]
    tp_closes = [t for t in completed if "TP" in (t.get("close_type") or "")]
    if sl_closes and tp_closes:
        suggestions.append(f"🎯 Close types: {len(tp_closes)} TP hits vs {len(sl_closes)} SL hits")

    # Symbol-specific
    for sym, data in by_symbol.items():
        wr = data["wins"] / max(data["total"], 1) * 100
        if data["total"] >= 2:
            suggestions.append(f"📊 {sym}: {data['wins']}/{data['total']} wins ({wr:.0f}%) PnL=${data['pnl']:+.2f}")

    if not suggestions:
        suggestions.append("✅ No issues detected — keep monitoring")

    # ── Build report ──
    report = {
        "generated_at": datetime.now(timezone(timedelta(hours=7))).isoformat(),
        "summary": {
            "total_trades": len(trades),
            "completed_trades": len(completed),
            "open_trades": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "win_pnl": round(win_pnl, 2),
            "loss_pnl": round(loss_pnl, 2),
            "longs": len(longs),
            "shorts": len(short_longs),
        },
        "patterns": {
            "by_trend": by_trend,
            "by_confidence": by_conf,
            "by_symbol": by_symbol,
            "by_close_type": by_close,
        },
        "indicators": {
            "win_rsi_avg": round(sum(win_rsi) / max(len(win_rsi), 1), 1),
            "loss_rsi_avg": round(sum(loss_rsi) / max(len(loss_rsi), 1), 1),
            "win_vol_avg": round(sum(win_vol) / max(len(win_vol), 1), 2),
            "loss_vol_avg": round(sum(loss_vol) / max(len(loss_vol), 1), 2),
        },
        "trades": [
            {
                "symbol": t["symbol"],
                "direction": t["direction"],
                "entry": t["entry"],
                "close_price": t.get("close_price"),
                "pnl": t.get("close_pnl"),
                "result": t["result"],
                "close_type": t.get("close_type"),
                "rsi": t["rsi"],
                "vol_ratio": t["vol_ratio"],
                "trend_15m": t["trend_15m"],
                "reason": t["reason"],
            }
            for t in matched
        ],
        "suggestions": suggestions,
    }

    REPORT_FILE.write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    report = analyze()
    print(json.dumps(report, indent=2))
