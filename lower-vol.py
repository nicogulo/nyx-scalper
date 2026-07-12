#!/usr/bin/env python3
"""Lower adaptive vol thresholds for scalping — current 2.0x is too restrictive."""
import json

path = "/root/.openclaw/workspace/frontend/scalper/state/adaptive-config.json"
with open(path) as f:
    cfg = json.load(f)

# Lower global vol threshold from 2.0 to 1.0 for scalping
cfg["global"]["vol_threshold"] = 1.0

# Lower per-pair thresholds
for pair in cfg.get("pairs", {}):
    if cfg["pairs"][pair].get("vol_threshold", 2.0) > 1.0:
        cfg["pairs"][pair]["vol_threshold"] = 1.0

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)

print("✅ Vol thresholds lowered:")
print(f"  Global: 2.0 → 1.0")
for pair in cfg.get("pairs", {}):
    print(f"  {pair}: {cfg['pairs'][pair]['vol_threshold']}")
