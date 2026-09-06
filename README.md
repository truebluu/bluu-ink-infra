# Bluu Ink Studio — Production Pipeline (infra)

Version-controlled copy of the live production automation that runs Bluu Ink Studio's
game-dev pipeline. These are the scripts the Hermes cron jobs execute; they live in
`C:/Users/bluue/AppData/Local/hermes/scripts/` on the box and are mirrored here so
fixes are preserved, reviewable, and revertible.

> **Deployed-from note:** the canonical runtime copy is the one under
> `hermes/scripts/`. After committing a change here, re-deploy by copying the file
> back into `hermes/scripts/` (or keep them in sync). Always `rm -rf __pycache__`
> in `hermes/scripts/` after deploying, or the launcher runs stale bytecode.

## Scripts

| File | Role | Cron |
|------|------|------|
| `dispatcher_singlebuild.py` | **Producer** — pulls tasks, generates GDScript via local/fallback models, grades, integration-checks, parks/bumps, reconciles cloud-wire sidecar, auto-refills idea pool | dispatcher (5m) |
| `wire_artifact.py` | **Wirer** — validates + merges a produced artifact into the game repo via a feature branch, runs the playtest gate, fails closed on any error | inline (dispatcher + cloud worker) |
| `cloud_wire_worker.py` | **Cloud wirer / parallel worker** — wires parked wire-failed tasks via the free remote model, commits+merges, writes sidecar | cloud-wire-worker (15m) |
| `hourly_verify.py` | **Health/unblock** — unblocks non-parked blocked tasks, verifies GPU/gateway, fires producer on empty boards | hourly-verify (1h) |
| `playtest_runner.py` | **Fun-playtest harness** — headless Godot run scoring beatability/dodge/escalation/softlock/boss/powerup; gates merges | tied to wiring |
| `stall_audit.py` | Monitor — flags stalls, auto-restarts gateway (pythonw) | stall-audit (15m) |
| `gpu_watchdog.py` | Monitor — watches GPU/VRAM | gpu-watchdog (5m) |
| `prod_monitor.py` | Monitor — production health | nano-prod-monitor (30m) |
| `cloud_replenisher.py` | Maintains the free remote-model idea/wire pool | with dispatcher |
| `grade_rubric.py` | **Canonical grading rubric** — scores generated GDScript; wired into dispatcher + wire gate | referenced |

## Current state

- All production crons are **PAUSED** while the pipeline is under review sweep
  (2026-09-06). Do not resume until sweeps are clean.
- Latest pipeline review: **sweep #2** (3 models) confirmed the 13 fixes correct and
  found 3 more (parked-marker gap, targeted-retry key bug, artifact-commit-fail).

## Commit hygiene

- Scripts use **CRLF** line endings (Windows). Do not let a tool normalize them to
  LF wholesale, or patches become noisy. Configure `core.autocrlf` / `.gitattributes`
  to keep line endings stable.
- This repo is **infra only** — no game `.gd`, no training artifacts, no secrets.
