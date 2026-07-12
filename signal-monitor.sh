#!/bin/bash
# Signal Monitor — alerts on signals, entries, AND critical errors.
# Monitors BOTH live and testnet logs with clear mode tags.

# Trace invoker for debugging (caught every 5min, source unknown)
echo "$(date '+%Y-%m-%d %H:%M:%S') PID=$$ PPID=$PPID PPCMD=$(cat /proc/$PPID/cmdline 2>/dev/null | tr '\0' ' ')" >> /tmp/sigmon-runs.log

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-8787356885:AAGu7mw0RyFWoPPT887QoLSf2FBAFBcLsZU}"
CHAT_ID="${TELEGRAM_CHAT_ID:--5123376297}"
REGIME="/root/.openclaw/workspace/frontend/scalper/state/market-regime.json"

send_tg() {
    local MSG="$1"
    # HTML parse_mode — underscore-safe (Markdown was eating _ in strategy/symbol names)
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$(MSG="$MSG" CHAT_ID="$CHAT_ID" python3 -c "import json, os; print(json.dumps({'chat_id': os.environ['CHAT_ID'], 'text': os.environ['MSG'], 'parse_mode': 'HTML'}))")" \
        > /dev/null 2>&1
}

# ── Generic scan function for a log file ──
scan_log() {
    local LOG="$1"
    local MODE="$2"        # "LIVE" or "TESTNET"
    local MARKER="$3"      # marker file path
    local ERROR_MARKER="$4"

    [ ! -f "$LOG" ] && return

    # ── Error Alerting ──
    ERROR_LAST=$(cat "$ERROR_MARKER" 2>/dev/null || echo "0")
    ERROR_CURRENT=$(wc -l < "$LOG" 2>/dev/null | tr -d '[:space:]')
    [ -z "$ERROR_CURRENT" ] && ERROR_CURRENT=0

    if [ "$ERROR_CURRENT" -gt "$ERROR_LAST" ]; then
        ERROR_LINES=$(sed -n "$((ERROR_LAST+1)),${ERROR_CURRENT}p" "$LOG")

        ERR_WS=$(echo "$ERROR_LINES" | grep -c "name 'direction' is not defined" 2>/dev/null || true)
        ERR_KEY=$(echo "$ERROR_LINES" | grep -c "Failed to create listenKey" 2>/dev/null || true)
        ERR_OTHER=$(echo "$ERROR_LINES" | grep -c "\[ERROR\]" 2>/dev/null || true)
        ERR_WS=${ERR_WS:-0}
        ERR_KEY=${ERR_KEY:-0}
        ERR_OTHER=${ERR_OTHER:-0}
        TOTAL_ERR=$((ERR_WS + ERR_KEY))

        if [ "$TOTAL_ERR" -gt 20 ]; then
            LAST_ALERT="/tmp/signal-monitor-error-alert-ts-${MODE}"
            NOW=$(date +%s)
            LAST_TS=$(cat "$LAST_ALERT" 2>/dev/null || echo "0")
            DIFF=$((NOW - LAST_TS))

            if [ "$DIFF" -gt 3600 ]; then
                local MODE_TAG=""
                if [ "$MODE" = "LIVE" ]; then
                    MODE_TAG="💰"
                else
                    MODE_TAG="🧪"
                fi
                ALERT_MSG="${MODE_TAG} <b>SCALPER ERROR (${MODE})</b>

⚠️ Critical errors detected:
• <code>direction not defined</code>: ${ERR_WS}x
• <code>listenKey failed</code>: ${ERR_KEY}x
• Other errors: ${ERR_OTHER}x

<i>Throttled: 1 alert/hr per mode</i>"
                send_tg "$ALERT_MSG"
                echo "$NOW" > "$LAST_ALERT"
            fi
        fi
    fi
    echo "$ERROR_CURRENT" > "$ERROR_MARKER"

    # ── Signal/Entry Alerting ──
    LAST=$(cat "$MARKER" 2>/dev/null || echo "0")
    CURRENT=$(wc -l < "$LOG" 2>/dev/null | tr -d '[:space:]')
    [ -z "$CURRENT" ] && CURRENT=0

    # Update marker always
    echo "$CURRENT" > "$MARKER"

    # Nothing new
    [ "$CURRENT" -le "$LAST" ] && return

    # Extract new lines
    NEW_LINES=$(sed -n "$((LAST+1)),${CURRENT}p" "$LOG")

    # Mode tag for messages
    local MODE_ICON=""
    local MODE_LABEL=""
    if [ "$MODE" = "LIVE" ]; then
        MODE_ICON="💰"
        MODE_LABEL="LIVE"
    else
        MODE_ICON="🧪"
        MODE_LABEL="TESTNET"
    fi

    # Check for signals OR actual entry fills
    while IFS= read -r sig_line; do
        SYMBOL=$(echo "$sig_line" | grep -oP '[A-Z]+USDT' | head -1)
        DIR=$(echo "$sig_line" | grep -oP '(LONG|SHORT)' | head -1)
        PRICE=$(echo "$sig_line" | grep -oP '\$[\d.]+' | head -1 | tr -d '$')
        CONF=$(echo "$sig_line" | grep -oP 'C[234]' | head -1)
        [ -z "$SYMBOL" ] && continue

        # Get all related lines
        CONTEXT=$(echo "$NEW_LINES" | sed -n "/🎯 SIGNAL:.*$SYMBOL/,/^[0-9]\{4\}-.*\[^ 📊\]/p" | head -25)

        TP=$(echo "$CONTEXT" | grep -oP 'TP=\$?[\d.]+' | head -1 | sed 's/TP=\$*//')
        SL=$(echo "$CONTEXT" | grep -oP 'SL=\$?[\d.]+' | head -1 | sed 's/SL=\$*//')
        RR=$(echo "$CONTEXT" | grep -oP 'Net R:R=[\d.]+' | head -1 | sed 's/Net R:R=//')
        QTY=$(echo "$CONTEXT" | grep -oP 'Size=[\d.]+' | head -1 | sed 's/Size=//')
        NOTIONAL=$(echo "$CONTEXT" | grep -oP 'Notional=\$[\d.]+' | head -1 | sed 's/Notional=\$//')
        REASON=$(echo "$CONTEXT" | grep -oP 'Reason:.*' | head -1 | sed 's/Reason: //')
        RSI=$(echo "$CONTEXT" | grep -oP 'RSI=[\d.]+' | head -1 | sed 's/RSI=//')
        VOL=$(echo "$CONTEXT" | grep -oP 'Vol=[\d.]+x' | head -1 | sed 's/Vol=//' | tr -d 'x')
        ADAPTIVE=$(echo "$CONTEXT" | grep -oP 'Adaptive:.*' | head -1 | sed 's/Adaptive: //')
        STRATEGY=$(echo "$CONTEXT" | grep -oP 'Strategy: [a-z_]+' | head -1 | sed 's/Strategy: //')

        # Calculate TP/SL % distance
        if [ -n "$TP" ] && [ -n "$PRICE" ] && [ "$PRICE" != "0" ]; then
            TP_PCT=$(python3 -c "print(f'{abs($TP-$PRICE)/$PRICE*100:.2f}%')" 2>/dev/null)
            SL_PCT=$(python3 -c "print(f'{abs($SL-$PRICE)/$PRICE*100:.2f}%')" 2>/dev/null)
        fi

        # Check if DRY RUN or REAL entry
        IS_DRY=$(echo "$CONTEXT" | grep -c "DRY RUN" 2>/dev/null | tr -d '[:space:]')
        [ -z "$IS_DRY" ] && IS_DRY=0
        IS_ENTRY=$(echo "$NEW_LINES" | grep -c "Entry filled.*$SYMBOL\|Entry result.*$SYMBOL\|position opened.*$SYMBOL" 2>/dev/null | tr -d '[:space:]')
        [ -z "$IS_ENTRY" ] && IS_ENTRY=0

        if [ "$IS_DRY" -gt 0 ]; then
            HEADER="${MODE_ICON} <b>SIGNAL (DRY RUN | ${MODE_LABEL}): ${DIR} ${SYMBOL}</b>"
        elif [ "$IS_ENTRY" -gt 0 ]; then
            HEADER="${MODE_ICON} <b>ENTRY FILLED (${MODE_LABEL}): ${DIR} ${SYMBOL}</b>"
        else
            HEADER="${MODE_ICON} <b>SIGNAL (${MODE_LABEL}): ${DIR} ${SYMBOL}</b>"
        fi

        # Build message (HTML — underscore-safe)
        MSG="${HEADER}

💰 Entry: <code>\$${PRICE}</code> | ${CONF}
📋 ${REASON}
🏷️ Strategy: ${STRATEGY:-?}

✅ TP: <code>\$${TP}</code> (+${TP_PCT:-?})
🛑 SL: <code>\$${SL}</code> (-${SL_PCT:-?})
📐 R:R: ${RR:-?} | Size: ${QTY:-?}
📏 Adaptive: ${ADAPTIVE:-?}"

        send_tg "$MSG"

    done <<< "$(echo "$NEW_LINES" | grep -E "🎯 SIGNAL:|Entry filled|Entry PLACED")"

    # Note: SL/TP close alerts are sent directly by scalper-v2.py (send_telegram_close_alert)
    # No need to duplicate them here — that caused empty "🧪 TESTNET" messages."
}

# ── Health Check: Service monitoring + auto-restart ──
health_check() {
    local SERVICE="$1"
    local MODE="$2"        # "LIVE" or "TESTNET"
    local LOG="$3"
    local STALE_MIN="$4"   # stale threshold in minutes
    local ALERT_TS_FILE="/tmp/signal-monitor-health-alert-${MODE}"

    local MODE_ICON="💰"
    [ "$MODE" = "TESTNET" ] && MODE_ICON="🧪"

    # Check if service is active
    local IS_ACTIVE
    IS_ACTIVE=$(systemctl is-active "$SERVICE" 2>/dev/null)

    if [ "$IS_ACTIVE" != "active" ]; then
        # Service is down — try auto-restart
        systemctl restart "$SERVICE" 2>/dev/null
        sleep 3
        IS_ACTIVE=$(systemctl is-active "$SERVICE" 2>/dev/null)

        local NOW=$(date +%s)
        local LAST_TS=$(cat "$ALERT_TS_FILE" 2>/dev/null || echo "0")
        local DIFF=$((NOW - LAST_TS))

        # Alert throttle: max 1 alert per 30 min
        if [ "$DIFF" -gt 1800 ]; then
            if [ "$IS_ACTIVE" = "active" ]; then
                send_tg "${MODE_ICON} <b>AUTO-FIXED: ${SERVICE}</b>

✅ Service was DOWN — auto-restarted successfully.
⏱️ $(date '+%Y-%m-%d %H:%M:%S')"
            else
                send_tg "${MODE_ICON} <b>🚨 SERVICE DOWN: ${SERVICE}</b>

❌ Service is inactive and auto-restart FAILED!
⚠️ Manual intervention required.
⏱️ $(date '+%Y-%m-%d %H:%M:%S')"
            fi
            echo "$NOW" > "$ALERT_TS_FILE"
        fi
        return
    fi

    # Service is running — check if log is stale
    if [ -f "$LOG" ]; then
        local LOG_MOD
        LOG_MOD=$(stat -c %Y "$LOG" 2>/dev/null)
        local NOW=$(date +%s)
        local AGE_MIN=$(( (NOW - LOG_MOD) / 60 ))

        if [ "$AGE_MIN" -gt "$STALE_MIN" ]; then
            local ALERT_NOW=$(date +%s)
            local LAST_TS=$(cat "$ALERT_TS_FILE" 2>/dev/null || echo "0")
            local DIFF=$((ALERT_NOW - LAST_TS))

            # Alert throttle: max 1 alert per 30 min
            if [ "$DIFF" -gt 1800 ]; then
                send_tg "${MODE_ICON} <b>⚠️ STALE LOG: ${SERVICE}</b>

📝 Log not updated for <b>${AGE_MIN} min</b> (threshold: ${STALE_MIN} min)
🔄 Service is running but may be hung.
⏱️ $(date '+%Y-%m-%d %H:%M:%S')"
                echo "$ALERT_NOW" > "$ALERT_TS_FILE"
            fi
        fi
    fi
}

# ── Scan LIVE log ──
scan_log \
    "/root/.openclaw/workspace/frontend/scalper/state/scalper-v2-live.log" \
    "LIVE" \
    "/tmp/signal-monitor-live-last" \
    "/tmp/signal-monitor-live-error-last"

# ── Scan TESTNET log ──
scan_log \
    "/root/.openclaw/workspace/frontend/scalper/state/scalper-v2-testnet.log" \
    "TESTNET" \
    "/tmp/signal-monitor-testnet-last" \
    "/tmp/signal-monitor-testnet-error-last"

# ── Health Checks ──
health_check "nyx-scalper" "LIVE" "/root/.openclaw/workspace/frontend/scalper/state/scalper-v2-live.log" 30
health_check "nyx-scalper-testnet" "TESTNET" "/root/.openclaw/workspace/frontend/scalper/state/scalper-v2-testnet.log" 30
health_check "nyx-scalper-api" "API" "/dev/null" 9999

# Silent exit
exit 0
