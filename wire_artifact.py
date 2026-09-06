#!/usr/bin/env python3
"""Bluu Ink — Wire + Commit accepted artifacts into the LIVE Galage game.

The standalone dispatcher grades artifacts (forge-NNNN.gd) for quality but
wires NOTHING into the shipped scene — so the game stopped growing last at F64
even though 427 graded class files piled up. This is the missing "does it enter
the playable game" step, run AFTER an artifact clears standalone grading.

Design (per lead-designer decision 2026-09-02):
  - Keep standalone grading in dispatcher_singlebuild.py untouched.
  - This step asks the model for a MINIMAL WIRING SPEC: where to instantiate the
    class and what signals to connect, matching the established pattern (e.g.
    main.gd does `BonusWaveController.new()` + connect + add_child).
  - Apply the spec to main.gd / main.tscn. Godot --import + smoke + static
    validation must pass. On ANY failure we `git checkout` the touched files
    (rollback) so the working game is never harmed — the wire is all-or-nothing,
    atomic per artifact.
  - On success we COMMIT: "alpha1.1 F<next>: <title>" so (a) the game visibly
    grows and (b) playtest_runner's latest_feature() label advances off stale F63.

Safety: never edits main.gd/main.tscn unless the wire applies cleanly AND all
checks pass; never commits a failing state. Dry-run flag for validation.

Run:  python wire_artifact.py --dept forge --task FORGE-1637 \
        --artifact C:/Users/bluue/Documents/Galage/scripts/forge-1637.gd \
        --title "Boss phase 2 mechanics" [--dry-run] [--feature F65]
"""
import argparse
import json
import os
import time
import re
import subprocess
import urllib.request
from pathlib import Path

BOTS = Path("C:/Users/bluue/AppData/Local/hermes/bots")
GALAGE = Path("C:/Users/bluue/Documents/Galage")
SANCTUARY = Path("C:/Users/bluue/Documents/Sanctuary")
GODOT = r"C:/Users/bluue/Downloads/godot_extracted/Godot_v4.7.1-stable_win64.exe"
OLLAMA = "http://127.0.0.1:11434/api/generate"
def get_model() -> str:
    return os.environ.get("WIRE_MODEL", "deepseek-v4-pro:cloud")

MODEL = get_model()
SMOKE = "res://scenes/forge800_main_smoke_test.tscn"
VALIDATE = GALAGE / "scripts" / "validate_integration.py"
VENV = r"C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"

# Per-project wiring config (2026-09-04): the wire tool is no longer Galage-only.
# Each department wires into its OWN Godot project so reverting one game never
# breaks the other (3-repo isolation). Harmony classes are cross-game shared
# (both games expose GameState + EventBus autoloads) and wire into Galage as the
# primary playable target; Sanctuary wires into the Sanctuary game.
PROJECTS = {
    "forge": {
        "root": GALAGE,
        "smoke": "res://scenes/forge800_main_smoke_test.tscn",
        "validate": GALAGE / "scripts" / "validate_integration.py",
        "edit_allow": {"scripts/main.gd", "scripts/enemy_spawner.gd"},
        "ground_truth": "Galage (Galaga retro-shooter)",
    },
    "sanctuary": {
        "root": SANCTUARY,
        "smoke": None,  # Sanctuary uses a SceneTree --script test, not a scene
        "smoke_script": "res://scripts/sanctuary_demo_test.gd",
        "validate": None,
        "edit_allow": {"scripts/main.gd"},
        "ground_truth": "Sanctuary (Taming pet-evolution game)",
    },
    "harmony": {
        "root": GALAGE,  # shared systems integrate into the main game
        "smoke": "res://scenes/forge800_main_smoke_test.tscn",
        "validate": GALAGE / "scripts" / "validate_integration.py",
        "edit_allow": {"scripts/main.gd", "scripts/enemy_spawner.gd"},
        "ground_truth": "Galage (shared cross-game systems wired into the main game)",
    },
}

WIRE_PROMPT = """You are wiring a newly-approved Galage (Godot 4.7) class file into the LIVE game so the feature ships and actually runs. This is REAL, valid, running GDScript — fabrication is fatal.

You emit a SEARCH/REPLACE edit (content-anchored, like Aider/claude). You never invent filenames or anchor strings: every `search_block` you write must be copied VERBATIM from the file content I provide, and it must match EXACTLY once.

== HARD GROUND TRUTH (the ONLY APIs that exist — do NOT invent others) ==
1. **EventBus is a typed-signal relay. It has NO generic emit().** The ONLY valid ways to use it: `EventBus.OnEnemyDestroyed.connect(callable)` or `EventBus.OnEnemyDestroyed.emit(args)`. There is NO `EventBus.emit("name")` and NO string-based `EventBus.connect("name", obj, "method")`. Using those WILL crash the game.
2. **boss.gd (extends Area2D) declares only these signals you MAY connect: `boss.health_changed`, `boss.phase_changed`, `boss.boss_defeated`, `boss.vulnerability_opened`, `boss.vulnerability_closed`, `boss.boss_spawned`.** The spawner instantiates the boss: `var boss: Area2D = _boss_scene.instantiate()` inside `enemy_spawner.gd:_spawn_boss()`. That is where the boss lives.
3. **Produced class files live at `scripts/<taskid>.gd`.** If they call `EventBus.emit` or string-connect, they are WRONG — put a correction in `patch_class`.
4. **Autoloads** (GameState, EventBus, InputRemap, ShipUnlock, ...) are singletons — reference directly, never re-instantiate.
5. **main.gd** top-level scene script uses `var X: Type = Type.new()` + `X.sig.connect(callable)` + `add_child(X)` + `X.begin()`.
6. **Valid GDScript 4**: no nested `func` inside `func`; no `connect` with a string method name (use Callable); tabs for indentation; `-> void` on functions.

TASK: produce a SINGLE JSON object (NO prose, NO code fences). Schema:
{
  "edit_file": "scripts/main.gd",
  "edits": [
    {
      "search_block": "<VERBATIM existing lines from the file, bytes-exact, matches exactly once>",
      "replace_block": "<the same lines with your wire addition (>=3 typed-API lines); keep original lines intact, append new lines>"
    }
  ],
  "patch_class": {"scripts/forge-1637.gd": [{"old":"<exact old line>","new":"<exact corrected line>"}]} | null,
  "explanation": "<one line: how this makes the feature reachable/playable; which boss.gd/EventBus signal you connect>"
}

Rules:
- `edit_file` MUST be "scripts/main.gd" or "scripts/enemy_spawner.gd" (the only files I allow you to touch). NEVER anything else.
- Every `search_block` you output MUST exist byte-exact in the file I give you, and MUST appear exactly once there. Copy it from the file verbatim — tabs and all. Do NOT invent or approximate it.
- `replace_block` = the SAME search_block content (unchanged) PLUS your new typed-API lines. It ABSOLUTELY FORBIDDEN to contain: `EventBus.emit(`, string-method `connect("`, nested `func`, instantiating a boss as a new node (a boss already exists — the spawner owns it).
- If the class calls the non-existent `EventBus.emit` or string-connect, put correction edits in `patch_class`.
- If you cannot honestly wire it honestly, return {"edit_file":"","edits":[],"patch_class":null,"explanation":"NOT_WIRABLE <why>"}."""

# Sanctuary-specific wire prompt (2026-09-04): the Galage prompt above is
# boss/enemy_spawner-specific and would make the model fabricate Galage APIs
# (EventBus.OnEnemyDestroyed, boss.gd, enemy_spawner.gd) inside the Sanctuary
# project. Sanctuary is a Control-based demo (Breeding Lab) with its own
# GameState + EventBus autoloads and CreatureGenome classes.
SANCTUARY_WIRE_PROMPT = """You are wiring a newly-approved Sanctuary (Godot 4.7) class file into the LIVE demo so the feature ships and actually runs. This is REAL, valid, running GDScript — fabrication is fatal.

You emit a SEARCH/REPLACE edit (content-anchored, like Aider/claude). You never invent filenames or anchor strings: every `search_block` you write must be copied VERBATIM from the file content I provide, and it must match EXACTLY once.

== HARD GROUND TRUTH (the ONLY APIs that exist — do NOT invent others) ==
1. **Sanctuary autoloads are GameState and EventBus** (both `extends Node`). GameState holds the breeding/genetics state: `GameState.parent_a`, `GameState.parent_b`, `GameState.offspring`, `GameState.rng`, signals `parents_changed(parent_a, parent_b)` and `offspring_bred(offspring)`, and funcs `new_random_parent(species)`, `set_parents(a, b)`, `breed()`. EventBus signals: `creature_selected(genome)`, `breeding_completed(offspring)`, `stat_view_requested(genome)`. There is NO generic `EventBus.emit("name")` and NO string-based connect — use typed signals only.
2. **CreatureGenome** is the core class (Mendelian genetics + phenotype + stats). It has `generation`, `describe(rng)`, `compute_stat_power(BASE_STATS, rng)`, and `STAT_NAMES`. Reference it directly; never re-instantiate the autoloads.
3. **main.gd** is a `Control` demo scene (Breeding Lab) with `@onready` node refs (labels, buttons) and `_ready()` that connects button presses + GameState signals. Wire new features by connecting their signals to existing UI or by adding a new Control node wired into the scene flow.
4. **Produced class files live at `scripts/<taskid>.gd`.** If they call `EventBus.emit` or string-connect, they are WRONG — put a correction in `patch_class`.
5. **Valid GDScript 4**: no nested `func` inside `func`; no `connect` with a string method name (use Callable); tabs for indentation; `-> void` on functions.

TASK: produce a SINGLE JSON object (NO prose, NO code fences). Schema:
{
  "edit_file": "scripts/main.gd",
  "edits": [
    {
      "search_block": "<VERBATIM existing lines from the file, bytes-exact, matches exactly once>",
      "replace_block": "<the same lines with your wire addition (>=3 typed-API lines); keep original lines intact, append new lines>"
    }
  ],
  "patch_class": {"scripts/<taskid>.gd": [{"old":"<exact old line>","new":"<exact corrected line>"}]} | null,
  "explanation": "<one line: how this makes the feature reachable/playable; which GameState/EventBus signal you connect>"
}

Rules:
- `edit_file` MUST be "scripts/main.gd" (the only file I allow you to touch in Sanctuary).
- Every `search_block` you output MUST exist byte-exact in the file I give you, and MUST appear exactly once there. Copy it from the file verbatim — tabs and all. Do NOT invent or approximate it.
- `replace_block` = the SAME search_block content (unchanged) PLUS your new typed-API lines. It ABSOLUTELY FORBIDDEN to contain: `EventBus.emit(`, string-method `connect("`, nested `func`, or any Galage-specific API (boss.gd, enemy_spawner.gd, OnEnemyDestroyed, ShipUnlock, InputRemap).
- If the class calls the non-existent `EventBus.emit` or string-connect, put correction edits in `patch_class`.
- If you cannot honestly wire it honestly, return {"edit_file":"","edits":[],"patch_class":null,"explanation":"NOT_WIRABLE <why>"}."""


def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), *args],
                          capture_output=True, text=True, timeout=60)


def live_game_context() -> str:
    """Compact current-state summary for the wirer: what is queued (don't
    recreate), what is done/merged (build on it), what is archived (never call
    its functions), and the recent git history (the live game state)."""
    lines = ["=== LIVE GAME STATE (wire against THIS, not assumptions) ==="]
    queued = []
    for d in ["forge", "sanctuary", "harmony"]:
        try:
            b = json.load(open(BOTS / d / "kanban" / "board.json", encoding="utf-8"))
            cols = b.get("columns", {})
            for c in ("backlog", "ready"):
                for t in cols.get(c, []):
                    if isinstance(t, dict):
                        queued.append(f"{t.get('id','?')} [{d}] {t.get('title','?')}")
        except Exception:
            pass
    if queued:
        lines.append("ALREADY-QUEUED (do NOT recreate these — they are pending):")
        lines.append("\n".join("  - " + q for q in queued[:60]))
    try:
        b = json.load(open(BOTS / "forge" / "kanban" / "board.json", encoding="utf-8"))
        cols = b.get("columns", {})
        done = [t.get("title", "?") for t in cols.get("done", []) if isinstance(t, dict)]
        seen, uniq = set(), []
        for t in done:
            if t not in seen:
                seen.add(t); uniq.append(t)
        if uniq:
            lines.append("ALREADY-DONE in forge (build on these, do not redo):")
            lines.append("\n  - " + "\n  - ".join(uniq[-40:]))
        arch = [t.get("title", "?") for t in cols.get("archived_legacy", []) if isinstance(t, dict)]
        if arch:
            lines.append("ARCHIVED in forge (removed — do NOT call their functions):")
            lines.append("\n  - " + "\n  - ".join(arch[-30:]))
    except Exception:
        pass
    try:
        r = subprocess.run(["git", "-C", str(GALAGE), "log", "--oneline", "-15"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            lines.append("RECENT MERGED COMMITS (the live game is at this state):")
            lines.append("\n".join("  " + l for l in r.stdout.strip().splitlines()))
    except Exception:
        pass
    return "\n".join(lines)


def call_model(prompt: str) -> str:
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "temperature": 0.1, "keep_alive": "30m",
                       "num_predict": 16384, "num_ctx": 32768}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    # Bounded retry with backoff on HTTP 429 (rate limit) and 5xx (transient).
    # The free cloud model rate-limits per-minute; without this, a burst of
    # wires fails instantly with 429 instead of waiting out the window.
    max_retries = int(os.environ.get("WIRE_RETRIES", "6"))
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=480) as r:
                data = json.loads(r.read().decode())
            return data.get("response") or data.get("thinking", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = 15 * (2 ** attempt)  # 15s, 30s, 60s, 120s, 240s, 480s
                print(f"  ⏳ call_model HTTP {e.code} (attempt {attempt+1}/{max_retries}) — "
                      f"backing off {wait}s…", flush=True)
                time.sleep(wait)
                continue
            raise
    return ""


def parse_json(text: str) -> dict:
    """Robust JSON extraction. Returns {} if none parseable.
    Tries whole text, then each '{'..'}' candidate (left-to-right), so prose
    containing braces before a trailing JSON dict can't poison the greedy match."""
    if not text:
        return {}
    candidates = []
    for m in re.finditer(r"\{", text):
        end = text.rfind("}")
        if end > m.start():
            candidates.append(text[m.start(): end + 1])
    # whole text first, then largest sub-dict candidates
    if text.strip().startswith("{"):
        candidates.insert(0, text)
    for c in candidates:
        try:
            out = json.loads(c)
            if isinstance(out, dict):
                return out
        except Exception:
            continue
    return {}


def extract_snippet(before: str, after: str) -> str:
    """(unused after search/replace refactor — retained for safety) Return the text
    between two markers, or '' if `after` isn't in `before`."""
    try:
        a = before.index(after)
        return before[a+len(after):]
    except ValueError:
        return ""


FORBIDDEN = [
    "EventBus.emit(",                         # no generic emit
    ".connect(\"",                              # no string-method connect
    ".connect('",                              # no string-method connect (single quote)
]

def validate_spec(spec: dict, artifact: Path, project: Path, edit_allow: set) -> tuple[bool, str]:
    """Reject specs that would reintroduce the known failure modes (scheme-level,
    BEFORE any file is written). Returns (ok, error)."""
    ef = spec.get("edit_file", "")
    if ef not in edit_allow:
        return False, f"edit_file not allowlisted: {ef!r} (allowed: {sorted(edit_allow)})"
    edits = spec.get("edits")
    if not isinstance(edits, list) or not edits:
        return False, "no edits array"
    for i, e in enumerate(edits):
        sb = (e or {}).get("search_block", ""); rb = (e or {}).get("replace_block", "")
        if not sb or not rb:
            return False, f"edit[{i}] missing search_block/replace_block"
        for bad in FORBIDDEN:
            if bad in rb:
                return False, f"edit[{i}] replace_block contains forbidden API: {bad!r}"
        if "func " in rb and f"\tfunc " in rb:
            pass  # allow legit func defs at class scope; nested-checked by GDScript parse
        # search_block must exist exactly once in the target file (content-anchored)
        f = project / ef
        if not f.exists():
            return False, f"edit_file missing: {ef}"
        cnt = f.read_text(encoding="utf-8").count(sb)
        if cnt != 1:
            return False, f"edit[{i}] search_block matched {cnt}× (need exactly 1) in {ef}"
    # patch_class must point at the real artifact (no invented filenames)
    for rel in (spec.get("patch_class") or {}):
        if rel != artifact.name:
            # allow scripts/<artifact.name> too
            if rel not in (artifact.name, f"scripts/{artifact.name}"):
                return False, f"patch_class file not the artifact: {rel!r}"
        # FIXED 2026-09-06: FORBIDDEN was only checked on replace_block, so a
        # patch_class edit could inject EventBus.emit( / string-connect into the
        # artifact verbatim. EventBus.emit("x") is syntactically valid GDScript
        # (passes --import) and only fails at runtime — a gate bypass. Apply the
        # same FORBIDDEN scan to every patch_class "new" value.
        for e in (spec.get("patch_class") or {}).get(rel) or []:
            new = (e or {}).get("new", "")
            for bad in FORBIDDEN:
                if bad in new:
                    return False, f"patch_class {rel} new-value contains forbidden API: {bad!r}"
    return True, ""


def ws_replace(text: str, old: str, new: str) -> str:
    """Whitespace-tolerant single replacement. Falls back to a whitespace-flexible
    regex that matches any whitespace run (so tab-vs-space drift in the model's
    anchor doesn't kill an otherwise-correct patch). Raises if not found."""
    if old in text:
        return text.replace(old, new, 1)
    _re = re
    # split old into tokens / whitespace runs
    parts = _re.split(r"(\s+)", old)
    pat = ""
    for p in parts:
        if p and p.isspace():
            pat += r"\s+"
        elif p:
            pat += _re.escape(p)
    m = _re.search(pat, text)
    if not m:
        raise ValueError(f"anchor not found (even whitespace-tolerant): {old[:60]!r}")
    return text[:m.start()] + new + text[m.end():]


def apply_wire(project: Path, spec: dict, artifact: Path) -> tuple[bool, str, list[str]]:
    """Apply the wiring spec: patch the artifact's API calls, then apply search/
    replace edits to main.gd / enemy_spawner.gd. Returns (ok, error, written)
    where `written` is the list of project-relative files actually changed —
    the authoritative set to stage/commit (git never guesses paths)."""
    written: list[str] = []
    # 0) Patch class file API corrections (EventBus.emit -> typed), if requested.
    pc = spec.get("patch_class") or {}
    for rel, edits in pc.items():
        # resolve allowed: artifact.name, scripts/<name>, or a scripts/*.gd path
        cand = project / rel if not rel.startswith("scripts/") else project / rel
        if rel == artifact.name:
            cand = project / "scripts" / artifact.name
        elif rel == f"scripts/{artifact.name}":
            cand = project / "scripts" / artifact.name
        if not cand.exists():
            return False, f"patch_class file missing: {rel}", written
        cf = cand
        txt = cf.read_text(encoding="utf-8")
        for e in edits:
            old = e.get("old"); new = e.get("new")
            if not old or new is None:
                return False, f"patch_class edit missing old/new: {rel}", written
            try:
                txt = ws_replace(txt, old, new)
            except ValueError as ex:
                return False, f"patch_class anchor not found in {rel}: {ex}", written
        cf.write_text(txt, encoding="utf-8")
        print(f"  patched {rel}: {len(edits)} API correction(s)", flush=True)
        written.append(str(cf.relative_to(project)).replace("\\", "/"))
    # 1) Apply search/replace edits (content-anchored, exactly-once verified).
    for i, e in enumerate(spec.get("edits") or []):
        sb = e["search_block"]; rb = e["replace_block"]
        f = project / spec["edit_file"]
        before = f.read_text(encoding="utf-8")
        cnt = before.count(sb)
        if cnt != 1:
            return False, f"edit[{i}] search_block matched {cnt}× (need exactly 1)", written
        after = before.replace(sb, rb, 1)
        # Guard (refined 2026-09-03): only reject when the replace_block ADDS a
        # definition line (func / top-level var / class) that the file ALREADY
        # declares elsewhere. Unchanged context lines at the block's edges are
        # standard (Aider-style) and must NOT be treated as duplicates. This was
        # over-aggressive: a correct wire to _assign_fire was rejected because its
        # replace_block ended with the untouched context line 'if difficulty >= 2.0:'.
        # Lines already present in the SEARCH block are anchor/context being
        # preserved, NOT new additions — only flag a definition that is genuinely
        # new (not in sb) yet already exists in the file.
        sb_lines = sb.splitlines()
        added = rb.splitlines()
        for ln in added:
            s = ln.strip()
            if not s or any(x.strip() == s for x in sb_lines):
                continue  # blank or anchor/context line — legitimately preserved
            if re.match(r"^(func|class_name)\s", s) and any(
                    x.strip() == s for x in before.splitlines()):
                return False, f"edit[{i}] replace would add duplicate definition: {s!r}", written
            if re.match(r"^var\s+\w+\s*[:=]", s) and any(
                    x.strip() == s for x in before.splitlines()):
                return False, f"edit[{i}] replace would add duplicate var: {s!r}", written
        f.write_text(after, encoding="utf-8")
        print(f"  wired {spec['edit_file']} edit[{i}]: applied search/replace", flush=True)
        written.append(str(f.relative_to(project)).replace("\\", "/"))
    if not spec.get("edits") and not spec.get("patch_class"):
        return False, "spec has no edits and no patch_class", written
    return True, "", written


def validate_game(project: Path, cfg: dict) -> tuple[bool, str]:
    """--import + smoke + static validation. Returns (ok, error)."""
    # STALE-CACHE GOTCHA (2026-09-04): Godot caches compiled scripts in .godot/.
    # If the wirer added a NEW class file (e.g. forge-1671.gd -> WaveModifierSystem),
    # a stale cache won't register the global class and --import reports a false
    # "Could not parse global class" Parse Error. Clear the cache so --import
    # recompiles from source and the new class is actually registered.
    import shutil
    cache = project / ".godot"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    imp = subprocess.run([GODOT, "--headless", "--path", str(project), "--import"],
                         capture_output=True, text=True, timeout=180)
    if imp.returncode != 0:
        return False, f"godot --import failed (rc {imp.returncode})"
    # Godot --import returns 0 even when scripts fail to compile (it logs
    # SCRIPT ERROR / Parse Error / Compile Error but does not fail). Scan the
    # output so a broken artifact is caught here, not silently shipped.
    imp_out = (imp.stdout or "") + (imp.stderr or "")
    for pat in ("SCRIPT ERROR", "Parse Error", "Compile Error",
                "Failed to load script", "Compilation failed"):
        if pat in imp_out:
            # Surface the actual error lines so the failure is diagnosable,
            # not just "SCRIPT ERROR". Grab the matching line + context.
            lines = imp_out.splitlines()
            ctx = [l for l in lines if pat in l or "res://" in l or "at:" in l]
            detail = " | ".join(ctx[-6:]) if ctx else imp_out[-300:]
            return False, f"godot --import reported '{pat}': {detail}"
    # Smoke: Sanctuary uses a SceneTree --script test (no scene); Forge/Harmony
    # use the forge800 smoke scene.
    if cfg.get("smoke_script"):
        smoke = subprocess.run([GODOT, "--headless", "--path", str(project),
                                "--script", cfg["smoke_script"]],
                               capture_output=True, text=True, timeout=180)
    else:
        smoke = subprocess.run([GODOT, "--headless", "--path", str(project),
                                "--scene", cfg["smoke"]],
                               capture_output=True, text=True, timeout=180)
    if smoke.returncode != 0:
        return False, f"smoke failed (rc {smoke.returncode}): {smoke.stdout[-400:]} {smoke.stderr[-400:]}"
    # Same hole in the smoke run: it can return 0 while logging SCRIPT ERRORs
    # (assertions still pass because the game runs; the feature silently fails).
    smoke_out = (smoke.stdout or "") + (smoke.stderr or "")
    for pat in ("SCRIPT ERROR", "Parse Error", "Compile Error",
                "Failed to load script", "Compilation failed"):
        if pat in smoke_out:
            return False, f"smoke reported '{pat}' (script compile error)"
    # Static integration validation (Forge/Harmony only; Sanctuary has none).
    if cfg.get("validate"):
        vi = subprocess.run([VENV, str(cfg["validate"]), "--root", str(project),
                             "--output", str(project / "validation_report.json")],
                            capture_output=True, text=True, timeout=180)
        try:
            rep = json.loads(vi.stdout) if vi.stdout.strip().startswith("{") else \
                  json.loads((project / "validation_report.json").read_text(encoding="utf-8"))
            if rep.get("summary", {}).get("errors", 0) != 0:
                return False, f"{rep['summary']['errors']} integration errors"
        except Exception as e:
            return False, f"could not parse validation report: {e}"
    return True, ""


def next_feature_id(project: Path) -> str:
    # FIXED 2026-09-06: previously only read the LAST commit. If the last commit
    # was a chore (no F-number), it returned "F1" — a regression that mislabeled
    # the wire and confused the playtest's latest_feature tracking. Now scans the
    # recent history for the MAX F-number so a chore commit can't reset the count.
    r = git(project, "log", "--oneline", "-50")
    nums = [int(m) for m in re.findall(r"F(\d+)", r.stdout or "")]
    hi = max(nums) if nums else 0
    return f"F{hi + 1}"


def wire_branch_name(task: str) -> str:
    """Feature branch per wire: atomic, revertible, diagnosable. The branch IS
    the rollback mechanism — dropping it reverts every tracked edit with zero
    hand-rolled pathspec logic, and the untracked input artifact is never
    touched (structurally impossible to delete)."""
    return f"wire/{task.lower()}"


def ensure_wire_base_clean(project: Path, spec: dict, artifact: Path) -> list:
    """Only the TRACKED live files this wire will MODIFY must be at their
    committed state before branching, so a stale edit isn't carried onto the
    feature branch. The artifact and patch_class targets are UNTRACKED grader
    outputs (the new class being wired) — they are EXPECTED to be present and
    untracked, so they must NOT be in this check. The rest of the tree is
    intentionally dirty (art churn + dispatcher artifacts) — never check it.
    Returns a list of dirty porcelain lines."""
    targets = set()
    if spec.get("edit_file"):
        targets.add(spec["edit_file"])
    for e in spec.get("edits") or []:
        if e.get("edit_file"):
            targets.add(e["edit_file"])
    dirty = []
    for line in git(project, "status", "--porcelain").stdout.splitlines():
        path = line[3:].strip().strip('"')
        if path in targets:
            dirty.append(line)
    return dirty


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dept", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate wiring WITHOUT committing (no git commit)")
    ap.add_argument("--spec", default=None,
                    help="path to a pre-written wire spec JSON (skip the model call; deterministic tests)")
    ap.add_argument("--playtest-gate", action="store_true",
                    help="after validate, run the Galage fun-playtest headless; if the game is no longer fun+beatable, revert the wire (forge only)")
    args = ap.parse_args()

    project = GALAGE
    cfg = PROJECTS.get(args.dept)
    if cfg is None:
        print(f"❌ unknown dept {args.dept!r} (expected forge/sanctuary/harmony)", flush=True)
        return 2
    project = cfg["root"]
    artifact = Path(args.artifact)
    if not artifact.exists():
        print(f"❌ artifact missing: {artifact}", flush=True)
        return 2

    # --spec injection: load a hand-authored spec so we can prove rollback
    # DETERMINISTICALLY (no model -> no nondeterminism). Otherwise the model
    # writes the spec as usual. `spec` is a module-level name so _drop_wire
    # could reference it in diagnostics.
    if args.spec:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        print(f"↻ {args.task}: loaded hand-authored spec from {args.spec} (deterministic, no model)", flush=True)
    else:
        spec = None

    # 0) Ask the model for a minimal wiring spec (reuses established pattern).
    #    NOTE: feed ONLY the target function(s) (~30 lines) + artifact, never whole
    #    files — whole files overflow the model's context (HTTP 500). Per ultra's
    #    "drastic context reduction" fix.
    def _func_text(path: str, name: str) -> str:
        lines = (project / path).read_text(encoding="utf-8").splitlines()
        out, active = [], False
        for i, ln in enumerate(lines):
            if not active and name in ln and ln.strip().startswith("func"):
                active = True
            if active:
                out.append(f"{i+1}: {ln}")
            if active and out and i < len(lines)-1 and re.match(r"^func ", lines[i+1]):
                break
        return "\n".join(out) or f"(func {name} not found in {path})"

    spawn_ctx = fire_ctx = main_ctx = ""
    if args.dept == "forge" or args.dept == "harmony":
        spawn_ctx = _func_text("scripts/enemy_spawner.gd", "_spawn_boss")
        fire_ctx = _func_text("scripts/enemy_spawner.gd", "_assign_fire")
        spawn_ctx += "\n\n(enemy_spawner.gd ALSO declares signals: boss_spawned, boss_defeated)"
        fire_ctx += "\n\n(NORMAL enemy fire path: _assign_fire(enemy) sets up per-enemy cadence + bullet types. Spawnable feature nodes for projectile patterns belong HERE in the firing path, not in _spawn_boss.)"
        main_ctx = (project / "scripts/main.gd").read_text(encoding="utf-8")
        # trim main.gd to just the spawn-flow connect block (lines ~31-98) + bonus anchor (~491)
        mlines = main_ctx.splitlines()
        main_ctx = "\n".join(
            (f"{i+1}: {mlines[i]}" for i in range(len(mlines))
             if 30 <= i <= 98 or 483 <= i <= 496))
    else:  # sanctuary: main.gd is the only edit target
        main_ctx = (project / "scripts/main.gd").read_text(encoding="utf-8")
        mlines = main_ctx.splitlines()
        main_ctx = "\n".join(f"{i+1}: {mlines[i]}" for i in range(len(mlines)))
    if args.spec is None:
        base_prompt = SANCTUARY_WIRE_PROMPT if args.dept == "sanctuary" else WIRE_PROMPT
        prompt = (base_prompt
                  + f"\n\nARTIFACT PATH: {artifact.name} resides at scripts/{artifact.name} (reference it in patch_class).\n"
                  + f"=== LIVE GAME STATE (what is already merged/queued — wire against THIS) ===\n{live_game_context()}\n"
                  + (f"=== TARGET: enemy_spawner.gd :: _spawn_boss() (boss path; copy search_block from HERE verbatim — must match EXACTLY once) ===\n{spawn_ctx}\n"
                     if spawn_ctx else "")
                  + (f"=== TARGET: enemy_spawner.gd :: _assign_fire() (NORMAL enemy fire path; pick THIS anchor if the artifact is a projectile/shot-pattern system) ===\n{fire_ctx}\n"
                     if fire_ctx else "")
                  + f"=== main.gd relevant lines (connect wiring + bonus anchor; may be your edit target) ===\n{main_ctx}\n"
                  + f"NEW CLASS FILE to wire (this defines the feature's class + signals):\n{artifact.read_text(encoding='utf-8')}")
        # Bounded-retry: models are nondeterministic — a single malformed spec
        # (bad JSON, wrong path, multi-match search_block) must NOT sink a
        # capable model. Retry with a corrective hint. `--dry-run` + a single
        # model per process means VRAM stays predictable.
        max_attempts = int(os.environ.get("WIRE_ATTEMPTS", "3"))
        spec = None
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            print(f"↻ {args.task}: asking {MODEL} for wiring spec (attempt {attempt}/{max_attempts})…", flush=True)
            raw = call_model(prompt)
            spec = parse_json(raw)
            if not spec or (not spec.get("edits") and not spec.get("patch_class")):
                last_err = "no valid wire spec (missing edits/patch_class)"
                print(f"  ✗ attempt {attempt}: {last_err}. raw tail:\n{raw[-500:]}", flush=True)
            elif spec.get("explanation", "").upper().startswith("NOT_WIRABLE"):
                print(f"↷ {args.task}: NOT WIRABLE — {spec.get('explanation')}", flush=True)
                return 1
            else:
                ok, verr = validate_spec(spec, artifact, project, cfg["edit_allow"])
                if ok:
                    break
                last_err = verr
                print(f"  ✗ attempt {attempt}: spec rejected by validator: {verr}", flush=True)
            if attempt < max_attempts:
                # corrective hint: what to fix next attempt
                prompt += f"\n\n[FIX REQUIRED] Previous spec was rejected: {last_err}. Output a fresh, corrected \"edits\" spec — the search_block must match EXACTLY one location in {spec.get('edit_file', 'the target') if spec else 'the target'}."
            spec = None
        if spec is None:
            print(f"❌ {args.task}: no valid spec after {max_attempts} attempts ({last_err})", flush=True)
            return 1
    ok, verr = validate_spec(spec, artifact, project, cfg["edit_allow"])
    if not ok:
        print(f"❌ {args.task}: spec rejected by validator: {verr}", flush=True)
        print(f"   spec was: {json.dumps(spec)[:1200]}", flush=True)
        return 1

    # 2) Apply the wire on an ISOLATED FEATURE BRANCH. The branch IS the
    #    rollback mechanism: git reverts a committed edit atomically when we
    #    drop the branch — no hand-rolled pathspec logic, no mixed
    #    tracked/untracked checkout bugs, and the untracked input artifact is
    #    never deleted (it re-appears as untracked on master after the drop).
    #    Only the files THIS wire edits must start clean; the rest of the tree
    #    is intentionally dirty (art churn + dispatcher artifacts).
    dirty = ensure_wire_base_clean(project, spec, artifact)
    if dirty:
        print(f"⚠️  {args.task}: wire targets not in committed state: {dirty} — aborting",
              flush=True)
        return 2
    br = wire_branch_name(args.task)
    # Fresh branch from HEAD (master). A pre-existing stale branch is deleted:
    # it only ever held a rejected attempt we already dropped.
    if git(project, "rev-parse", "--verify", "--quiet", br).returncode == 0:
        git(project, "branch", "-D", br)
    if git(project, "checkout", "-b", br).returncode != 0:
        print(f"❌ {args.task}: could not create branch {br}", flush=True)
        return 2
    base = git(project, "rev-parse", "--short", "HEAD").stdout.strip()

    # Apply, then COMMIT the wire state on the branch BEFORE validating. A
    # branch-dispatch only reverts *committed* edits — an uncommitted worktree
    # edit survives `git checkout master` and leaks onto master (the original
    # bug, returned via branch instead of pathspec). Commit-first makes the
    # branch drop a true atomic revert. The artifact is committed with the
    # feature, so it enters history on ship and survives the drop as untracked.
    ok, err, written = apply_wire(project, spec, artifact)
    if ok:
        fe = next_feature_id(project)
        msg = f"alpha1.1 {fe}: {args.title or args.task} (wired from {args.task}, branch {br}/{base})"
        # Stage EXACTLY what the wire wrote (authoritative `written` set from
        # apply_wire), EXCLUDING the artifact path (even when patch_class edited
        # it): the artifact is a fresh untracked grader output that must survive
        # rollback untouched. Committing it to the branch would make
        # `reset --hard master` DELETE it on drop (master never had it). It is
        # added to master only on SHIP, after the merge + validation.
        artifact_rel = "scripts/" + artifact.name
        stage = list(dict.fromkeys(w for w in written if w != artifact_rel))
        if not stage:
            # Wire only touched the artifact (pure patch_class) — no tracked
            # edits to stage; commit the branch-empty state so the drop reverts.
            rc = subprocess.run(["git", "-C", str(project), "commit", "--allow-empty", "-m", msg],
                                capture_output=True, text=True)
        else:
            git(project, "add", "--", *stage)
            rc = git(project, "commit", "-m", msg)
        if rc.returncode != 0:
            return _drop_wire(project, br, args, base, f"commit failed ({rc.stderr.strip()})")
        # Validate the COMMITTED state (what actually ships).
        ok, err = validate_game(project, cfg)

    if not ok:
        # Revert ALL tracked wire edits (committed or not) back to the pre-wire
        # base atomically. `git reset --hard master` resets the branch's
        # index+worktree tracked files to base — catching BOTH a mid-way
        # apply_wire failure (patch_class/edit[0] written but edit[1] failed —
        # uncommitted dirty edits that a bare `branch -D` would NOT revert and
        # `checkout` would carry onto master) AND a post-commit validate failure
        # (committed edits). The untracked input artifact is NEVER touched — it
        # survives on master as an untracked file (the deletion bug is
        # structurally impossible). This same drop is used for dry-run and
        # commit-failure: every rejection is atomic, no leaked edits.
        return _drop_wire(project, br, args, base, err or "unknown")

    # PLAYTEST GATE (2026-09-05): the wire is validated (builds+smoke+integrate)
    # but we must ALSO confirm the game is still FUN + BEATABLE after this wire
    # before shipping it. Runs the Galage fun-playtest harness headless (CPU, no
    # GPU, no cloud credits). If the game regressed (overall<60 or beat<55 or
    # softlock<50), revert the wire atomically so master always holds a passing
    # iteration. Forge only — sanctuary/harmony have no playable loop yet.
    if args.playtest_gate and args.dept == "forge":
        import subprocess as _sp
        runner = "C:/Users/bluue/AppData/Local/hermes/scripts/playtest_runner.py"
        try:
            pt = _sp.run([VENV, runner, "--gate"], capture_output=True,
                         text=True, timeout=1200)
            pt_out = (pt.stdout or "") + (pt.stderr or "")
            m = re.search(r"GATE overall=([\d.]+) beat=([\d.]+) softlock=([\d.]+) verdict=(\w+)", pt_out)
            if m:
                overall, beat, softlock, verdict = m.groups()
                if verdict != "PASS":
                    return _drop_wire(project, br, args, base,
                                      f"playtest gate FAIL (overall={overall} beat={beat} softlock={softlock})")
                print(f"  🎮 playtest gate PASS (overall={overall} beat={beat} softlock={softlock})", flush=True)
            else:
                # FAIL CLOSED (2026-09-06): a broken playtest harness (no verdict
                # line, crash, timeout) must NOT ship the wire. Previously this
                # "shipped anyway" — a harness failure silently bypassed the fun/
                # beatability gate, exactly the class of bug the user wants gone.
                return _drop_wire(project, br, args, base,
                                  f"playtest gate no verdict (exit {pt.returncode}): {pt_out[-200:]}")
        except _sp.TimeoutExpired:
            return _drop_wire(project, br, args, base, "playtest gate timed out")
        except Exception as e:
            return _drop_wire(project, br, args, base, f"playtest gate error: {e}")

    if args.dry_run:
        # Validated + committed on a branch WITHOUT merging — drop it to prove
        # the rollback path and leave the game untouched.
        return _drop_wire(project, br, args, base, None, dry_run=True)

    # 3) Ship: fast-forward merge the feature branch into master, then commit
    # the artifact (was kept untracked on the branch so rollback never deletes
    # it; now that validation passed, land it on master).
    if git(project, "checkout", "-q", "master").returncode != 0:
        return 1
    merg = git(project, "merge", "--ff-only", br)
    git(project, "branch", "-D", br) if merg.returncode == 0 else None
    if merg.returncode != 0:
        print(f"❌ {args.task}: fast-forward merge failed ({merg.stderr.strip()})", flush=True)
        return 1
    artifact_rel = "scripts/" + artifact.name
    git(project, "add", "--", artifact_rel)
    ac = git(project, "commit", "-m", f"alpha1.1 {fe}: add artifact {artifact_rel} (wired feature class)")
    if ac.returncode != 0:
        # MERGE OK but artifact not committed → the class file is an untracked
        # orphan on master and the feature is incomplete. Do NOT report success:
        # returning 0 made the dispatcher mark the task done while the wired
        # class was missing from git history (ultra sweep #2 2026-09-06).
        print(f"❌ {args.task}: merge ok but artifact commit ({artifact_rel}) failed: {ac.stderr.strip()}", flush=True)
        return 1
    print(f"✅ {args.task}: WIRED + MERGED {fe} — {msg}", flush=True)
    print(git(project, "log", "--oneline", "-3").stdout.strip(), flush=True)
    return 0


def _drop_wire(project: Path, br: str, args, base: str, reason: str | None,
               dry_run: bool = False) -> int:
    """Atomic, uniform rejection of a wire branch. reset --hard to the branch's
    base (master) FIRST so ANY uncommitted worktree edit from a mid-way
    apply_wire failure is reverted, then checkout master and delete the branch.
    This is the single code path every rejection (apply-fail, validate-fail,
    commit-fail, dry-run) goes through — the original F64/163-leak was a
    `checkout;branch -D` that skipped the reset and carried a dirty edit onto
    master. Never touches the untracked input artifact."""
    git(project, "reset", "--hard", "-q", "master")
    git(project, "checkout", "-q", "master")
    git(project, "branch", "-D", br)
    if dry_run:
        print(f"✅ {args.task}: DRY-RUN wire VALIDATED on branch {br} (game builds+smoke+integrate pass); dropped branch — master unchanged", flush=True)
    else:
        print(f"❌ {args.task}: wire FAILED ({reason}) on branch {br} (base {base}) — reset+drop, game reverted to master unchanged.", flush=True)
    return 0 if dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
