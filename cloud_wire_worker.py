#!/usr/bin/env python3
"""Bluu Ink — Cloud-parallel wire worker.

Wires Forge tasks that the inline Godoter wire step PARKED (note contains
"wire step failed") using the FREE remote cloud model deepseek-v4-pro:cloud.
Runs as a SEPARATE cron so it parallelizes with the dispatcher's serial inline
wire WITHOUT swapping the proven wirer (the inline-swap mistake, 2026-09-04).

BOARD-SAFETY (hard rule, 2026-09-04): this worker only READS board.json. It
NEVER writes it — the 5-min dispatcher cron (a28bec9eccb7) owns board writes;
concurrent writers corrupt board.json. On a successful wire it appends to the
durable grade ledger + writes a sidecar wired_tasks.json that the dispatcher
reconciles into 'done' on its next tick (inside its own board-write context).

GIT-SAFETY: acquires a lock file so it never races the dispatcher's inline
wire on the Galage repo. Yields to the dispatcher (if the lock is held, skip
this run). The dispatcher's wire step also checks the same lock.

Usage:
    python cloud_wire_worker.py [--max N] [--dry-run] [--dept forge]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BOTS = Path("C:/Users/bluue/AppData/Local/hermes/bots")
GALAGE = Path("C:/Users/bluue/Documents/Galage")
SANCTUARY = Path("C:/Users/bluue/Documents/Sanctuary")
VENV = r"C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
WIRE_HOOK = "C:/Users/bluue/AppData/Local/hermes/scripts/wire_artifact.py"
CLOUD_MODEL = "deepseek-v4-pro:cloud"  # free remote Ollama model (layers:[], proxied to ollama.com)
STATE_DIR = Path("C:/Users/bluue/AppData/Local/bluu-ink/state")
LOCK = STATE_DIR / "wire.lock"
SIDECAR = STATE_DIR / "wired_tasks.json"
LEDGER = STATE_DIR / "grade_ledger.jsonl"

# Per-dept wiring target (2026-09-04): each department wires into its OWN Godot
# project so reverting one game never breaks the other (3-repo isolation).
# Harmony classes are cross-game shared and wire into Galage (primary playable).
PROJECTS = {
    "forge": GALAGE,
    "sanctuary": SANCTUARY,
    "harmony": GALAGE,
}

LOCK_STALE_SECS = 900  # a lock older than 15 min is stale (crashed holder) -> steal it


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def acquire_lock(timeout=30):
    """File-based lock. Returns True if acquired, False if held (yield).
    FIXED 2026-09-06: previously `if not LOCK.exists(): LOCK.write_text(...)`
    was a TOCTOU race — two processes could both see not-exists and both write,
    so both thought they held the lock. Now uses atomic O_CREAT|O_EXCL so only
    one process can ever create the lock file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_utcnow()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            # stale lock (crashed holder) -> steal
            try:
                age = time.time() - LOCK.stat().st_mtime
                if age > LOCK_STALE_SECS:
                    LOCK.unlink()
                    continue
            except OSError:
                pass
        time.sleep(2)
    return False


def release_lock():
    try:
        LOCK.unlink()
    except OSError:
        pass


def load_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def find_parked_wire_failed(board):
    """Return tasks whose note carries the wire-failed parked marker, in any column."""
    out = []
    for col in ("backlog", "ready", "blocked"):
        for t in board.get("columns", {}).get(col, []):
            if not isinstance(t, dict):
                continue
            note = t.get("note", "") or ""
            if "wire step failed" in note:
                out.append(t)
    return out


def already_wired(task_id):
    if not SIDECAR.exists():
        return False
    try:
        data = json.loads(SIDECAR.read_text(encoding="utf-8"))
        return any(e.get("task_id") == task_id for e in data)
    except Exception:
        return False


def record_sidecar(task_id, artifact, feature_id, title):
    entries = []
    if SIDECAR.exists():
        try:
            entries = json.loads(SIDECAR.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entries.append({
        "task_id": task_id, "artifact": str(artifact),
        "feature_id": feature_id, "title": title, "wired_at": _utcnow(),
    })
    SIDECAR.write_text(json.dumps(entries, indent=1), encoding="utf-8")


def record_ledger(dept, task_id, title, artifact, score, note):
    """Append to the durable grade ledger (append-only, safe from board clobber)."""
    try:
        sys.path.insert(0, "C:/Users/bluue/AppData/Local/hermes/scripts")
        from grade_ledger import record_grade
        record_grade(dept, task_id, title=title, artifact=str(artifact),
                     score=score, grade=None, note=note)
    except Exception as e:
        print(f"  ⚠ ledger write failed: {e}", flush=True)


def run_wire(task, dry_run, project, dept):
    """Run wire_artifact.py with the cloud model. Returns (rc, feature_id)."""
    artifact = project / "scripts" / (task.get("output_rel", "").split("/")[-1])
    if not artifact.exists():
        # fall back to task id naming
        artifact = project / "scripts" / f"{task['id'].lower()}.gd"
    if not artifact.exists():
        print(f"  ✗ {task['id']}: artifact missing ({artifact})", flush=True)
        return 2, None
    cmd = [VENV, WIRE_HOOK, "--dept", dept, "--task", task["id"],
           "--artifact", str(artifact), "--title", task.get("title", ""),
           "--playtest-gate"]
    if dry_run:
        cmd.append("--dry-run")
    env = {**os.environ, "WIRE_MODEL": CLOUD_MODEL}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
    except subprocess.TimeoutExpired:
        print(f"  ✗ {task['id']}: wire timed out (1800s)", flush=True)
        return 2, None
    out = (r.stdout or "") + (r.stderr or "")
    # extract feature id from the success line "WIRED + MERGED Fxx"
    import re
    m = re.search(r"WIRED \+ MERGED (F\d+)", out)
    feature_id = m.group(1) if m else None
    # FIXED 2026-09-06: dry-run must NOT record a sidecar entry. Previously a
    # dry-run success set feature_id="DRY-RUN", which got recorded in the sidecar
    # and the dispatcher marked the task DONE even though nothing merged — a
    # false-done. Dry-run is a validation-only path; it must never touch the
    # sidecar/ledger or the board.
    if dry_run:
        feature_id = None
    print(out[-1500:], flush=True)
    return r.returncode, feature_id


def post_discord(msg: str):
    """Post a compact status line to the Bluu Ink Discord via config.json webhook."""
    try:
        import json as _json, urllib.request as _ur
        cfg = _json.load(open("C:/Users/bluue/AppData/Local/bluu-ink/config.json", encoding="utf-8"))
        url = cfg["discord"]["webhook_url"]
        body = _json.dumps({"content": msg}).encode("utf-8")
        req = _ur.Request(url, data=body, headers={"Content-Type": "application/json",
                                                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
        with _ur.urlopen(req, timeout=15) as r:
            if r.status != 204:
                print(f"  ⚠ discord post rc={r.status}", flush=True)
    except Exception as e:
        print(f"  ⚠ discord post failed: {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=2, help="max tasks to wire per run")
    ap.add_argument("--dry-run", action="store_true", help="validate wiring without merging")
    ap.add_argument("--dept", default=None,
                    help="wire only this dept (forge/sanctuary/harmony); default: all")
    args = ap.parse_args()

    depts = [args.dept] if args.dept else list(PROJECTS.keys())

    if not acquire_lock():
        print("⏭ wire lock held (dispatcher inline wire in progress) — skipping this run", flush=True)
        return 0

    try:
        total_done = 0
        for dept in depts:
            project = PROJECTS[dept]
            bp = BOTS / dept / "kanban" / "board.json"
            board = load_json(bp)
            if not board:
                print(f"❌ cannot read {bp}", flush=True)
                continue
            parked = find_parked_wire_failed(board)
            # filter out already-wired + junk-titled (meta-instruction echoes)
            import re
            junk = re.compile(r"output exactly|each line|format: TITLE|We need to output|We need to propose|我们只需要", re.I)
            candidates = []
            for t in parked:
                if already_wired(t["id"]) or junk.search(t.get("title", "")):
                    continue
                # skip tasks whose artifact was quarantined (file moved out of project/scripts)
                art = project / "scripts" / (t.get("output_rel", "").split("/")[-1])
                if not art.exists():
                    art = project / "scripts" / f"{t['id'].lower()}.gd"
                if not art.exists():
                    print(f"  ⏭ {t['id']}: artifact quarantined/missing — skipping", flush=True)
                    continue
                candidates.append(t)
            print(f"🔌 cloud wire worker [{dept}]: {len(candidates)} parked wire-failed tasks to retry "
                  f"(of {len(parked)} parked)", flush=True)
            if not candidates:
                print("  (none — all parked tasks already wired or junk-titled)", flush=True)
                continue

            done = 0
            for t in candidates[: args.max]:
                print(f"  → {t['id']}: {t.get('title','')[:50]}", flush=True)
                rc, feature_id = run_wire(t, args.dry_run, project, dept)
                if rc == 0 and feature_id:
                    record_sidecar(t["id"], project / "scripts" / f"{t['id'].lower()}.gd",
                                   feature_id, t.get("title", ""))
                    record_ledger(dept, t["id"], t.get("title", ""),
                                  project / "scripts" / f"{t['id'].lower()}.gd",
                                  None, f"cloud-wired {feature_id}")
                    print(f"  ✅ {t['id']}: WIRED {feature_id} (sidecar recorded; dispatcher will mark done)", flush=True)
                    post_discord(f"🔌 Cloud wire worker: **{t['id']}** wired via free cloud model → **{feature_id}** ({t.get('title','')[:60]})")
                    done += 1
                elif rc == 0 and not feature_id:
                    print(f"  ⚠ {t['id']}: wire returned 0 but no feature id parsed — check output", flush=True)
                else:
                    print(f"  ✗ {t['id']}: wire failed (rc={rc}) — leaving parked", flush=True)
            print(f"🔌 cloud wire worker [{dept}]: {done} wired this run", flush=True)
            total_done += done
        print(f"🔌 cloud wire worker: {total_done} wired total this run", flush=True)
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
