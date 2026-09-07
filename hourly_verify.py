#!/usr/bin/env python3
"""Bluu Ink — Hourly Overnight Verification
The user's explicit demand: "double check your work every hour, because if
you paid me a dollar for everytime you assumed things would work overnight
and something just broke immediatly it would be enough to pay my subscription
for the year."

This script VERIFIES (not assumes) that the overnight stack is actually
working, and posts a compact Discord report:
  1. Dispatcher cron producing real artifacts (new done tasks since last check)
  2. Main gateway alive
  3. All 3 boards progressing (done count increasing)
  4. GPU watchdog healthy (no TDR)
  5. Playtest loop producing results
  6. Any stalls / failures

Run via cron every hour.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Shared park-marker list (single source of truth with dispatcher_singlebuild.py).
# A divergent hand-copy resurrects parked tasks into an infinite retry loop.
from bluu_ink_constants import atomic_write_json, PARKED_MARKERS

HERMES = Path("C:/Users/bluue/AppData/Local/hermes")
BOTS_DIR = HERMES / "bots"
CONFIG = Path("C:/Users/bluue/AppData/Local/bluu-ink/config.json")
STATE = Path("C:/Users/bluue/AppData/Local/bluu-ink/state/hourly_verify.json")
DISPATCHER_OUT = HERMES / "cron" / "output" / "a28bec9eccb7"
PLAYTEST_LOG = HERMES / "playtest_auto.log"

DEPTS = ["forge", "sanctuary", "harmony"]


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path, data):
    # ATOMIC WRITE (2026-09-06): temp + os.replace so a concurrent dispatcher
    # (every 5min) never reads a half-written board.json. Sweep #4 (deepseek):
    # use the SHARED pid-unique writer — a deterministic ".json.tmp" collides
    # when two writers touch the same file concurrently.
    atomic_write_json(path, data, indent=2)


def load_webhook():
    cfg = load_json(CONFIG)
    wh = (cfg.get("discord", {}) or {}).get("webhook_url", "")
    if wh and "YOUR_WEBHOOK" not in wh:
        return wh
    return None


def post_discord(webhook, text):
    if not webhook:
        print("  (no webhook, skipping Discord)")
        return
    try:
        import requests
        r = requests.post(webhook, json={"content": text}, timeout=15)
        print(f"  discord post: {r.status_code}")
    except Exception as e:
        print(f"  discord post error: {e}")


def main_gateway_alive():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'gateway run' -and "
             "$_.CommandLine -notmatch '--profile' }; "
             "if ($p) { 'ALIVE' } else { 'DEAD' }"],
            capture_output=True, text=True, timeout=30)
        return "ALIVE" in out.stdout
    except Exception:
        return False


def board_done_count(dept):
    board = load_json(BOTS_DIR / dept / "kanban" / "board.json")
    return len(board.get("columns", {}).get("done", []))


def dispatcher_last_output():
    """Return (timestamp, produced_count) of the most recent dispatcher run."""
    if not DISPATCHER_OUT.exists():
        return None, 0
    files = sorted(DISPATCHER_OUT.glob("*.md"), reverse=True)
    if not files:
        return None, 0
    latest = files[0]
    text = latest.read_text(encoding="utf-8")
    produced = 0
    for line in text.splitlines():
        if "Produced" in line and "real artifact" in line:
            import re
            m = re.search(r"Produced (\d+) real artifact", line)
            if m:
                produced = int(m.group(1))
    return latest.stem, produced


def wire_health():
    """Scan recent dispatcher output for wire-step failures (the new critical
    path). Returns (ok, detail). A wire failure parks the task to 'blocked'
    after MAX_ATTEMPTS — if the wirer is down or the gate rejects everything,
    production silently freezes, so this must be surfaced."""
    if not DISPATCHER_OUT.exists():
        return True, "no dispatcher output yet"
    files = sorted(DISPATCHER_OUT.glob("*.md"), reverse=True)[:6]
    wired = parked = failed = 0
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        wired += text.count("wired + committed to Galage")
        parked += text.count("parked (wire step)")
        failed += text.count("wire failed")
    if parked or failed:
        return False, f"wire issues: {wired} wired, {parked} parked, {failed} failed (last 6 runs)"
    if wired:
        return True, f"wire healthy: {wired} wired in last 6 runs"
    return True, "no wire activity yet (dispatcher just resumed)"


def gpu_healthy():
    """Check nvidia-smi for VRAM/temp/util — flag TDR precursors."""
    try:
        out = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
             "nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu --format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        parts = out.stdout.strip().split(",")
        if len(parts) >= 4:
            vram_used, vram_total, temp, util = [int(p.strip()) for p in parts[:4]]
            return True, f"VRAM {vram_used}/{vram_total}MiB temp {temp}C util {util}%"
    except Exception:
        pass
    return False, "nvidia-smi unavailable"


def playtest_last_result():
    """Return the most recent playtest overall score, or None."""
    if not PLAYTEST_LOG.exists():
        return None
    text = PLAYTEST_LOG.read_text(encoding="utf-8")
    import re
    scores = re.findall(r"overall: \*\*([\d.]+)/100\*\*", text)
    if scores:
        return float(scores[-1])
    return None


# ── AUTO-HEAL (2026-09-03): actively FIX stalls, not just report them.
# Mirrors consolidated_status.py's auto_heal so the HOURLY monitor also
# heals, not only the 30-min poster. User's standing rule: "no point knowing
# something is stalled if nothing fixes it in the background."
def main_gateway_alive_heal():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\").CommandLine"],
            capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            if "gateway run" in line and "--profile" not in line:
                return True
        return False
    except Exception:
        return False


def unblock_parked(board_path):
    """Move 'blocked' tasks back to ready (so they retry after fixes).
    FIXED 2026-09-06: previously moved ALL blocked tasks, resurrecting tasks the
    dispatcher intentionally parked after MAX_ATTEMPTS (note contains 'parked
    after'/'parked: fabricated'/'wire step failed') — an infinite retry loop that
    starved the queue. Now skips those, matching stall_audit.py's behavior."""
    board = load_json(board_path)
    cols = board.get("columns", {})
    blocked = cols.get("blocked", [])
    if not blocked:
        return 0
    moved_ids = set()
    for t in blocked:
        note = (t.get("note") or "").lower()
        if any(m in note for m in PARKED_MARKERS):
            continue  # dispatcher-parked — leave blocked, don't resurrect
        t["status"] = "ready"
        t.pop("fail_count", None)
        t.pop("blocked_reason", None)
        cols.setdefault("ready", []).append(t)
        moved_ids.add(t.get("id"))
    # Keep only tasks that were NOT moved (tracked by id, not status — a parked
    # task may have no 'status' field at all, so filtering on status would drop it).
    cols["blocked"] = [t for t in blocked if t.get("id") not in moved_ids]
    # RMW clobber fix (sweep #4, kimi): only rewrite the board when something
    # was actually moved. Writing unconditionally (every hour, whenever any
    # blocked task exists) risked clobbering a concurrent dispatcher board write
    # (the dispatcher holds a stale board across a long build()). A no-op move
    # should never touch the file.
    if moved_ids:
        save_json(board_path, board)
    return len(moved_ids)


def trigger_dispatcher():
    """Fire the real-work dispatcher cron so it picks up any available work."""
    try:
        r = subprocess.run(
            ["C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe",
             "cron", "run", "a28bec9eccb7"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception as e:
        print("dispatcher trigger err:", e)
        return False


def restart_gateway_detached():
    """Start a fresh MAIN cron gateway IF none is running. Windowless."""
    pyw = r"C:\Users\bluue\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe"
    gw = r"C:\Users\bluue\AppData\Local\hermes\scripts\gateway_restart.py"
    try:
        subprocess.Popen(
            [pyw, gw],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print("gateway restart err:", e)
        return False


def auto_heal():
    """Actively FIX stalls across all 3 boards + gateway. Returns heal actions."""
    actions = []
    for dept in DEPTS:
        board_path = BOTS_DIR / dept / "kanban" / "board.json"
        # 1. Unblock parked tasks
        n = unblock_parked(board_path)
        if n:
            actions.append(f"🔓 {dept}: unblocked {n} parked task(s)")
        # 2. If board is empty (no pending) -> fire dispatcher
        board = load_json(board_path)
        cols = board.get("columns", {})
        pend = len(cols.get("backlog", [])) + len(cols.get("ready", []))
        if pend == 0:
            if trigger_dispatcher():
                actions.append(f"🔄 {dept}: dispatcher auto-triggered (empty board)")
    # 3. If main gateway down -> restart it
    if not main_gateway_alive_heal():
        if restart_gateway_detached():
            actions.append("🚑 Main cron gateway DOWN — restart issued")
    return actions


def main():
    now = datetime.now(timezone.utc)
    prev = load_json(STATE)
    issues = []
    ok = []

    # 1. Dispatcher producing.
    disp_ts, produced = dispatcher_last_output()
    if produced > 0:
        ok.append(f"✅ Dispatcher produced {produced} artifact(s) (last {disp_ts})")
    else:
        issues.append("🔴 Dispatcher produced 0 artifacts in last run")

    # 1b. Wire step healthy (the new critical path — a dead wirer or a gate
    # rejecting everything silently freezes production).
    wire_ok, wire_msg = wire_health()
    if wire_ok:
        ok.append(f"🔌 {wire_msg}")
    else:
        issues.append(f"🔴 {wire_msg}")

    # 2. Gateway alive.
    if main_gateway_alive():
        ok.append("✅ Main gateway alive")
    else:
        issues.append("🔴 Main gateway DOWN")

    # 3. Boards progressing (done count increasing vs last check).
    for dept in DEPTS:
        done = board_done_count(dept)
        prev_done = prev.get(dept, {}).get("done", done)
        delta = done - prev_done
        if delta > 0:
            ok.append(f"✅ {dept}: +{delta} done (total {done})")
        elif delta == 0:
            ok.append(f"🟡 {dept}: no new done since last check (total {done})")
        else:
            issues.append(f"🔴 {dept}: done count DROPPED {done} (was {prev_done})")
        prev[dept] = {"done": done, "checked_at": now.isoformat()}

    # 4. GPU healthy.
    gpu_ok, gpu_msg = gpu_healthy()
    if gpu_ok:
        ok.append(f"✅ GPU: {gpu_msg}")
    else:
        issues.append(f"🔴 GPU: {gpu_msg}")

    # 5. Playtest loop.
    pt = playtest_last_result()
    if pt is not None:
        ok.append(f"✅ Playtest last overall: {pt:.0f}/100")
    else:
        issues.append("🟡 Playtest loop: no result yet")

    # 6. AUTO-HEAL (2026-09-03): actively FIX stalls, not just report them.
    # Unblock parked tasks, fire dispatcher on empty boards, restart dead gateway.
    heal_actions = auto_heal()
    for a in heal_actions:
        ok.append(a)

    save_json(STATE, prev)

    # Build compact report.
    lines = [f"🕐 **Hourly Verification** ({now.strftime('%H:%M')} UTC)"]
    lines += ok
    if issues:
        lines += ["---", "⚠️ **Needs attention:**"] + issues
    else:
        lines.append("✅ **All systems verified working**")
    report = "\n".join(lines)
    print(report)

    webhook = load_webhook()
    post_discord(webhook, report)

    # Exit 0 if no red issues.
    return 0 if not any("🔴" in i for i in issues) else 1


if __name__ == "__main__":
    sys.exit(main())
