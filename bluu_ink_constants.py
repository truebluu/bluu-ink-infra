#!/usr/bin/env python3
"""Bluu Ink — shared constants for the production pipeline scripts.

Single source of truth for values that MUST stay in sync across the
standalone cron scripts (dispatcher_singlebuild.py, hourly_verify.py).

WHY (2026-09-06, sweep #4): PARKED_MARKERS was duplicated as a hand-copied
literal in two files. Every NEW park-note prefix (e.g. "cloud review rejected")
had to be added in BOTH places; missing one caused hourly_verify to resurrect a
parked task into an infinite retry loop. Import from here instead — edit ONE place.

DEPLOYMENT RULE: this file lives in the same directory as the scripts that
import it (C:/Users/bluue/AppData/Local/hermes/scripts/). Python auto-adds a
script's own directory to sys.path, so `from bluu_ink_constants import ...`
resolves with NO explicit sys.path manipulation. Do not move it out of that dir.
"""
from pathlib import Path
import json
import os
import time

# Which board-note substrings mark a task as PARKED (dead, excluded from the
# pending count and the pick pool, and eligible for unblock_parked/auto-heal).
# CRITICAL: park-note prefixes MUST be matched here, and RETRY-notes must NOT
# collide with any marker (a retry note that contains a marker makes the task
# invisible to the picker -> silent starvation).
# Every marker requires the "parked" prefix so a NON-parked retry note can
# never be mistaken for a parked task (e.g. a "cloud review rejected: ..."
# retry note would collide with a bare "cloud review rejected"). Verified
# against every park-note writer 2026-09-06 (sweep #4, kimi):
#   "parked after N failed attempts"
#   "parked: fabricated EventBus API <bad>"
#   "parked: wire step failed [...]"        (contains "wire step failed" for
#                                             cloud_wire_worker's substring match)
#   "parked: cloud review rejected [...]"
PARKED_MARKERS = (
    "parked after",
    "parked: fabricated",
    "parked: wire step failed",
    "parked: cloud review rejected",
)

# File lock: a lock file older than this many seconds is considered stale
# (crashed holder) and may be stolen. MUST exceed the longest wire_artifact.py
# subprocess timeout (1800s) so a slow-but-alive wire is never stolen and
# run twice concurrently on the same repo (git conflict/corruption).
#   - cloud_wire_worker.run_wire: subprocess timeout=1800
#   - dispatcher_singlebuild wire step: timeout=1800
# Raised 900 -> 2000 (sweep #4, ultra Finding 2).
LOCK_STALE_SECS = 2000


def parked(note: str) -> bool:
    """True if a task note carries any parked marker (case-sensitive)."""
    return any(m in str(note or "") for m in PARKED_MARKERS)


# --------------------------------------------------------------------------
# RUN LOCK (single-instance guard)
# --------------------------------------------------------------------------
# Root cause of the "sanctuary/harmony/galaga created at the same time timeout
# + GPU timeouts at the same time" (2026-09-06, sweep #5): the dispatcher cron
# fires EVERY 5 MIN but a run can take 30+ minutes (up to 3 wire steps, each a
# 1800s subprocess, run serially across 3 depts). Nothing stopped a SECOND
# dispatcher from starting while the first was still running. Two concurrent
# dispatchers then (a) BOTH hammer Ollama /api/generate -- each call_model has
# a 600s hard timeout, and with the resident model busy serving the other
# process the call blows past 600s -> timeout + task parked as build-failed;
# and (b) BOTH clear_godot_cache() (rmtree .godot) then run Godot --import on
# the SAME project -> cache-delete race + concurrent import on the GPU -> import
# errors/hangs -> the GPU "times out". The cloud wire worker (15-min cron) has
# the same overrun shape.
#
# The fix is a RUN lock identical in pattern to the proven wire.lock
# (O_CREAT|O_EXCL, pid written, stale-steal). Every pipeline ENTRY script
# (dispatcher, cloud wire worker) acquires it at the very top of main(); if it
# is held by a LIVE process (mtime fresh), the script EXITS immediately
# (skips this cron tick) rather than running concurrently. The run lock is
# SEPARATE from wire.lock -- wire.lock serializes a single git-wire op between
# the two processes; run.lock guarantees only ONE pipeline process total is
# alive at a time (so the wire.lock serialization can never deadlock against
# itself or against a double Godot --import on the same project).
#
# RUN_LOCK_STALE_SECS must exceed the longest single pipeline run. Dispatcher
# runtime upper bound ~ 3 tasks x (model call up to ~10min + grade 5min +
# wire 30min) plus refill/grow_dataset; use a generous ceiling so a legit
# long run is never stolen and run twice.
import socket
RUN_LOCK_STALE_SECS = 10800  # 3h; exceeds worst-case 3x-wire(1800s)=5400s + 3x model-call(600s) + refill ceiling

# Per-entry-script lock path so dispatcher and cloud_wire_worker each guard
# their OWN concurrency (they run on different crons and MAY legally both be
# alive; the run lock prevents TWO COPIES OF THE SAME SCRIPT). Two different
# scripts sharing a run lock would make the dispatcher skip while the cloud
# worker runs -- that's fine and even safer. Use a shared lock: only one
# pipeline entry point may be active at a time.
RUN_LOCK_PATH = Path("C:/Users/bluue/AppData/Local/bluu-ink/state/run.lock")


def acquire_run_lock() -> bool:
    """Try to take the single-instance RUN lock. Returns True if acquired
    (or if this script owns it), False if a LIVE peer holds it (caller must
    exit/skip this tick). Stale-steal after RUN_LOCK_STALE_SECS."""
    try:
        RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()} {socket.gethostname()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - RUN_LOCK_PATH.stat().st_mtime
            if age >= RUN_LOCK_STALE_SECS:
                try:
                    RUN_LOCK_PATH.unlink()
                except OSError:
                    pass
                # retry acquire once
                try:
                    fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, f"{os.getpid()} {time.time()} {socket.gethostname()}".encode())
                    os.close(fd)
                    return True
                except FileExistsError:
                    return False
                except OSError:
                    return False
            return False
        except OSError:
            # lock vanished between stat and here -- peer released; retry once
            try:
                fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()} {socket.gethostname()}".encode())
                os.close(fd)
                return True
            except OSError:
                return False
    except OSError:
        return False


def release_run_lock():
    """Best-effort release of the RUN lock. Only unlink if we own it
    (pid matches) so we never delete a peer's fresh lock."""
    try:
        if RUN_LOCK_PATH.exists():
            pid = RUN_LOCK_PATH.read_text(encoding="utf-8").split()[0].strip()
            if str(os.getpid()) == pid:
                RUN_LOCK_PATH.unlink()
    except Exception:
        pass  # best-effort; stale-steal handles a left lock


def atomic_write_json(path, data, indent=1):
    """Atomically replace a JSON file with a PID-unique temp name.

    Atomic vs. in-place truncation: a concurrent reader never sees a
    half-written file (fixes the 2-week-deadlock board-corruption class).
    PID-unique temp name vs. a deterministic `.json.tmp`: two writers (e.g.
    dispatcher every 5min and hourly_verify hourly, both writing board.json)
    would otherwise truncate the SAME temp file then both os.replace -> lost
    update / stray tmp. Sweep #4 (deepseek Finding): this supersedes every
    hand-rolled `p.with_suffix(\".json.tmp\")` copy.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{int(time.time()*1000)}.tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    os.replace(tmp, p)
