#!/usr/bin/env python3
"""Scalper Daily Report Generator — parses log for accurate trade data."""
import re, json, sys
from datetime import datetime, timedelta, timezone

LOG_FILE = "/root/.openclaw/workspace/frontend/scalper/state/scalper-v2-live-analyze.log"
REGIME_FILE = "/root/.openclaw/workspace/frontend/scalper/state/market-regime.json"

# Today in WIB (UTC+7)
WIB = timezone(timedelta(hours=7))
today = datetime.now(WIB).strftime("%Y-%m-%d")

# Parse CLOSE DETECTED from log
with open(LOG_FILE) as f:
    log = f.read()

# Pattern: 2026-05-19 01:39:41,483 [INFO] 🎯 CLOSE DETECTED: ZECUSDT | SL Hit 🛑 (market) | PnL=$+195.2911 (+0.72%) | source=grace_buffer
pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}).*CLOSE DETECTED: (\w+) \| (.+?) \| PnL=\$([+-]?[\d.]+) \(([+-]?[\d.]+)%\)'
closes = re.findall(pattern, log)

# Deduplicate (same timestamp + symbol + pnl = duplicate)
seen = set()
unique_closes = []
for c in closes:
    key = (c[0], c[1], c[3])
    if key not in seen:
        seen.add(key)
        unique_closes.append(c)

today_closes = [c for c in unique_closes if c[0].startswith(today)]
all_closes = unique_closes

def stats(trades):
    if not trades:
        return {"n": 0, "wins": 0, "losses": 0, "wr": 0, "pnl": 0, "best": None, "worst": None}
    wins = [t for t in trades if float(t[3].replace('+','')) > 0]
    losses = [t for t in trades if float(t[3].replace('+','')) <= 0]
    pnl = sum(float(t[3].replace('+','')) for t in trades)
    wr = len(wins)/len(trades)*100 if trades else 0
    
    best = max(trades, key=lambda t: float(t[3].replace('+',''))) if trades else None
    worst = min(trades, key=lambda t: float(t[3].replace('+',''))) if trades else None
    return {"n": len(trades), "wins": len(wins), "losses": len(losses), "wr": wr, "pnl": pnl, "best": best, "worst": worst}

today_s = stats(today_closes)
all_s = stats(all_closes)

# Regime
try:
    with open(REGIME_FILE) as f:
        regimes = json.load(f)
except:
    regimes = {}

regime_lines = []
vol_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "EXTREME": "🔴"}
trend_emoji = {"STRONG_UP": "🚀", "UP": "📈", "RANGE": "➡️", "DOWN": "📉", "STRONG_DOWN": "⬇️"}
for sym, r in regimes.items():
    if isinstance(r, dict) and "volatility" in r:
        v = vol_emoji.get(r.get("volatility",""), "⚪")
        t = trend_emoji.get(r.get("trend",""), "➡️")
        regime_lines.append(f"• {sym}: {v} {r['volatility']} | {t} {r['trend']}")

# Build report
lines = []
lines.append(f"📊 SCALPER DAILY REPORT — {today}")
lines.append("")
lines.append(f"💰 Balance: check /api/balance")
lines.append("")

if today_s["n"] > 0:
    lines.append(f"📈 TODAY: {today_s['n']} trades ({today_s['wins']}W/{today_s['losses']}L) | Win rate: {today_s['wr']:.0f}% | PnL: ${today_s['pnl']:+.2f}")
    if today_s["best"]:
        b = today_s["best"]
        lines.append(f"🏆 Best: {b[1]} ${float(b[3].replace('+','')):+.2f} ({b[4]}%)")
    if today_s["worst"]:
        w = today_s["worst"]
        lines.append(f"💀 Worst: {w[1]} ${float(w[3].replace('+','')):+.2f} ({w[4]}%)")
else:
    lines.append(f"📈 TODAY: No closed trades")

lines.append("")
lines.append(f"📊 ALL-TIME: {all_s['n']} trades ({all_s['wins']}W/{all_s['losses']}L) | Win rate: {all_s['wr']:.0f}% | Total PnL: ${all_s['pnl']:+.2f}")

if all_s["best"]:
    b = all_s["best"]
    lines.append(f"🏆 Best: {b[1]} ${float(b[3].replace('+','')):+.2f}")
if all_s["worst"]:
    w = all_s["worst"]
    lines.append(f"💀 Worst: {w[1]} ${float(w[3].replace('+','')):+.2f}")

lines.append("")
lines.append("🔮 Market Regime:")
for rl in regime_lines:
    lines.append(rl)

# Recent trades detail
lines.append("")
lines.append("📋 Recent trades:")
for c in all_closes[-8:]:
    e = '✅' if float(c[3].replace('+','')) > 0 else '❌'
    lines.append(f"  {e} {c[0]} {c[1]:10s} {c[2]:25s} ${float(c[3].replace('+','')):+.2f}")

print('\n'.join(lines))
