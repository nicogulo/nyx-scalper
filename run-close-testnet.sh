#!/bin/bash
set -e
cd /root/.openclaw/workspace/frontend/scalper
source ~/.bashrc 2>/dev/null || true
source .env.scalper 2>/dev/null || true
python3 close-all.py
