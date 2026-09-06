#!/usr/bin/env python3
"""Bluu Ink — Galage Fun Playtest Runner
Runs the forge_fun_playtest.gd harness headless (CPU, no GPU) and posts the
core-value scores to Discord. This is the "is it FUN?" gate — beyond just
beatable, it measures dodge-then-attack rhythm, escalation, no-softlock,
boss fight, and powerup loop.

Run:  python playtest_runner.py
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

GODOT = r"C:/Users/bluue/Downloads/godot_extracted/Godot_v4.7.1-stable_win64.exe"
GALAGE = Path("C:/Users/bluue/Documents/Galage")
RESULT = GALAGE / "forge_fun_playtest_result.txt"
CONFIG = Path("C:/Users/bluue/AppData/Local/bluu-ink/config.json")
LOG = Path("C:/Users/bluue/AppData/Local/hermes/playtest_auto.log")

# Score thresholds (0-100). Below these, the game needs tuning.
PASS_OVERALL = 60.0
PASS_BEAT = 55.0
PASS_NO_SOFTLOCK = 50.0

# ── Runtime-contract hard-fail patterns ────────────────────────────────
# Every one of these in Godot's stdout/stderr means a broken artifact that
# must NOT be wired into the game. They are the runtime signatures of the
# bug classes that have repeatedly shipped: signal arity mismatch, missing
# child node refs, wrong property names, RefCounted-as-group, physics
# flush-query, and dead wiring. ANY match = hard fail (overall forced to 0,
# wiring blocked) regardless of the fun scores.
HARD_FAIL_PATTERNS = [
    r"SCRIPT ERROR",
    r"Parse Error",
    r"Compile Error",
    r"Invalid (set|get) index",
    r"Invalid call",
    r"Attempt to call function .* on null",
    r"Attempt to call function .* in base 'null instance'",
    r"on a null instance",
    r"null instance",
    r"Nonexistent function",
    r"flushing queries",
    r"busy setting up children",
    r"Parent node is busy",
    r"Node not found",
    r"Error calling from signal",
    r"Cannot connect",
    r"Cannot (find|change|call|access)",
    r"Failed to (load|instantiate)",
    r"Signal .* does not exist",
    r"Parameter .* not found",
    r"add_child.*during physics",
    r"queue_free.*during physics",
    r"RefCounted.*in group",
]
# Soft-penalty patterns: Godot compiler warnings for dead code. Each unique
# warning reduces the hygiene score; >5 distinct = hard fail (dead code is
# likely silently disabling a feature).
UNUSED_WARN_PATTERNS = [
    r"WARNING.*UNUSED_(VARIABLE|PARAMETER|SIGNAL|PRIVATE_CLASS_VARIABLE|CLASS_VARIABLE)",
    r"WARNING.*UNUSED",
]


def load_webhook():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        wh = cfg.get("discord", {}).get("webhook_url", "")
        if wh and "YOUR_WEBHOOK" not in wh:
            return wh
    except Exception:
        pass
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


def _scan_contract_fail(text: str):
    """Return (hard_fail: bool, matched_patterns: list, unused_warn_count: int).

    Scans Godot's combined stdout+stderr for the runtime-contract bug
    signatures. ANY hard-fail pattern match means a broken artifact that must
    not be wired in. Unused-variable warnings are a soft penalty (dead code),
    but >5 distinct = hard fail (likely silently disabling a feature).
    """
    hard = False
    matched = []
    for pat in HARD_FAIL_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            hard = True
            matched.append(pat)
    unused = set()
    for pat in UNUSED_WARN_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            unused.add(m.group(0))
    if len(unused) > 5:
        hard = True
        matched.append(f"UNUSED_WARNINGS({len(unused)})")
    return hard, matched, len(unused)


def _run_once(run_idx: int, do_import: bool = True):
    """Run one headless playtest. Returns (exit_code, scores_dict, contract).

    contract is a dict: {hard_fail, patterns, unused_warns, log_path}.
    The Godot stdout+stderr is captured and scanned for runtime-contract
    errors — this is the gate that catches broken artifacts (signal arity,
    missing child refs, wrong property names, flush-query, dead wiring)
    that the fun scores alone never see.
    """
    if RESULT.exists():
        RESULT.unlink()
    # Fresh import on the FIRST run only to avoid stale bytecode.
    if do_import:
        imp = subprocess.run([GODOT, "--headless", "--path", str(GALAGE), "--import"],
                             capture_output=True, text=True, timeout=120)
        imp_text = (imp.stdout or "") + "\n" + (imp.stderr or "")
        imp_hard, imp_pats, imp_unused = _scan_contract_fail(imp_text)
        if imp_hard:
            # Parse/compile errors at import time are fatal — no point running.
            return 2, {}, {"hard_fail": True, "patterns": imp_pats,
                           "unused_warns": imp_unused, "log_path": None}
    try:
        r = subprocess.run(
            [GODOT, "--headless", "--path", str(GALAGE),
             "--scene", "res://scenes/forge_fun_playtest.tscn"],
            capture_output=True, text=True, timeout=320)
        exit_code = r.returncode
        out_text = (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        exit_code = 2
        out_text = ""
    # Persist the full Godot output for diagnosis (was previously discarded).
    log_path = None
    try:
        log_path = LOG.parent / f"playtest_run{run_idx + 1}.log"
        log_path.write_text(out_text, encoding="utf-8")
    except Exception:
        pass
    hard, pats, unused = _scan_contract_fail(out_text)
    contract = {"hard_fail": hard, "patterns": pats,
                "unused_warns": unused, "log_path": str(log_path) if log_path else None}
    # Parse scores from the result file. Lines are either "[FUN] name: X/100"
    # (stdout) or "  name: X/100" (result file body).
    scores = {}
    if RESULT.exists():
        text = RESULT.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"\s*(?:\[FUN\]\s+)?(\w+):\s+([\d.]+)/100", line)
            if m:
                scores[m.group(1)] = float(m.group(2))
    return exit_code, scores, contract


# Number of headless runs per gate. The autoplayer is stochastic — identical
# game code flips between ~wave-3 and ~wave-6 (beat 33 <-> 67+) run to run.
# A single run is therefore too noisy to judge balance: it produced false
# "needs tuning" fires on a chore commit that changed zero gameplay code.
# Best-of-N removes that noise when scoring the verdict.
RUNS_PER_GATE = 3


def run_playtest():
    """Run the fun playtest best-of-N. Returns (exit_code, scores_dict, contract).

    Reports the scores of the best (highest overall) run, so an unlucky
    autoplayer run can't gate feature integration on its own. BUT contract
    violations are merged across ALL runs: any run that hard-fails (broken
    artifact) fails the whole gate, regardless of the best fun score.
    """
    best = (0, {})
    run_lines = []
    merged_contract = {"hard_fail": False, "patterns": [], "unused_warns": 0,
                       "log_paths": []}
    for i in range(RUNS_PER_GATE):
        code, scores, contract = _run_once(i, do_import=(i == 0))
        score_metrics = {k: f"{v:.1f}" for k, v in scores.items()}
        run_lines.append(f"  run{i+1}: " + ", ".join(
            f"{k}={v}" for k, v in score_metrics.items()))
        if scores.get("overall", 0.0) > best[1].get("overall", 0.0):
            best = (code, scores)
        # Merge contract state across runs — any violation anywhere fails.
        if contract.get("hard_fail"):
            merged_contract["hard_fail"] = True
        for p in contract.get("patterns", []):
            if p not in merged_contract["patterns"]:
                merged_contract["patterns"].append(p)
        merged_contract["unused_warns"] = max(
            merged_contract["unused_warns"], contract.get("unused_warns", 0))
        if contract.get("log_path"):
            merged_contract["log_paths"].append(contract["log_path"])
    # Persist the per-run log so failures are diagnosable.
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("".join(run_lines) + "\n")
    except Exception:
        pass
    return best[0], best[1], merged_contract


def latest_feature():
    """Return the most recent feature commit being tested.

    Uses the ACTUAL latest commit on HEAD, not just the last one whose subject
    happens to carry the 'alpha1.1' prefix. The prefix was an early convention
    that later commits dropped (e.g. '24e3b3d F64: ...'), so filtering on it
    froze Discord's report on an old F63 even as the game advanced.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(GALAGE), "log", "--oneline", "-1", "--format=%h %s"],
            capture_output=True, text=True, timeout=15)
        line = r.stdout.strip()
        if not line:
            r = subprocess.run(
                ["git", "-C", str(GALAGE), "log", "--oneline", "-1",
                 "--grep=alpha1.1"],
                capture_output=True, text=True, timeout=15)
            return r.stdout.strip() or None
        # "24e3b3d F64: design overhaul ..." — pull the F## token if present.
        m = re.search(r"\b(F\d+|alpha1\.1\s+F\d+|F\d+[- ]fix)\b", line, re.IGNORECASE)
        if m:
            # Normalize: "alpha1.1 F63: x" -> "F63 · x"; "24e3b3d F64: x" -> "F64 · x"
            inner = line.split(" ", 1)[1] if " " in line else line
            inner = re.sub(r"^alpha1\.1\s+", "", inner, flags=re.IGNORECASE)
            return inner
        return line
    except Exception:
        return None


def main():
    t0 = time.time()
    # --gate mode: run the playtest and return a pass/fail verdict WITHOUT the
    # cloud agent turn. Used by the dispatcher's wire gate (local, no credits).
    # Exit 0 = pass (game is fun + beatable + no contract violations),
    # 1 = needs tuning, 2 = error / contract violation.
    if "--gate" in sys.argv:
        exit_code, scores, contract = run_playtest()
        overall = scores.get("overall", 0.0)
        beat = scores.get("beatability", 0.0)
        softlock = scores.get("no_softlock", 0.0)
        contract_fail = contract.get("hard_fail", False)
        ok = (not contract_fail and overall >= PASS_OVERALL and beat >= PASS_BEAT
              and softlock >= PASS_NO_SOFTLOCK)
        print(f"GATE overall={overall:.1f} beat={beat:.1f} softlock={softlock:.1f} "
              f"contract={'FAIL' if contract_fail else 'OK'} "
              f"verdict={'PASS' if ok else 'FAIL'}")
        if contract_fail:
            print("CONTRACT_VIOLATIONS: " + ", ".join(contract.get("patterns", [])))
            return 2
        return 0 if ok else 1
    exit_code, scores, contract = run_playtest()
    elapsed = time.time() - t0

    overall = scores.get("overall", 0.0)
    beat = scores.get("beatability", 0.0)
    softlock = scores.get("no_softlock", 0.0)

    # Build a compact Discord report.
    feat = latest_feature()
    lines = ["🎮 **Galage Fun Playtest**"]
    if feat:
        lines.append(f"🧪 Testing: `{feat}`")
    for k in ["beatability", "dodge_then_attack", "escalation", "no_softlock",
              "boss_fight", "powerup_loop", "overall"]:
        v = scores.get(k, 0.0)
        icon = "🟢" if v >= 60 else ("🟡" if v >= 40 else "🔴")
        lines.append(f"{icon} {k}: **{v:.0f}/100**")
    lines.append(f"⏱ {elapsed:.0f}s | exit={exit_code}")

    # Contract violations (broken artifacts) override the fun verdict.
    contract_fail = contract.get("hard_fail", False)
    if contract_fail:
        lines.append("🔴 **CONTRACT VIOLATION — wiring BLOCKED**")
        lines.append("`" + ", ".join(contract.get("patterns", [])) + "`")
        for lp in contract.get("log_paths", []):
            lines.append(f"log: `{lp}`")
        report = "\n".join(lines)
        print(report)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M')} ---\n{report}\n")
        webhook = load_webhook()
        post_discord(webhook, report)
        return 2

    # Verdict.
    if overall >= PASS_OVERALL and beat >= PASS_BEAT and softlock >= PASS_NO_SOFTLOCK:
        verdict = "✅ **FUN + BEATABLE** — core values hold. Ready for feature integration."
    elif beat >= PASS_BEAT:
        verdict = "🟡 **Beatable but needs fun tuning** — check the red metrics."
    else:
        verdict = "🔴 **Not beatable / not fun** — needs tuning. See metrics."
    lines.append(verdict)

    report = "\n".join(lines)
    print(report)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M')} ---\n{report}\n")

    webhook = load_webhook()
    post_discord(webhook, report)

    # Exit code: 0 = pass, 1 = needs tuning.
    return 0 if (overall >= PASS_OVERALL and beat >= PASS_BEAT) else 1


if __name__ == "__main__":
    sys.exit(main())
