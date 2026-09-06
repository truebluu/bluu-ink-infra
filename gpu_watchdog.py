#!/usr/bin/env python3
"""GPU Pre-load Watchdog for RTX 5090 (Blackwell) — CSV-based (works with driver 610.88).
Monitors VRAM headroom, GPU utilization, temperature. Pre-flight check blocks heavy
jobs when GPU isn't safely idle. Posts to Discord on warnings.
"""
import subprocess, json, sys, os
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("C:/Users/bluue/AppData/Local/hermes/state/gpu_watchdog.jsonl")
WEBHOOK = None
cfg = Path("C:/Users/bluue/AppData/Local/bluu-ink/config.json")
if cfg.exists():
    try:
        import json as _j
        WEBHOOK = (_j.load(open(cfg)) or {}).get("discord", {}).get("webhook_url")
    except Exception:
        WEBHOOK = None

def run_smi():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,"
             "utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"], text=True, timeout=10)
        parts = [p.strip() for p in out.strip().split(",")]
        if len(parts) >= 6:
            return {
                "total_mib": float(parts[0]), "used_mib": float(parts[1]),
                "free_mib": float(parts[2]), "util": float(parts[3]),
                "temp": float(parts[4]), "power": float(parts[5]),
            }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse"}

def post_discord(text):
    if not WEBHOOK or "YOUR_WEBHOOK" in WEBHOOK:
        return
    import urllib.request
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(WEBHOOK, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Hermes-GPUWatchdog/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print("discord:", r.status)
    except Exception as e:
        print("discord err:", e)

def check():
    gpu = run_smi()
    if "error" in gpu:
        print(f"ERROR: {gpu['error']}")
        return gpu
    free_gb = gpu["free_mib"] / 1024
    used_gb = gpu["used_mib"] / 1024
    alerts = []
    level = "OK"
    if free_gb < 1.0:
        alerts.append(f"CRITICAL VRAM: {free_gb:.1f}GB free"); level = "CRITICAL"
    elif free_gb < 2.0:
        alerts.append(f"LOW VRAM: {free_gb:.1f}GB free"); level = max(level, "WARN")
    if gpu["temp"] >= 88:
        alerts.append(f"CRITICAL TEMP: {gpu['temp']}C"); level = "CRITICAL"
    elif gpu["temp"] >= 80:
        alerts.append(f"HIGH TEMP: {gpu['temp']}C"); level = max(level, "WARN")
    if gpu["util"] >= 95:
        alerts.append(f"HIGH GPU UTIL: {gpu['util']}%"); level = max(level, "WARN")
    if gpu["power"] > 550:
        alerts.append(f"HIGH POWER: {gpu['power']:.0f}W"); level = max(level, "WARN")
    rec = {"ts": datetime.now().isoformat(), "level": level, "vram_free_gb": round(free_gb,1),
           "vram_used_gb": round(used_gb,1), "util": gpu["util"], "temp": gpu["temp"],
           "power": gpu["power"], "alerts": alerts}
    LOG = LOG_FILE
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if level in ("WARN", "CRITICAL"):
        post_discord(f"🔴 GPU Watchdog {level}: {' | '.join(alerts)} | VRAM {used_gb:.1f}/{used_gb+free_gb:.0f}GB util {gpu['util']}% temp {gpu['temp']}C")
    print(json.dumps(rec))
    return rec

def preflight():
    gpu = run_smi()
    if "error" in gpu:
        print(f"❌ {gpu['error']}"); return False
    free_gb = gpu["free_mib"] / 1024
    util = gpu["util"]
    temp = gpu["temp"]
    issues = []
    if free_gb < 4.0:
        issues.append(f"VRAM headroom only {free_gb:.1f}GB (need >=4GB)")
    if util > 10:
        issues.append(f"GPU not idle: {util}% util")
    if temp > 55:
        issues.append(f"GPU warm: {temp}C")
    if issues:
        print(f"❌ Pre-flight BLOCKED: {' | '.join(issues)}")
        return False
    print(f"✅ Pre-flight OK: {free_gb:.1f}GB free, {util}% util, {temp}C")
    return True

def now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")

if __name__ == "__main__":
    if "--preflight" in sys.argv:
        sys.exit(0 if preflight() else 1)
    check()
