#!/usr/bin/env python3
"""
Market Regime Engine — Adaptive volatility & trend detection.
- Detects market regime per symbol (VOL level + trend strength)
- Outputs adaptive parameters for scalper entry filters
- Sends Telegram alerts on regime changes
"""

import os, json, time, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("scalper-v2")

# ── Config ──────────────────────────────────────────────────────────────────
STATE_DIR = Path(__file__).resolve().parent / "state"
REGIME_FILE = STATE_DIR / "market-regime.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Alert cooldown per symbol (don't spam)
_last_alert = {}  # {symbol_ts: timestamp}
ALERT_COOLDOWN = 300  # 5 min between same-symbol alerts

# ── Indicators ──────────────────────────────────────────────────────────────

def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return round(ema, 4)

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

# ── Regime Detection ────────────────────────────────────────────────────────

def detect_regime(symbol, candles_5m, candles_15m):
    """Detect market regime for a symbol.
    
    Returns:
        {
            "symbol": str,
            "volatility": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
            "trend": "STRONG_UP" | "UP" | "RANGE" | "DOWN" | "STRONG_DOWN",
            "trend_strength": 0-100,
            "atr_pct": float,        # ATR as % of price
            "vol_ratio": float,      # Current volume vs 20-period avg
            "rsi_5m": float,
            "rsi_15m": float,
            "ema_alignment": str,    # "bullish" | "bearish" | "mixed"
            "spread_bps": float,     # Bid-ask spread in basis points
            "regime_summary": str,   # Human readable
            "ts": str,
            "params": {...},         # Adaptive parameters
        }
    """
    if len(candles_5m) < 30 or len(candles_15m) < 50:
        return _default_regime(symbol)

    closes_5m = [c["close"] for c in candles_5m]
    closes_15m = [c["close"] for c in candles_15m]
    current_price = closes_5m[-1]

    # ATR as % of price (volatility measure)
    atr_5m = calc_atr(candles_5m)
    atr_pct = (atr_5m / current_price * 100) if current_price > 0 else 0

    # ATR 15m for regime
    atr_15m = calc_atr(candles_15m)
    atr_15m_pct = (atr_15m / current_price * 100) if current_price > 0 else 0

    # Historical ATR distribution (for percentile ranking)
    atr_history = []
    for i in range(1, min(len(candles_5m), 100)):
        chunk = candles_5m[max(0, i-14):i+1]
        if len(chunk) >= 2:
            atr_history.append(calc_atr(chunk))
    
    # Volatility percentile
    if atr_history and atr_5m > 0:
        atr_percentile = sum(1 for a in atr_history if a < atr_5m) / len(atr_history) * 100
    else:
        atr_percentile = 50

    # Volume ratio
    if len(candles_5m) >= 21:
        avg_vol = sum(c["volume"] for c in candles_5m[-21:-1]) / 20
        vol_ratio = candles_5m[-1]["volume"] / avg_vol if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0
    vol_ratio = round(vol_ratio, 2)

    # RSI
    rsi_5m = calc_rsi(closes_5m)
    rsi_15m = calc_rsi(closes_15m)

    # EMA alignment (15m)
    ema9 = calc_ema(closes_15m, 9)
    ema21 = calc_ema(closes_15m, 21)
    ema50 = calc_ema(closes_15m, 50)
    
    if ema9 and ema21 and ema50:
        if ema9 > ema21 > ema50:
            ema_alignment = "bullish"
        elif ema9 < ema21 < ema50:
            ema_alignment = "bearish"
        else:
            ema_alignment = "mixed"
    else:
        ema_alignment = "mixed"

    # ── Classify Volatility ──
    if atr_percentile >= 90 or atr_pct > 1.0:
        volatility = "EXTREME"
    elif atr_percentile >= 70 or atr_pct > 0.6:
        volatility = "HIGH"
    elif atr_percentile >= 30 or atr_pct > 0.3:
        volatility = "MEDIUM"
    else:
        volatility = "LOW"

    # ── Classify Trend ──
    # Using EMA alignment + slope
    if ema9 and ema21:
        ema_slope = (ema9 - ema21) / current_price * 100 if current_price > 0 else 0
    else:
        ema_slope = 0

    trend_strength = min(100, abs(ema_slope) * 50)  # 0-100 scale

    if ema_alignment == "bullish" and ema_slope > 0.05:
        trend = "STRONG_UP" if trend_strength > 40 else "UP"
    elif ema_alignment == "bearish" and ema_slope < -0.05:
        trend = "STRONG_DOWN" if trend_strength > 40 else "DOWN"
    elif ema_alignment == "bullish":
        trend = "UP"
    elif ema_alignment == "bearish":
        trend = "DOWN"
    else:
        trend = "RANGE"

    # ── Adaptive Parameters ──
    params = _calc_adaptive_params(volatility, trend, trend_strength, rsi_5m, rsi_15m, vol_ratio, atr_pct)

    # ── Summary ──
    vol_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "EXTREME": "🔴"}
    trend_emoji = {"STRONG_UP": "🚀", "UP": "📈", "RANGE": "➡️", "DOWN": "📉", "STRONG_DOWN": "⬇️"}
    summary = f"{vol_emoji.get(volatility, '⚪')} Vol: {volatility} ({atr_pct:.2f}%) | {trend_emoji.get(trend, '➡️')} Trend: {trend} ({trend_strength:.0f}%) | RSI: {rsi_5m:.0f}/{rsi_15m:.0f}"

    # ── Calculate projected TP/SL prices from current price ──
    tp_pct = params.get("tp_pct", 0.01)
    sl_pct = params.get("sl_pct", 0.005)
    long_tp = round(current_price * (1 + tp_pct), 4)
    long_sl = round(current_price * (1 - sl_pct), 4)
    short_tp = round(current_price * (1 - tp_pct), 4)
    short_sl = round(current_price * (1 + sl_pct), 4)

    return {
        "symbol": symbol,
        "current_price": current_price,
        "volatility": volatility,
        "atr_pct": round(atr_pct, 4),
        "atr_percentile": round(atr_percentile, 1),
        "trend": trend,
        "trend_strength": round(trend_strength, 1),
        "vol_ratio": vol_ratio,
        "rsi_5m": rsi_5m,
        "rsi_15m": rsi_15m,
        "ema_alignment": ema_alignment,
        "regime_summary": summary,
        "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
        "params": params,
        "projected": {
            "entry": current_price,
            "long": {"tp": long_tp, "sl": long_sl},
            "short": {"tp": short_tp, "sl": short_sl},
        },
    }


def _calc_adaptive_params(volatility, trend, trend_strength, rsi_5m, rsi_15m, vol_ratio, atr_pct):
    """Calculate adaptive parameters based on market regime.
    
    ══════════════════════════════════════════════════════════════
    SCALPING-FIRST PHILOSOPHY (v3.0 — 2026-05-28)
    ══════════════════════════════════════════════════════════════
    Core principle: "Sedikit tapi sering" — small TP, tight SL, trade BOTH directions.
    
    Key differences from swing approach:
    1. BOTH directions ALWAYS open — scalpers don't care about macro trend
    2. Volume threshold LOW — we scalp in any volume, not just spikes
    3. RSI 5m for decisions — 15m is too slow for scalping
    4. Tighter TP/SL across the board — quick in-and-out
    5. Oversold RSI does NOT block SHORT — momentum scalping works in any RSI
    6. Quick scalp mode in ALL trend conditions, not just STRONG_UP
    """
    base = {
        # ── Scalping Base (tight & fast) ──
        "vol_ratio_min": 0.6,       # Very low — scalpers trade in any volume
        "rsi_long_max": 55,         # Wide window — more LONG opportunities
        "rsi_short_min": 45,        # Wide window — more SHORT opportunities  
        "tp_pct": 0.006,            # 0.6% TP — quick scalp target
        "sl_pct": 0.004,            # 0.4% SL — tight risk control
        "trailing_activate": 0.003, # Start trailing at 0.3%
        "trailing_distance": 0.0015,# Tight trail distance
        "max_positions": 2,         # More concurrent positions for scalping
        "confidence_min": "C2",     # Allow C2+ — more trades, learn from data
        "allow_long": True,         # ALWAYS allow both directions
        "allow_short": True,        # ALWAYS allow both directions
        "risk_multiplier": 1.0,
        "quick_scalp": True,        # Always in quick scalp mode
        "scalp_mode": "SCALP",
    }

    # ══════════════════════════════════════════════════════════════
    # ── Volatility adjustments (scalping lens) ──
    # ══════════════════════════════════════════════════════════════
    if volatility == "EXTREME":
        base["vol_ratio_min"] = 0.5     # Lower threshold — vol is already extreme
        base["tp_pct"] = 0.010         # Wider TP (1.0%) — ride the volatility
        base["sl_pct"] = 0.006         # Slightly wider SL (0.6%) — avoid noise stops
        base["trailing_activate"] = 0.005
        base["risk_multiplier"] = 0.6  # Smaller size — respect the chaos
        base["confidence_min"] = "C2"  # Scalping: allow C2 even in extreme vol
        base["scalp_mode"] = "SCALP_EXTREME"
    elif volatility == "HIGH":
        base["vol_ratio_min"] = 0.5
        base["tp_pct"] = 0.008         # 0.8% TP
        base["sl_pct"] = 0.005         # 0.5% SL
        base["trailing_activate"] = 0.004
        base["risk_multiplier"] = 0.8
        base["confidence_min"] = "C2"
        base["scalp_mode"] = "SCALP_VOL"
    elif volatility == "LOW":
        base["vol_ratio_min"] = 0.4     # Ultra-low — calm market, take what we can
        base["tp_pct"] = 0.004         # 0.4% — tiny but consistent
        base["sl_pct"] = 0.002         # 0.2% — very tight SL
        base["trailing_activate"] = 0.002
        base["trailing_distance"] = 0.001
        base["risk_multiplier"] = 1.2  # Bigger size in calm — less noise
        base["confidence_min"] = "C2"
        base["scalp_mode"] = "SCALP_CALM"

    # ══════════════════════════════════════════════════════════════
    # ── Trend adjustments (scalping: trade BOTH sides) ──
    # ══════════════════════════════════════════════════════════════
    if trend in ["STRONG_UP", "UP"]:
        # Uptrend → LONG is primary, but SHORT on overbought bounces is VALID
        base["rsi_long_max"] = 60          # Wide — enter LONG pullbacks anytime
        base["rsi_short_min"] = 65         # SHORT only on overbought bounces (counter-trend scalp)
        base["vol_ratio_min"] = max(0.4, base["vol_ratio_min"] - 0.1)
        base["risk_multiplier"] = round(base["risk_multiplier"] * 1.15, 2)
        
        if trend == "STRONG_UP" and trend_strength > 50:
            base["tp_pct"] = 0.005         # 0.5% — fast profit in strong momentum
            base["sl_pct"] = 0.003         # 0.3% — very tight
            base["trailing_activate"] = 0.002
            base["trailing_distance"] = 0.001
            base["scalp_mode"] = "SCALP_MOMENTUM_LONG"
        else:
            base["scalp_mode"] = "SCALP_TREND_LONG"
    
    elif trend in ["STRONG_DOWN", "DOWN"]:
        # Downtrend → SHORT is primary, but LONG on oversold bounces is VALID
        # KEY CHANGE: allow_short ALWAYS True — scalpers SHORT in downtrend!
        base["rsi_short_min"] = 40         # Very wide — enter SHORT on any bounce
        base["rsi_long_max"] = 35          # LONG only on deep oversold (counter-trend scalp)
        base["vol_ratio_min"] = max(0.4, base["vol_ratio_min"] - 0.1)
        base["risk_multiplier"] = round(base["risk_multiplier"] * 1.15, 2)
        
        if trend == "STRONG_DOWN" and trend_strength > 50:
            base["tp_pct"] = 0.005         # 0.5% — fast profit in strong momentum
            base["sl_pct"] = 0.003         # 0.3% — very tight
            base["trailing_activate"] = 0.002
            base["trailing_distance"] = 0.001
            base["scalp_mode"] = "SCALP_MOMENTUM_SHORT"
        else:
            base["scalp_mode"] = "SCALP_TREND_SHORT"
    
    elif trend == "RANGE":
        # Range → both directions equally, tighter stops
        base["tp_pct"] = min(base["tp_pct"], 0.005)  # Max 0.5% in range
        base["sl_pct"] = min(base["sl_pct"], 0.003)  # Max 0.3% SL in range
        base["risk_multiplier"] = round(base["risk_multiplier"] * 0.9, 2)
        base["scalp_mode"] = "SCALP_RANGE"

    # ══════════════════════════════════════════════════════════════
    # ── RSI extremes (scalping: NO direction blocking!) ──
    # ══════════════════════════════════════════════════════════════
    # OLD (swing): RSI < 25 → block SHORT. RSI > 75 → block LONG.
    # NEW (scalp): Use RSI 5m for ENTRY TIMING, not direction blocking.
    # Oversold + downtrend = SHORT momentum is valid! That's the edge.
    # Overbought + uptrend = LONG momentum is valid! That's the edge.
    #
    # We ONLY block in truly extreme conditions (RSI 5m > 90 or < 10)
    # to avoid entering right before a reversal.
    if rsi_5m > 90:
        base["allow_long"] = False     # Extremely overbought — LONG is risky
    elif rsi_5m < 10:
        base["allow_short"] = False    # Extremely oversold — SHORT is risky
    # NOTE: Using rsi_5m (not 15m) — scalping is fast timeframe

    # Round for cleanliness
    for k in ["tp_pct", "sl_pct", "trailing_activate", "trailing_distance"]:
        base[k] = round(base[k], 4)

    return base


def _default_regime(symbol):
    return {
        "symbol": symbol,
        "volatility": "MEDIUM",
        "atr_pct": 0,
        "atr_percentile": 50,
        "trend": "RANGE",
        "trend_strength": 0,
        "vol_ratio": 1.0,
        "rsi_5m": 50,
        "rsi_15m": 50,
        "ema_alignment": "mixed",
        "regime_summary": f"⚪ Awaiting data for {symbol}",
        "ts": datetime.now(timezone(timedelta(hours=7))).isoformat(),
        "params": _calc_adaptive_params("MEDIUM", "RANGE", 0, 50, 50, 1.0, 0.3),
    }


# ── Alert System ────────────────────────────────────────────────────────────

def send_regime_alert(symbol, old_regime, new_regime):
    """Send Telegram alert on significant regime change."""
    # Rate limit
    key = f"{symbol}_{new_regime['volatility']}_{new_regime['trend']}"
    now = time.time()
    if key in _last_alert and now - _last_alert[key] < ALERT_COOLDOWN:
        return
    _last_alert[key] = now

    vol_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "EXTREME": "🔴"}
    trend_emoji = {"STRONG_UP": "🚀", "UP": "📈", "RANGE": "➡️", "DOWN": "📉", "STRONG_DOWN": "⬇️"}

    old_vol = old_regime.get("volatility", "?") if old_regime else "?"
    old_trend = old_regime.get("trend", "?") if old_regime else "?"
    new_vol = new_regime["volatility"]
    new_trend = new_regime["trend"]
    params = new_regime["params"]
    scalp = params.get("scalp_mode", "NORMAL")
    quick = params.get("quick_scalp", False)

    # Only alert on meaningful changes
    vol_changed = old_vol != new_vol
    trend_changed = old_trend != new_trend
    if not vol_changed and not trend_changed:
        return

    # ── Price formatting helper ──
    price = new_regime.get('current_price', 0)
    proj = new_regime.get('projected', {})
    long_proj = proj.get('long', {})
    short_proj = proj.get('short', {})

    def fmt_price(p, sym):
        """Format price with appropriate decimals for the symbol."""
        if 'BTC' in sym:
            return f"{p:,.1f}"
        elif p < 1:
            return f"{p:.6f}"
        elif p < 100:
            return f"{p:.4f}"
        else:
            return f"{p:,.2f}"

    text = (
        f"🌐 **MARKET REGIME: {symbol}**\n"
        f"\n"
        f"Volatility: {vol_emoji.get(old_vol, '⚪')} {old_vol} → {vol_emoji.get(new_vol, '⚪')} {new_vol} ({new_regime['atr_pct']:.2f}%)\n"
        f"Trend: {trend_emoji.get(old_trend, '➡️')} {old_trend} → {trend_emoji.get(new_trend, '➡️')} {new_trend} ({new_regime['trend_strength']:.0f}%)\n"
        f"RSI: {new_regime['rsi_5m']:.0f} (5m) / {new_regime['rsi_15m']:.0f} (15m)\n"
        f"\n"
        f"💰 **Price Levels:**\n"
        f"• Entry: {fmt_price(price, symbol)}\n"
    )

    if params.get('allow_long'):
        text += f"• LONG → TP: {fmt_price(long_proj.get('tp', 0), symbol)} | SL: {fmt_price(long_proj.get('sl', 0), symbol)}\n"
    if params.get('allow_short'):
        text += f"• SHORT → TP: {fmt_price(short_proj.get('tp', 0), symbol)} | SL: {fmt_price(short_proj.get('sl', 0), symbol)}\n"

    text += (
        f"\n"
        f"⚙️ **Adaptive Params:**\n"
        f"• Vol min: {params['vol_ratio_min']}x | Risk: {params['risk_multiplier']}x\n"
        f"• TP: {params['tp_pct']*100:.2f}% | SL: {params['sl_pct']*100:.2f}%\n"
        f"• LONG: {'✅' if params['allow_long'] else '❌'} | SHORT: {'✅' if params['allow_short'] else '❌'}\n"
        f"• Conf min: {params['confidence_min']}\n"
    )
    if quick:
        text += f"\n⚡ **QUICK SCALP MODE: {scalp}** — small TP, tight SL, frequent entries"

    # Regime alerts disabled on Telegram — dashboard only (Nico, 2026-05-24)
    log.info(f"Regime changed: {symbol} {old_vol}→{new_vol} {old_trend}→{new_trend} (dashboard only)")


def save_regime(regimes):
    """Save current regime state to file."""
    REGIME_FILE.write_text(json.dumps(regimes, indent=2))


def load_regime():
    """Load last known regime state."""
    if REGIME_FILE.exists():
        try:
            return json.loads(REGIME_FILE.read_text())
        except:
            pass
    return {}


# ── Public API for scalper ──────────────────────────────────────────────────

# In-memory regime cache
_regime_cache = {}

def update_regime(symbol, candles_5m, candles_15m):
    """Called on every candle close. Updates regime, sends alerts if changed."""
    old = _regime_cache.get(symbol)
    new = detect_regime(symbol, candles_5m, candles_15m)
    _regime_cache[symbol] = new

    # Check for significant change → alert
    if old:
        send_regime_alert(symbol, old, new)
    
    # Save to disk
    save_regime(_regime_cache)
    return new


def get_regime(symbol):
    """Get current cached regime for a symbol."""
    return _regime_cache.get(symbol)


def get_params(symbol):
    """Get adaptive parameters for a symbol (for scalper to use)."""
    regime = _regime_cache.get(symbol)
    if regime:
        return regime.get("params", _calc_adaptive_params("MEDIUM", "RANGE", 0, 50, 50, 1.0, 0.3))
    return _calc_adaptive_params("MEDIUM", "RANGE", 0, 50, 50, 1.0, 0.3)


def get_all_regimes():
    """Get all cached regimes."""
    return _regime_cache


def init_regimes(symbols):
    """Load saved regimes on startup."""
    saved = load_regime()
    _regime_cache.update(saved)
    log.info(f"Loaded {len(saved)} cached regimes")
