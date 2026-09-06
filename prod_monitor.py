#!/usr/bin/env python3
"""Monitor bluu-nano-v4 production model + dispatcher health. Posts to Discord.
Runs NATIVELY on Windows (Ollama is Windows-hosted; WSL2 can't reach 127.0.0.1).
Checks: (1) model responds via Ollama, (2) GPU/VRAM state. Alerts on model failure."""
import json, os, subprocess, sys, time, urllib.request

STATE = os.path.join(os.environ.get("LOCALAPPDATA", "C:/Users/bluue/AppData/Local"), "bluu-ink", "state", "prod_monitor_state.json")
# KNOWN-VALID config.json webhook (the .env one 404s — Discord auth fixed 2026-09-04)
try:
    _cfg = json.load(open("C:/Users/bluue/AppData/Local/bluu-ink/config.json"))
    WEBHOOK = _cfg.get("discord", {}).get("webhook_url", "")
except Exception:
    WEBHOOK = ""

MODEL = "bluu-nano-v4:latest"
OLLAMA = "http://127.0.0.1:11434/api/generate"


def post(text):
    if not WEBHOOK:
        print("no webhook")
        return
    data = json.dumps({"content": text}).encode()
    req = urllib.request.Request(WEBHOOK, data=data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("posted")
    except Exception as e:
        print("post failed:", e)


def model_alive():
    body = json.dumps({"model": MODEL, "prompt": "Write: class_name Probe extends Node", "stream": False, "options": {"num_predict": 8}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        d = json.loads(r.read())
        # bluu-nano is qwen3-style: routes output to `thinking`, `response` may be empty
        return bool((d.get("response") or d.get("thinking") or "").strip())
    except Exception:
        return False


def gpu_state():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout.strip()
        return out
    except Exception:
        return "n/a"


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"last_ok": 0, "alerts": 0, "last_post": 0}
    now = time.time()
    alive = model_alive()
    gpu = gpu_state()

    msg = f"🛠️ [bluu-nano-v4] model={'OK' if alive else 'DOWN'} gpu={gpu}"

    if not alive:
        st["alerts"] = st.get("alerts", 0) + 1
        msg += f" **MODEL DOWN** (alert {st['alerts']})"
        if st["alerts"] <= 2:
            post(f"🔴 bluu-nano-v4 **NOT RESPONDING** via Ollama. GPU: {gpu}")
    else:
        st["alerts"] = 0
        st["last_ok"] = now

    if now - st.get("last_post", 0) > 3000:
        post(msg)
        st["last_post"] = now

    json.dump(st, open(STATE, "w"))
    print(msg)


if __name__ == "__main__":
    main()
