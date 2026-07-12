#!/bin/bash
# Scalper V2 runner — runs as background daemon
source /root/.bashrc
cd "$(dirname "$0")"

# Load config from .env.scalper (overrides defaults in code)
[ -f .env.scalper ] && source .env.scalper

# Allow CLI arg override: ./start-v2.sh testnet
export SCALPER_MODE=${1:-${SCALPER_MODE:-testnet}}

VENV_PYTHON="/root/.openclaw/workspace/frontend/polymarket-trader/venv/bin/python3"
LOG_FILE="state/scalper-v2-${SCALPER_MODE}.log"
PID_FILE="state/scalper-v2-${SCALPER_MODE}.pid"

# Kill existing instance
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null
    sleep 2
fi

echo "Scalper V2 starting — Mode: $SCALPER_MODE"
echo "  Pairs: $SCALPER_PAIRS"
echo "  Leverage: ${SCALPER_LEVERAGE}x | Size: ${SCALPER_SIZE_PCT} | TP: ${SCALPER_TP_PCT} | SL: ${SCALPER_SL_PCT}"
echo "  Log: $LOG_FILE"

# Start daemon
nohup $VENV_PYTHON scalper-v2.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Started — PID: $(cat $PID_FILE)"
