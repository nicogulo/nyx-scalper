#!/usr/bin/env python3
"""
Nyx Learning Engine — Analyzes trade history and outputs adaptive parameters.
Reads: trades-v2-*.jsonl
Writes: state/adaptive-config.json
Designed to run nightly via cron.
"""
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

STATE_DIR = Path("/root/.openclaw/workspace/frontend/scalper/state")
OUTPUT_FILE = STATE_DIR / "adaptive-config.json"
TRADE_FILES = sorted(STATE_DIR.glob("trades-v2-*.jsonl"))

# Timezone WIB
WIB = timezone(timedelta(hours=7))

# ── Load all trades ──────────────────────────────────────────────
def load_trades(min_trades=5):
    """Load completed trades with PnL data."""
    trades = []
    for f in TRADE_FILES:
        try:
            for line in open(f):
                d = json.loads(line.strip())
                # Must have PnL data (closed trade)
                if d.get("pnl") is not None:
                    trades.append(d)
                # Also include PLACED trades for signal quality analysis
                elif d.get("status") in ("PLACED", "FILLED"):
                    trades.append(d)
        except Exception as e:
            print(f"  ⚠️ Error reading {f.name}: {e}")
    print(f"📊 Loaded {len(trades)} trade records from {len(TRADE_FILES)} files")
    return trades


# ── Analysis ─────────────────────────────────────────────────────
def analyze(trades):
    """Run full analysis and return adaptive config."""
    report = {
        "timestamp": datetime.now(WIB).isoformat(),
        "total_trades": 0,
        "closed_trades": 0,
        "net_pnl": 0,
        "win_rate": 0,
        "insights": [],
        "actions": [],
    }

    # Separate closed vs placed
    closed = [t for t in trades if t.get("pnl") is not None]
    placed = [t for t in trades if t.get("status") in ("PLACED", "FILLED") and t.get("pnl") is None]
    
    report["total_trades"] = len(trades)
    report["closed_trades"] = len(closed)
    
    if len(closed) < min(5, len(trades)):
        report["insights"].append(f"⚠️ Only {len(closed)} closed trades — need more data for reliable stats")
    
    # ── Overall stats ──
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) < 0]
    breakeven = [t for t in closed if t.get("pnl", 0) == 0]
    net_pnl = sum(t.get("pnl", 0) for t in closed)
    total_fees = sum(t.get("fee_cost", 0) + t.get("exit_fee", 0) for t in closed)
    
    wr = len(wins) / len(closed) * 100 if closed else 0
    report["win_rate"] = round(wr, 1)
    report["net_pnl"] = round(net_pnl, 2)
    report["total_fees"] = round(total_fees, 2)
    report["wins"] = len(wins)
    report["losses"] = len(losses)
    report["breakeven"] = len(breakeven)

    # ── Adaptive config defaults ──
    config = {
        "version": "1.0",
        "updated": datetime.now(WIB).isoformat(),
        "global": {
            "vol_threshold": 2.0,       # Default min vol ratio
            "rsi_oversold": 35,
            "rsi_overbought": 65,
            "min_confidence": "C2",
            "max_trades_per_day": 10,
        },
        "pairs": {},      # Per-pair weights and params
        "directions": {},  # Per-direction bias
        "strategies": {},  # Per-strategy performance
        "disabled": [],    # Disabled strategies/pairs
    }

    # ── Per-pair analysis ──
    pair_stats = defaultdict(lambda: {"w": 0, "l": 0, "be": 0, "pnl": 0, "vol_win": [], "vol_loss": [], "trades": []})
    for t in closed:
        sym = t.get("symbol", "?")
        s = pair_stats[sym]
        pnl = t.get("pnl", 0)
        s["pnl"] += pnl
        vol = t.get("vol_ratio", 0)
        if pnl > 0:
            s["w"] += 1
            if vol: s["vol_win"].append(vol)
        elif pnl < 0:
            s["l"] += 1
            if vol: s["vol_loss"].append(vol)
        else:
            s["be"] += 1
        s["trades"].append(t)

    insights = []
    actions = []

    for sym, s in sorted(pair_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        n = s["w"] + s["l"] + s["be"]
        wr = s["w"] / n * 100 if n else 0
        avg_vol_win = sum(s["vol_win"]) / len(s["vol_win"]) if s["vol_win"] else 0
        avg_vol_loss = sum(s["vol_loss"]) / len(s["vol_loss"]) if s["vol_loss"] else 0

        # Weight: 0.0 (disabled) to 2.0 (boosted), default 1.0
        weight = 1.0
        pair_action = None

        if n >= 3:
            if wr >= 60 and s["pnl"] > 0:
                weight = 1.5  # Boost
                pair_action = "BOOST"
                insights.append(f"🟢 {sym}: {wr:.0f}% WR, +${s['pnl']:.2f} — BOOSTED (weight=1.5)")
            elif wr <= 25 or s["pnl"] < -50:
                weight = 0.3  # Reduce exposure
                pair_action = "REDUCE"
                insights.append(f"🔴 {sym}: {wr:.0f}% WR, ${s['pnl']:.2f} — REDUCED (weight=0.3)")
            elif wr == 0 and n >= 3:
                weight = 0.0  # Disable
                pair_action = "DISABLE"
                config["disabled"].append(sym)
                insights.append(f"⛔ {sym}: 0% WR across {n} trades — DISABLED")
            else:
                insights.append(f"⚪ {sym}: {wr:.0f}% WR, ${s['pnl']:.2f} — neutral")
        
        # Adaptive vol threshold per pair
        if s["vol_win"] and s["vol_loss"]:
            optimal_vol = (avg_vol_win + avg_vol_loss) / 2
            if optimal_vol > 1.5:
                pair_vol = round(optimal_vol, 1)
                insights.append(f"  📊 {sym}: vol threshold adjusted to {pair_vol}x (win avg={avg_vol_win:.1f}x, loss avg={avg_vol_loss:.1f}x)")
            else:
                pair_vol = 2.0
        else:
            pair_vol = 2.0

        config["pairs"][sym] = {
            "weight": weight,
            "win_rate": round(wr, 1),
            "pnl": round(s["pnl"], 2),
            "trades": n,
            "vol_threshold": pair_vol,
            "action": pair_action,
        }

    # ── Per-direction analysis ──
    for direction in ["LONG", "SHORT"]:
        dt = [t for t in closed if t.get("direction") == direction]
        if not dt:
            # Try from placed trades
            dt_placed = [t for t in placed if t.get("direction") == direction]
            config["directions"][direction] = {"weight": 1.0, "trades": len(dt_placed)}
            continue
        
        dw = [t for t in dt if t.get("pnl", 0) > 0]
        dpnl = sum(t.get("pnl", 0) for t in dt)
        dwr = len(dw) / len(dt) * 100 if dt else 0
        
        dir_weight = 1.0
        if len(dt) >= 5:
            if dwr >= 55 and dpnl > 0:
                dir_weight = 1.3
                insights.append(f"📈 {direction}: {dwr:.0f}% WR, +${dpnl:.2f} — BOOSTED")
            elif dwr <= 40 or dpnl < -30:
                dir_weight = 0.5
                insights.append(f"📉 {direction}: {dwr:.0f}% WR, ${dpnl:.2f} — REDUCED")
            else:
                insights.append(f"↔️ {direction}: {dwr:.0f}% WR, ${dpnl:.2f} — neutral")
        
        config["directions"][direction] = {
            "weight": dir_weight,
            "win_rate": round(dwr, 1),
            "pnl": round(dpnl, 2),
            "trades": len(dt),
        }

    # ── Per-strategy analysis ──
    strategy_names = {"TrendFollow": "B", "MeanRevert": "C", "Breakout": "D", "EMAMomentum": "E", "Reversal": "A"}
    for t in closed + placed:
        reason = t.get("reason", "")
        for name, code in strategy_names.items():
            if name in reason or reason.startswith(code):
                t["_strategy"] = code
                break
    
    strat_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0, "n": 0})
    for t in closed:
        strat = t.get("_strategy", "?")
        s = strat_stats[strat]
        s["n"] += 1
        pnl = t.get("pnl", 0)
        s["pnl"] += pnl
        if pnl > 0: s["w"] += 1
        elif pnl < 0: s["l"] += 1

    for strat, s in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["w"] / s["n"] * 100 if s["n"] else 0
        weight = 1.0
        if s["n"] >= 3:
            if wr <= 20 or s["pnl"] < -50:
                weight = 0.0
                config["disabled"].append(f"strategy:{strat}")
                insights.append(f"⛔ Strategy {strat}: {wr:.0f}% WR, ${s['pnl']:.2f} — DISABLED")
            elif wr >= 60:
                weight = 1.5
                insights.append(f"🟢 Strategy {strat}: {wr:.0f}% WR, +${s['pnl']:.2f} — BOOSTED")
            else:
                insights.append(f"⚪ Strategy {strat}: {wr:.0f}% WR, ${s['pnl']:.2f}")
        
        config["strategies"][strat] = {
            "weight": weight,
            "win_rate": round(wr, 1),
            "pnl": round(s["pnl"], 2),
            "trades": s["n"],
        }

    # ── Global vol threshold ──
    all_win_vols = [t.get("vol_ratio", 0) for t in wins if t.get("vol_ratio", 0) > 0]
    all_loss_vols = [t.get("vol_ratio", 0) for t in losses if t.get("vol_ratio", 0) > 0]
    
    if all_win_vols and all_loss_vols:
        avg_win_vol = sum(all_win_vols) / len(all_win_vols)
        avg_loss_vol = sum(all_loss_vols) / len(all_loss_vols)
        optimal = round((avg_win_vol + avg_loss_vol) / 2 + 0.2, 1)  # Add buffer
        
        if optimal > config["global"]["vol_threshold"]:
            old = config["global"]["vol_threshold"]
            config["global"]["vol_threshold"] = optimal
            insights.append(f"📊 Global vol threshold: {old}x → {optimal}x (win avg={avg_win_vol:.2f}x, loss avg={avg_loss_vol:.2f}x)")
    
    # ── Fee analysis ──
    if total_fees > 0 and net_pnl > 0:
        fee_ratio = total_fees / net_pnl * 100
        if fee_ratio > 100:
            insights.append(f"💸 Fee crisis: ${total_fees:.2f} = {fee_ratio:.0f}% of net PnL — consider fewer trades")
            config["global"]["max_trades_per_day"] = max(3, int(10 * net_pnl / total_fees))

    report["insights"] = insights
    report["actions"] = actions
    
    return report, config


# ── Send Telegram Report ─────────────────────────────────────────
def send_telegram(report, config):
    """Send learning report to Telegram."""
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    
    if not tg_token:
        # Try bashrc
        try:
            for line in open("/root/.bashrc"):
                line = line.strip()
                if line.startswith("export ") and "=" in line:
                    k, v = line.replace("export ", "").split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        except:
            pass
    
    if not tg_token:
        print("⚠️ No TELEGRAM_BOT_TOKEN — skipping Telegram report")
        return

    lines = [
        "🧠 <b>Nyx Learning Report</b>",
        f"📅 {datetime.now(WIB).strftime('%d %b %Y %H:%M')} WIB",
        "",
        f"📊 <b>Overall:</b> {report['closed_trades']} trades | WR: {report['win_rate']:.0f}%",
        f"💰 Net PnL: ${report['net_pnl']:.2f} | Fees: ${report.get('total_fees', 0):.2f}",
        "",
    ]

    # Direction summary
    for d in ["LONG", "SHORT"]:
        dd = config["directions"].get(d, {})
        w = dd.get("weight", 1.0)
        wr = dd.get("win_rate", 0)
        pnl = dd.get("pnl", 0)
        emoji = "🟢" if w > 1 else "🔴" if w < 0.5 else "⚪"
        lines.append(f"{emoji} <b>{d}</b>: {wr:.0f}% WR, ${pnl:.2f} (×{w})")

    lines.append("")

    # Top/Bottom pairs
    sorted_pairs = sorted(config["pairs"].items(), key=lambda x: x[1]["pnl"], reverse=True)
    for sym, p in sorted_pairs[:3]:
        if p["trades"] > 0:
            emoji = "🟢" if p["weight"] > 1 else "🔴" if p["weight"] < 0.5 else "⚪"
            lines.append(f"{emoji} <b>{sym}</b>: {p['win_rate']:.0f}% WR, ${p['pnl']:.2f} (×{p['weight']})")

    lines.append("")

    # Disabled items
    if config["disabled"]:
        lines.append("⛔ <b>Disabled:</b> " + ", ".join(config["disabled"]))

    # Adaptive changes
    vol = config["global"]["vol_threshold"]
    mtd = config["global"]["max_trades_per_day"]
    lines.append(f"")
    lines.append(f"⚙️ <b>Adaptive Config Applied:</b>")
    lines.append(f"  Vol threshold: {vol}x")
    lines.append(f"  Max trades/day: {mtd}")

    # Insights (top 5)
    if report["insights"]:
        lines.append("")
        lines.append("📋 <b>Insights:</b>")
        for ins in report["insights"][:8]:
            lines.append(f"  {ins}")

    msg = "\n".join(lines)
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": tg_chat,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=15)
        if r.status_code == 200:
            print(f"✅ Telegram report sent (msg {r.json()['result']['message_id']})")
        else:
            print(f"⚠️ Telegram error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Telegram failed: {e}")


# ── Main ─────────────────────────────────────────────────────────
def main():
    print(f"🧠 Nyx Learning Engine — {datetime.now(WIB).strftime('%Y-%m-%d %H:%M')} WIB")
    print("=" * 50)
    
    trades = load_trades()
    if not trades:
        print("⚠️ No trades found — nothing to learn from")
        return
    
    report, config = analyze(trades)
    
    # Save config
    with open(OUTPUT_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\n✅ Adaptive config saved to {OUTPUT_FILE}")
    
    # Print insights
    print(f"\n{'='*50}")
    print(f"📊 Results: {report['closed_trades']} trades | WR: {report['win_rate']:.0f}% | PnL: ${report['net_pnl']:.2f}")
    for ins in report["insights"]:
        print(f"  {ins}")
    
    # Send Telegram
    send_telegram(report, config)
    
    print(f"\n✅ Learning engine complete!")


if __name__ == "__main__":
    main()
