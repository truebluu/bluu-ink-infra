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
