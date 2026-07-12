#!/bin/bash
# Scalper runner — called by cron or OpenClaw
source /root/.bashrc
export SCALPER_MODE=${1:-testnet}
cd /root/.openclaw/workspace/frontend/scalper

RESULT=$(python3 scalper.py 2>&1)
echo "$RESULT"

# Send to Telegram alert group if there are trades
TRADES=$(echo "$RESULT" | grep "PLACED\|FAILED\|close_timeout")
if [ -n "$TRADES" ]; then
  # Will be handled by OpenClaw cron instead
  echo "TRADE_ACTIVITY_DETECTED"
fi
