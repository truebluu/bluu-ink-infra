#!/usr/bin/env python3
"""Bluu Ink — Hourly Stall Audit.
Checks all 3 bot boards + launcher liveness; auto-unblocks parked tasks;
reports a health summary to Discord. Run via cron every hour.
"""
import json, subprocess, sys, datetime, urllib.request
from pathlib import Path

from bluu_ink_constants import PARKED_MARKERS

BOTS = Path("C:/Users/bluue/AppData/Local/hermes/bots")
VENV = "C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
CONFIG = Path("C:/Users/bluue/AppData/Local/bluu-ink/config.json")
DEPTS = ["forge", "sanctuary", "harmony"]
STALE_THRESHOLD_MIN = 90  # if a board's done count hasn't moved in 90 min, flag it


def load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_webhook():
    cfg = load_json(CONFIG)
    return (cfg.get("discord", {}) or {}).get("webhook_url", "")


def post_discord(webhook, text):
    if not webhook or "YOUR_WEBHOOK" in webhook:
        print("no webhook, skipping discord")
        return
    payload = {"content": text}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Hermes-StallAudit")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print("discord:", r.status)
    except Exception as e:
        print("discord err:", e)


def unblock_parked(board_path):
    """Move 'blocked' tasks back to ready (so they retry after fixes).

    SKIPS tasks the dispatcher parked after repeated failed builds (note
    contains 'parked after N failed attempts') — those need spec/scope review,
    not blind retry. Only transiently-blocked tasks are unblocked.
    """
    board = load_json(board_path)
    cols = board.get("columns", {})
    blocked = cols.get("blocked", [])
    if not blocked:
        return 0
    keep, unblock = [], []
    # Sweep #5: ANY parked marker is the dispatcher's retry-cap/scope-review
    # park — leave for review. Previously only "parked after ... failed"
    # (retry-cap) protected the note; "parked: fabricated", "parked: wire
    # step failed" and "parked: cloud review rejected" were silently
    # resurrected here -> the exact infinite-retry loop the marker protocol
    # exists to prevent. Use the shared constants as the single source.
    for t in blocked:
        note = (t.get("note") or "").lower()
        if any(m.lower() in note for m in PARKED_MARKERS):
            keep.append(t)  # dispatcher park — leave for review
        else:
            unblock.append(t)
    for t in unblock:
        t["status"] = "ready"
        t.pop("fail_count", None)
        t.pop("blocked_reason", None)
        cols.setdefault("ready", []).append(t)
    cols["blocked"] = keep
    with open(board_path, "w", encoding="utf-8") as f:
        json.dump(board, f, indent=2)
    return len(unblock)


def launcher_running():
    """Check if any launcher.py process is alive."""
    try:
        out = subprocess.run(
            ["powershell", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\").CommandLine"],
            capture_output=True, text=True, timeout=30)
        return "launcher.py" in out.stdout
    except Exception:
        # fallback: if any board updated recently, assume alive
        for d in DEPTS:
            p = BOTS / d / "kanban" / "board.json"
            if p.exists() and (datetime.datetime.now() - datetime.datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() < 600:
                return True
        return False


def restart_launcher():
    """Start launcher.py detached (no waiting). Returns pid or error."""
    try:
        subprocess.Popen(
            [str(VENV), str(BOTS / "launcher.py")],
            cwd=str(BOTS),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "restart issued"
    except Exception as e:
        return f"restart FAILED: {e}"


def trigger_dispatcher():
    """Fire the real-work dispatcher cron so it picks up any available work.
    Used to actively FIX empty-board stalls rather than just reporting them."""
    try:
        r = subprocess.run(
            [str(VENV.replace("python.exe", "hermes.exe")), "cron", "run", "a28bec9eccb7"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception as e:
        print("dispatcher trigger err:", e)
        return False


def dispatcher_wedged(threshold_min=5):
    """True only if a 'running' dispatcher execution row is claimed by a gateway
    process that is now DEAD (crashed) — a legit in-flight agent build takes
    5-15+ min and must NOT be treated as wedged. This is the fix for the earlier
    false-trigger loop that kept killing healthy dispatcher runs and spawning
    duplicate gateways."""
    db = Path("C:/Users/bluue/AppData/Local/hermes/cron/executions.db")
    try:
        import sqlite3
        conn = sqlite3.connect(str(db)); cur = conn.cursor()
        rows = cur.execute(
            "SELECT pid, started_at, claimed_at FROM executions "
            "WHERE job_id LIKE 'a28bec9eccb7%' AND status='running'").fetchall()
        if not rows:
            return False
        for pid, started_at, claimed_at in rows:
            if not pid:
                continue
            # Check if the claiming gateway PID is STILL ALIVE. If it is, the run
            # is legitimately executing — do NOT touch it.
            try:
                alive = subprocess.run(
                    ["powershell", "-Command", f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
                    capture_output=True, text=True, timeout=15)
                if pid and "python" in alive.stdout.lower():
                    return False  # gateway alive -> legit run, not wedged
            except Exception:
                return False  # can't confirm dead -> don't interfere
            return True  # pid dead but row still 'running' -> truly wedged
        return False
    except Exception:
        return False


def clear_wedged_rows():
    """Force-fail any 'running' dispatcher rows so the in-flight guard clears."""
    db = Path(__file__).parent / "cron" / "executions.db"
    if not db.exists():
        db = Path("C:/Users/bluue/AppData/Local/hermes/cron/executions.db")
    try:
        import sqlite3
        conn = sqlite3.connect(str(db)); cur = conn.cursor()
        cur.execute("UPDATE executions SET status='failed', finished_at=datetime('now'), "
                    "error='auto-cleared wedged in-process run (stall audit)' WHERE status='running'")
        conn.commit()
        return cur.rowcount
    except Exception as e:
        print("clear_wedged err:", e)
        return 0


def main_gateway_running():
    """True if the MAIN cron gateway (no --profile) is alive. The dispatcher's
    scheduler runs in the main gateway, so if it's down nothing fires."""
    try:
        out = subprocess.run(
            ["powershell", "-Command",
             "(Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\").CommandLine"],
            capture_output=True, text=True, timeout=30)
        # main gateway = 'gateway run' WITHOUT '--profile'
        for line in out.stdout.splitlines():
            if "gateway run" in line and "--profile" not in line:
                return True
        return False
    except Exception:
        return False


def restart_gateway_detached():
    """Start a fresh MAIN cron gateway IF none is running. Does NOT kill anything
    (that was the bug — killing a healthy gateway mid-run or spawning duplicates).
    Uses gateway_restart.py via pythonw.exe so NO console window ever appears
    (the old .bat left a visible cmd window on every run)."""
    pyw = "C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/pythonw.exe"
    script = "C:/Users/bluue/AppData/Local/hermes/scripts/gateway_restart.py"
    try:
        subprocess.Popen(
            [pyw, script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print("gateway restart err:", e)
        return False


def audit():
    lines = ["🔧 **Bluu Ink Stall Audit**", f"🕐 {datetime.datetime.now().strftime('%H:%M UTC')}"]
    problems = []
    all_healthy = True
    fired_dispatcher = False

    for d in DEPTS:
        board_path = BOTS / d / "kanban" / "board.json"
        board = load_json(board_path)
        cols = board.get("columns", {})
        counts = {k: len(cols.get(k, [])) for k in ("backlog", "ready", "in_progress", "done", "blocked")}
        mtime = datetime.datetime.fromtimestamp(board_path.stat().st_mtime) if board_path.exists() else None
        age_min = int((datetime.datetime.now() - mtime).total_seconds() / 60) if mtime else -1

        unblocked = unblock_parked(board_path)

        status = f"{d}: done={counts['done']} running={counts['in_progress']} pending={counts['backlog']+counts['ready']} blocked={counts['blocked']}"
        stalled = counts["in_progress"] == 0 and counts["backlog"] + counts["ready"] == 0
        if stalled:
            status = f"🔴 {d}: EMPTY — no tasks running/pending (STALL)"
            problems.append(d)
            all_healthy = False
            # ACTIVE FIX: fire dispatcher so it pulls forward-backlog work into this board
            if not fired_dispatcher:
                fired_dispatcher = trigger_dispatcher()
        elif age_min > STALE_THRESHOLD_MIN:
            status += f" ⚠️ stale {age_min}min"
            all_healthy = False
        if unblocked:
            status += f" (+{unblocked} unblocked)"
        lines.append(status)

    if fired_dispatcher:
        lines.append("🔄 **Dispatcher auto-triggered to replenish stalled board(s)**")

    # WEDGE HEALING: DISABLED — the scheduler's own 300s fire_claim TTL already
    # handles genuinely-stale claims. Clearing 'running' rows here was killing
    # legitimate in-flight dispatcher builds (5-15+ min each) every 15 min,
    # which is exactly what made tasks look 'blocked'. Do NOT clear running rows.
    # Only report gateway status; never touch in-flight work.
    if not main_gateway_running():
        lines.append("🚑 **Main cron gateway DOWN — manual restart needed**")
        all_healthy = False
        problems.append("gateway-down")

    # Watchdog: launcher deliberately off; production via dispatcher cron.
    if not main_gateway_running():
        lines.append("🟡 main cron gateway NOT running (stall-audit will restart on next pass)")
    else:
        lines.append("🟢 main cron gateway running")

    report = "\n".join(lines)
    if all_healthy:
        report += "\n✅ All systems healthy — no stalls."
    else:
        report += f"\n🚨 ACTION: {'dispatcher triggered' if fired_dispatcher else 'see above'}"
    print(report)
    post_discord(get_webhook(), report)


if __name__ == "__main__":
    audit()
