#!/bin/bash
# Live Analyze Mode — real data, no execution
export SCALPER_MODE=live
export SCALPER_DRY_RUN=true

cd "$(dirname "$0")"
PYTHON="/root/.openclaw/workspace/frontend/polymarket-trader/venv/bin/python3"
PIDFILE="state/scalper-v2-live-analyze.pid"
LOGFILE="state/scalper-v2-live-analyze.log"

# Kill existing
if [ -f "$PIDFILE" ]; then
    kill $(cat "$PIDFILE") 2>/dev/null
    rm "$PIDFILE"
    sleep 2
fi

source /root/.bashrc

echo "Scalper V2 starting — LIVE ANALYZE MODE (DRY RUN)"
echo "  Mode: live + dry-run (NO trade execution)"
echo "  Pairs: BTCUSDT only"
echo "  Log: $LOGFILE"

nohup $PYTHON scalper-v2.py >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "Started — PID: $(cat $PIDFILE)"
