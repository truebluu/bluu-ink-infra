#!/usr/bin/env python3
"""Bluu Ink — build the top-priority pending task per department, one at a time,
with per-task error isolation so one failure doesn't kill the others. Uses
Godoter-27B via Ollama, validates --import, grades with --project-root."""
import json, re, subprocess, datetime, urllib.request, os, time
from pathlib import Path

# Shared constants (single source of truth): PARKED_MARKERS + LOCK_STALE_SECS.
# See bluu_ink_constants.py — same dir, auto-resolved on sys.path. Do NOT
# redefine these here (a divergent hand-copy resurrects parked tasks / steals a
# live wire lock). 2026-09-06 sweep #4 consolidation.
from bluu_ink_constants import atomic_write_json, PARKED_MARKERS, LOCK_STALE_SECS, acquire_run_lock, release_run_lock

BOTS = Path("C:/Users/bluue/AppData/Local/hermes/bots")
GALAGE = Path("C:/Users/bluue/Documents/Galage")
SANCTUARY = Path("C:/Users/bluue/Documents/Sanctuary")
GODOT = "C:/Users/bluue/Downloads/Godot_v4.7.1-stable_win64.exe/Godot_v4.7.1-stable_win64.exe"
GRADE = "C:/Users/bluue/AppData/Local/hermes/scripts/grade_rubric.py"
VENV = "C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
VENV_PY = VENV
WIRE_MODEL = "hf.co/Ruler97/Godoter-27B-GGUF:q4_K_M"  # inline serial wirer (proven). Cloud-parallel via separate cloud_wire_worker.py
WIRE_HOOK = "C:/Users/bluue/AppData/Local/hermes/scripts/wire_artifact.py"
OLLAMA = "http://127.0.0.1:11434/api/generate"
# PRIMARY producer (2026-09-06): Godoter-27B — the proven reliable GDScript writer.
# FALLBACK (2026-09-06): bluu-nano-v4 (fine-tuned GDScript model, 4096-seq).
# 2026-09-06 FLIP: Godoter is now PRIMARY. v4 underperforms on the real dispatcher
# prompt (narrates instead of coding, emits Godot 3 syntax), so every task burned 2
# calls (v4 prose + Godoter real work) and the GPU sat at 97%, tripping the gpu_safe
# gate and starving sanctuary/harmony. Godoter primary keeps production moving; v4
# stays as fallback so the training investment isn't wasted.
MODEL = "hf.co/Ruler97/Godoter-27B-GGUF:q4_K_M"  # primary producer (proven reliable writer)
FALLBACK_MODEL = "bluu-nano-v4:latest"  # fallback if primary emits prose or fails compile
# JUNK-TITLE REGEX (2026-09-04): the idea-generator (bluu-nano-1000, qwen3-style)
# sometimes parrots its format prompt back as a "feature title" ("We need to
# output exactly 12 lines, each line format: TITLE"; Chinese "我们只需要输出一行，
# 格式为TITLE"). These are meta-instruction echoes, NOT features. A real feature
# title is a short noun phrase ("Wave Modifier System"), not a sentence. Used at
# (a) generate_fresh_ideas() to reject junk at creation, (b) refill pending-count
# so junk doesn't block refill, (c) pick-time so pre-existing junk tasks are skipped.
JUNK_TITLE = re.compile(
    r"\b(we|i'?ll|i will|you|the prompt|the model|the format|"
    r"format|output|line|lines|propose|proposal|must|need|ensure|"
    r"check|list|exactly|thus|let'?s|now|make sure|but|so|maybe|"
    r"not already|not in|new feature|department|existing|"
    r"something like|one line|two lines|three lines|"
    r"we need|we must|we should|we can|we want|we have|"
    r"we are|we're|we will|we'd|we've|"
    r"i'll write|i will write|i propose|i suggest|i think|"
    r"tension curve director|audio adaptive|dynamic audio focus|"
    r"我们只需要|格式为|输出一行|输出.*行|请输出|请提供|"
    r"需要输出|必须输出|确保.*格式|格式.*TITLE|"
    r"每个.*格式|一行.*格式|输出格式)\b",
    re.I,
)
def is_junk_title(title: str) -> bool:
    """True if a task title is a meta-instruction echo (not a real feature name)."""
    t = str(title)
    return bool(JUNK_TITLE.search(t)) or len(t.split()) > 8
# Tasks to process per department per dispatcher tick. KEPT AT 1 for GPU safety:
# running multiple 17-24GB models back-to-back at 100% GPU sustained power draw
# triggered nvlddmkm 153 TDR resets on the RTX 5090 (two computer stalls 2026-09-02).
# 13 real GameEventBus signals (harmony infra). Any .emit()/.connect() on the
# bus referencing a signal NOT in this set is a fabricated API -> reject.
EVENTBUS_SIGNALS = {
    "OnCreatureBonded","OnCreatureCaptured","OnCreatureEvolved","OnDebugEvent",
    "OnEnemyDestroyed","OnGamePaused","OnGameStateChanged","OnPlayerHit",
    "OnPowerupCollected","OnScoreChanged","OnSettingsChanged","OnWaveCleared",
    "OnWeaponFired",
}
_BUS_EMIT_CONNECT = re.compile(r"\b(?:EventBus|GameEventBus)\.(\w+)\.(?:emit|connect)\s*\(")
_BUS_STRING_API = re.compile(
    r"\b(?:EventBus|GameEventBus)\.(?:emit|connect|broadcast)\s*\(\s*[\"']")
_BUS_PLAIN_EMIT = re.compile(r"\b(?:EventBus|GameEventBus)\.emit\s*\(")

def api_gate(gd_path: Path) -> list[str]:
    """Reject artifacts wired to fabricated EventBus APIs.
    Returns a list of offending lines (empty = passes)."""
    txt = gd_path.read_text(encoding="utf-8", errors="replace")
    bad = []
    for m in _BUS_EMIT_CONNECT.finditer(txt):
        sig = m.group(1)
        if sig and sig not in EVENTBUS_SIGNALS:
            line = txt.count("\n", 0, m.start()) + 1
            bad.append(f"line {line}: EventBus.{sig} is not a real signal")
    if _BUS_STRING_API.search(txt):
        m = _BUS_STRING_API.search(txt)
        line = txt.count("\n", 0, m.start()) + 1
        bad.append(f"line {line}: string-based EventBus.emit/connect is not supported")
    elif _BUS_PLAIN_EMIT.search(txt):
        m = _BUS_PLAIN_EMIT.search(txt)
        line = txt.count("\n", 0, m.start()) + 1
        bad.append(f"line {line}: bare EventBus.emit() has no generic target")
    return bad


# ============================================================================
# CLOUD REVIEW PASS (2026-09-05, option 2 — acceleration via review, not producer)
# ----------------------------------------------------------------------------
# The compile gate + grade + api_gate catch syntax, prose-dumps, and fabricated
# EventBus signals. They MISS semantic bugs: an artifact that references a
# class_name that doesn't exist in the project, calls a real method with the
# wrong signature, or is a stub that doesn't actually implement the task. Those
# pass grade (55-76) and only fail at the wire step's full-project --import,
# burning hours of production on artifacts that get scrapped.
#
# This pass sends the artifact + a COMPACT symbol index (autoloads, global
# classes, EventBus signals — NOT the 216KB API_INDEX) to the FREE remote
# model deepseek-v4-pro:cloud. It costs zero credits (free :cloud model, not a
# paid one), so it runs on EVERY artifact before the wire step. It returns a
# parseable JSON verdict; on FAIL the reasons are fed back as targeted
# integration_errors so the next build fixes exactly those, and the task parks
# to blocked after MAX_ATTEMPTS (same anti-thrash as api_gate).
# ============================================================================
CLOUD_REVIEW = True          # master switch — set False to disable the pass
CLOUD_REVIEW_MODEL = "deepseek-v4-pro:cloud"   # FREE remote model, 0 credits
CLOUD_REVIEW_TIMEOUT = 120   # free model is fast (~12M tok/s); generous cap

# Compact symbol index, built once from agents.md (the reference already lists
# autoloads + the full global-class set + resource paths). Cached so we don't
# re-read the file every tick.
_SYMBOL_CACHE_STR = None
_SYMBOL_CACHE_DICT = None
def _compact_symbols() -> str:
    """Return a compact 'known symbols' block for the review prompt."""
    global _SYMBOL_CACHE_STR
    if _SYMBOL_CACHE_STR is not None:
        return _SYMBOL_CACHE_STR
    parts = []
    # Autoloads + global classes from agents.md (compact, ~4KB, not the 216KB index)
    try:
        ag = REFERENCE.read_text(encoding="utf-8", errors="replace")
        # autoloads section
        m = re.search(r"## Registered autoloads.*?\n(.*?)(?=\n## |\Z)", ag, re.S)
        if m:
            parts.append("AUTOLOADS (global singletons, callable from anywhere):\n" + m.group(1).strip())
        # global classes paragraph
        m = re.search(r"## Global classes\n\n(.*?)(?=\n\n## |\Z)", ag, re.S)
        if m:
            parts.append("GLOBAL CLASSES (class_name, usable as types anywhere):\n" + m.group(1).strip())
    except Exception:
        pass
    parts.append("EVENTBUS SIGNALS (the ONLY valid EventBus signals):\n" + ", ".join(sorted(EVENTBUS_SIGNALS)))
    _SYMBOL_CACHE_STR = "\n\n".join(parts)
    return _SYMBOL_CACHE_STR


def _known_symbols() -> dict:
    """Build the set of known autoloads, global classes, and EventBus signals
    from the reference library. Used by the LOCAL fallback review (deterministic,
    zero model calls). Cached."""
    global _SYMBOL_CACHE_DICT
    if _SYMBOL_CACHE_DICT is not None:
        return _SYMBOL_CACHE_DICT
    autoloads, classes = set(), set()
    try:
        ag = REFERENCE.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"## Registered autoloads.*?\n(.*?)(?=\n## |\Z)", ag, re.S)
        if m:
            for name in re.findall(r"\*\*(\w+)\*\*", m.group(1)):
                autoloads.add(name)
        m = re.search(r"## Global classes\n\n(.*?)(?=\n\n## |\Z)", ag, re.S)
        if m:
            for name in re.findall(r"\b[A-Z]\w+\b", m.group(1)):
                classes.add(name)
    except Exception:
        pass
    _SYMBOL_CACHE_DICT = {
        "autoloads": autoloads,
        "classes": classes,
        "signals": set(EVENTBUS_SIGNALS),
    }
    return _SYMBOL_CACHE_DICT


def local_review(gd_path: Path, dept: str, task: dict) -> tuple[bool, list[str]]:
    """DETERMINISTIC LOCAL FALLBACK for cloud_review (2026-09-05). Catches the
    clear-cut semantic bugs the cloud model does — stubs, fabricated EventBus
    signals, and load('res://...') paths to missing files — with ZERO model
    calls. Runs when the free cloud model is unavailable (non-JSON, exception,
    rate-limit, credits exhausted). CONSERVATIVE BY DESIGN: it only flags cases
    with zero false positives. It does NOT try to detect 'invented class' (a
    producer legitimately creates new classes, and wired feature classes aren't
    in the reference library) — that nuanced check is exactly what the cloud
    model is for. Never blocks production on the cloud being up.
    Returns (pass, reasons)."""
    try:
        code = gd_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True, []
    sym = _known_symbols()
    reasons = []

    # 1. STUB CHECK: a real artifact must declare a class or extend a node and
    #    contain at least one func. A file that is only a const/comment/prose is
    #    a stub that does not implement the task.
    has_class = bool(re.search(r"^\s*class_name\s+\w+", code, re.M))
    has_extend = bool(re.search(r"^\s*extends\s+\w+", code, re.M))
    has_func = bool(re.search(r"^\s*func\s+\w+", code, re.M))
    if not (has_class or has_extend) or not has_func:
        reasons.append("stub: no class_name/extends and no func — does not implement the task")

    # 2. FABRICATED EVENTBUS SIGNAL: any EventBus.<signal>.emit/connect where the
    #    signal is not in the known set. (api_gate already catches the string-API
    #    form; this catches the dotted form.)
    for m in re.finditer(r"\b(?:EventBus|GameEventBus)\.(\w+)\.(?:emit|connect)\s*\(", code):
        sig = m.group(1)
        if sig not in sym["signals"]:
            reasons.append(f"fabricated EventBus signal '{sig}' (not in known set)")

    # 3. MISSING RESOURCE PATH: any load('res://...')/preload('res://...') path
    #    that does not exist in the project tree. (check_resources already does
    #    this for the build; this is the review-pass copy so the fallback is
    #    self-contained.)
    project = PROJECTS.get(dept, {}).get("project")
    if project:
        for m in re.finditer(r"(?:load|preload)\s*\(\s*['\"](res://[^'\"]+)['\"]\s*\)", code):
            res = m.group(1)
            rel = res[len("res://"):]
            if not (project / rel).exists():
                reasons.append(f"load path '{res}' does not exist in project")

    return (len(reasons) == 0), reasons


def cloud_review(gd_path: Path, dept: str, task: dict) -> tuple[bool, list[str]]:
    """Semantic review of a build artifact against the project's known symbols.
    Returns (pass, reasons). pass=True means the artifact is safe to wire.
    Uses the FREE deepseek-v4-pro:cloud model — zero credits. If the cloud model
    is unavailable (non-JSON, exception, rate-limit, credits exhausted), falls
    back to the DETERMINISTIC LOCAL review (local_review) so production never
    depends on the cloud being up."""
    if not CLOUD_REVIEW:
        return True, []
    try:
        code = gd_path.read_text(encoding="utf-8", errors="replace")
        if len(code) > 20000:
            code = code[:20000] + "\n# ...(truncated)"
        symbols = _compact_symbols()
        prompt = (
            "You are a senior Godot 4 / GDScript reviewer at Bluu Ink Studios. "
            "A producer model wrote the artifact below for a task. Your job is to catch "
            "SEMANTIC bugs that a compiler cannot: references to classes/signals/methods "
            "that do not exist in the project, calls to real methods with the wrong "
            "signature, load('res://...') paths to files that do not exist, and stubs "
            "that do not actually implement the task.\n\n"
            f"DEPARTMENT: {dept}\n"
            f"TASK TITLE: {task.get('title','')}\n"
            f"TASK DESCRIPTION: {task.get('description','')}\n\n"
            f"=== KNOWN SYMBOLS (the ONLY valid autoloads, classes, and EventBus signals) ===\n"
            f"{symbols}\n\n"
            "=== ARTIFACT TO REVIEW ===\n"
            f"```gdscript\n{code}\n```\n\n"
            "Respond with ONLY a JSON object, no prose, no markdown fences, no reasoning:\n"
            '{"verdict": "PASS" or "FAIL", "reasons": ["<specific issue 1>", "<specific issue 2>"]}\n'
            "Do NOT explain your reasoning. Output the JSON object only.\n"
            "PASS only if the artifact is genuinely correct and safe to wire into the game. "
            "FAIL if it references any symbol not in the known list, calls a method with "
            "the wrong signature, loads a resource path that does not exist, or is a stub "
            "that does not implement the task. Be specific in reasons — name the exact "
            "symbol/line so the producer can fix it."
        )
        body = json.dumps({"model": CLOUD_REVIEW_MODEL, "prompt": prompt,
                           "stream": False, "format": "json",
                           "options": {"num_predict": 2048, "temperature": 0.0}}).encode()
        req = urllib.request.Request(OLLAMA, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=CLOUD_REVIEW_TIMEOUT) as r:
            d = json.loads(r.read())
        out = (d.get("response", "") or d.get("thinking", "")).strip()
        # The free model sometimes narrates its reasoning before the JSON.
        # Extract the first balanced JSON object from the response.
        verdict = None
        start = out.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(out)):
                if out[i] == "{":
                    depth += 1
                elif out[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            verdict = json.loads(out[start:i+1])
                        except Exception:
                            verdict = None
                        break
        if verdict is None:
            # fall back to a regex scan for a JSON object
            m = re.search(r"\{.*\}", out, re.S)
            if m:
                try:
                    verdict = json.loads(m.group(0))
                except Exception:
                    verdict = None
        if verdict is None:
            print(f"  WARN {task.get('id')}: cloud review returned non-JSON — using LOCAL fallback", flush=True)
            return local_review(gd_path, dept, task)
        if str(verdict.get("verdict", "")).upper() == "PASS":
            return True, []
        reasons = verdict.get("reasons", []) or ["cloud review: FAIL (no reasons given)"]
        return False, [str(x) for x in reasons]
    except Exception as e:
        # Review is a safety net, not a hard gate — if the free model is down or
        # rate-limited, fall back to the DETERMINISTIC LOCAL review so production
        # never depends on the cloud being up (credits exhausted, limits full,
        # network down, model unavailable).
        print(f"  WARN {task.get('id')}: cloud review unavailable ({e}) — using LOCAL fallback", flush=True)
        return local_review(gd_path, dept, task)



TASKS_PER_TICK = 1
# Per-department project routing. Each department builds into ITS OWN project
# so Sanctuary tasks (which reference Creature, a Sanctuary-only class) can
# actually pass integration instead of failing in the Galage tree.
#   project  : the Godot project root for this department
#   smoke    : the integration smoke-test scene (or None if none)
#   validate : the static integration analyzer script (or None)
#   ref      : the reference-library file injected into the model prompt
PROJECTS = {
    "forge": {
        "project": GALAGE,
        "smoke": "res://scenes/forge800_main_smoke_test.tscn",
        "boss_gate": "res://scripts/boss_firing_test.gd",
        "validate": GALAGE / "scripts" / "validate_integration.py",
        "ref": GALAGE / "_reference" / "agents.md",
        "wire_hook": WIRE_HOOK,
    },
    "sanctuary": {
        "project": SANCTUARY,
        "smoke": None,  # Sanctuary uses a SceneTree --script test, not a scene
        "smoke_script": "res://scripts/sanctuary_demo_test.gd",
        "boss_gate": None,
        "validate": None,
        "ref": None,
        "wire_hook": WIRE_HOOK,
    },
    "harmony": {
        "project": GALAGE,  # shared systems integrate into the main game
        "smoke": "res://scenes/forge800_main_smoke_test.tscn",
        "boss_gate": "res://scripts/boss_firing_test.gd",
        "validate": GALAGE / "scripts" / "validate_integration.py",
        "ref": GALAGE / "_reference" / "agents.md",
        "wire_hook": WIRE_HOOK,
    },
}
REFERENCE = GALAGE / "_reference" / "agents.md"
# Anti-thrash: park a task to 'blocked' after this many consecutive failed
# builds (<70 or import error) so the dispatcher rotates to other work instead
# of hammering one hard task forever (which starves the rest of the board).
MAX_ATTEMPTS = 3

SYSTEM = ("You are a lead engineer at Bluu Ink Studios writing real, validated "
          "Godot 4 GDScript for an EXISTING project. Use Godot 4 idioms, proper "
          "signals/groups, modular classes, comments, edge-case handling. Never "
          "stub. Output ONLY the GDScript code, no markdown fences, no explanation.\n\n"
          "HARD RULE — GDScript ONLY: Your ENTIRE response must be a single valid "
          "Godot 4 GDScript file. It MUST begin with one of: `extends <NodeType>`, "
          "`class_name <Name>`, or `func <name>(`. Do NOT write prose, explanations, "
          "chain-of-thought, markdown, or any natural-language text. No ``` fences. "
          "No 'Here is the code'. No commentary before or after. If you cannot write "
          "the code, still output ONLY a GDScript stub — never narration. A response "
          "containing any non-GDScript prose will be REJECTED and discarded.\n\n"
          "MANDATORY: Before writing any new class/function/signal, check the "
          "reference library below. REUSE the existing autoloads and class_names "
          "instead of inventing new names. Only create a new symbol when the "
          "reference proves it does not already exist.\n\n"
          "=== TUNABILITY MANDATE (design for one-setting changes) ===\n"
          "Game-feel and difficulty values MUST be easy to change in ONE place. "
          "Rules:\n"
          "1. No magic numbers scattered through logic. Every tunable (damage, "
          "   fire rate, bullet cap, HP, drop chance, spawn interval, bullet cap, "
          "   shield gap, vuln window) is an @export var, a named const, or read "
          "   from the wave profile Dictionary. A reviewer must be able to change "
          "   a difficulty value without hunting through layers of code.\n"
          "2. Prefer constants grouped at the top of the file with a one-line "
          "   comment on what each tunes, e.g. '_MAX_BOSS_BULLETS'. Keep the same "
          "   naming and grouping the existing project already uses.\n"
          "3. A hard limit on a resource (e.g. max concurrent bullets, max on-"
          "   screen projectiles) is a single named constant, never an inline "
          "   number.\n"
          "4. If a value comes from the wave/difficulty profile, read it via "
          "   profile.get('key', default) so difficulty curving can change it "
          "   without editing the class.\n"
          "5. Reuse the existing autoloads (GameState, EventBus, InputRemap, "
          "   EnergySystem) and class_names in the reference for cross-cutting "
          "   state. Do NOT duplicate a global already registered.\n\n"
          "=== GAME FEEL & CONTINUOUS BUILD MANDATE (the new direction) ===\n"
          "You are building ONE continuous game, not appending random code. Every "
          "task is a LAYER on the existing foundation. Before writing anything:\n"
          "1. READ how the game currently plays. The feel pillars are: aimed fire "
          "   (enemies shoot at the player, not random), dive-repeat (Galaga "
          "   enemies peel off and dive repeatedly), shield/vulnerability windows "
          "   (bosses open up, then shield), and powerup impact (each pickup must "
          "   visibly change the player). Preserve these.\n"
          "2. The game must be challenging, fun, engaging, and RANDOM enough that "
          "   the player cannot use the same formula to win every wave. If a "
          "   change makes it a solved pattern or a bullet sponge, it is wrong.\n"
          "3. INTEGRATION OVER VOLUME. Prefer fewer, well-integrated changes over "
          "   many random ones. If a change risks breaking the existing build, "
          "   SLOW DOWN: write it, run --import + the smoke test, and verify it "
          "   integrates before moving on. A change that does not integrate is "
          "   worse than no change.\n"
          "4. Keep the player's agency. Player options (powerups, lives, repair "
          "   kits, movement) must stay meaningful. Do not add systems that "
          "   remove player control.\n"
          "5. Survivability across a full run matters: wave 1 to the final boss "
          "   must be winnable in one pass. If difficulty rises, the player needs "
          "   a way to recover (repair/heal drops, lives).\n"
          "6. When you finish a task, note in a comment what feel it adds and "
          "   which existing system it builds on, so the next task continues the "
          "   same thread instead of starting fresh.\n\n"
          "=== REFERENCE LIBRARY (single source of truth) ===\n" +
          (REFERENCE.read_text(encoding="utf-8") if REFERENCE.exists()
           else "(reference missing — regenerate via scripts/build_reference_library.py)"))

def load_json(p):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return {}

def save_json(p, data):
    # ATOMIC WRITE (2026-09-06): temp + os.replace so a concurrent reader
    # (hourly_verify, cloud_wire_worker) never sees a half-written board.json.
    # Sweep #4 (deepseek): use the SHARED pid-unique writer — a deterministic
    # ".json.tmp" collides when two writers touch the same file concurrently.
    atomic_write_json(p, data, indent=1)

def gpu_safe(min_free_gb=2.0, max_util=20.0):
    """GPU pre-flight guard. Three outcomes:

    1. WARM WORKER (default production state): MODEL is ALREADY resident in VRAM
       (keep_alive='30m' holds it) and the GPU is idle. Generation happens IN
       PLACE and needs almost no new VRAM, so this is SAFE and must NOT be
       gated out. This is the fix for the 2026-09-02/09-03 stall: a resident
       24.6GB worker leaves only ~5.6GB free, which the OLD `>=6GB free` rule
       rejected as unsafe, so every 5-min tick skipped all 3 departments
       (production frozen despite a warm, idle, ready worker).

    2. COLD LOAD (model not resident): the tick would load ~17-24GB into the
       card. Demand real headroom (min_free_gb) so we never load a heavy model
       into a nearly-full card — the confirmed cause of the nvlddmkm 153 TDR
       stalls when combined with 100% sustained GPU.

    3. BUSY GPU: skip regardless (a playtest/video job is using the card).

    Uses CSV (driver 610.88 rejects --format=json)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"], text=True, timeout=10)
        parts = [p.strip() for p in out.strip().split(",")]
        free_gb = float(parts[0]) / 1024
        util = float(parts[1])
    except Exception as e:
        print(f"  ⚠ gpu_safe: nvidia-smi failed ({e}) — proceeding cautiously", flush=True)
        return True
    # 1. WARM WORKER bypass: if MODEL is already resident and the GPU is idle,
    #    generation is safe in place. Queries ollama api/ps (cheap).
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            resident = json.loads(r.read()).get("models", [])
        if any(m.get("name", "").split(":")[0] == MODEL.split(":")[0] for m in resident):
            # WARM: this model IS the intended GPU consumer (keep_alive holds it
            # resident precisely so it can generate whenever work exists). The
            # util spike during its own generation must NOT block it — that was
            # the second half of the 2026-09-02/09-03 stall: forge's task spiked
            # the GPU to 90%+, and the old `util <= 20` check then skipped
            # sanctuary AND harmony for the whole tick, so at most ONE department
            # produced per 5-min tick. A video/playtest job holds the OTHER model
            # and will evict this one from VRAM anyway — the cold-load path below
            # handles headroom then. Resident = safe to generate in place.
            print(f"  ✅ gpu_safe: {MODEL} resident, {util:.0f}% util — generating in place (warm)", flush=True)
            return True
    except Exception:
        pass  # fall through to headroom check
    # 2. COLD LOAD guard: model not resident, will load ~24GB — need headroom.
    if free_gb < min_free_gb:
        print(f"  ⛔ gpu_safe: cold load, only {free_gb:.1f}GB VRAM free (need >= {min_free_gb:.1f}GB) — skipping tick", flush=True)
        return False
    if util > max_util:
        print(f"  ⛔ gpu_safe: GPU busy at {util:.0f}% (max {max_util:.0f}%) — skipping tick", flush=True)
        return False
    print(f"  ✅ gpu_safe: {free_gb:.1f}GB free, {util:.0f}% util (cold load ok)", flush=True)
    return True

def call_model(prompt, model=None, keep_alive="30m"):
    model = model or MODEL
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                   "keep_alive": keep_alive,
                   "options": {"num_predict": 16384, "temperature": 0.1, "num_ctx": 32768}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    code = d.get("response", "") or d.get("thinking", "")
    if "extends" not in code and "class_name" not in code and "func " not in code:
        code = d.get("thinking", "") or d.get("response", "")
    code = re.sub(r"^\s*(thinking|response)\s*", "", code)
    code = re.sub(r"^```(gdscript|gd|python)?\s*", "", code)
    code = re.sub(r"\s*```$", "", code)
    code = code.strip()
    # PROSE-PREFIX STRIPPER (2026-09-04): qwen3-style models (bluu-nano-1000)
    # route output to the `thinking` field and often narrate for hundreds of
    # chars before the actual code. The GDScript-validity gate only checks a
    # construct EXISTS, so a prose prefix would be written verbatim into the
    # .gd file and fail Godot import. Cut everything before the first real
    # GDScript construct (extends/class_name/func) so only code is written.
    m = re.search(r"^\s*(?:extends\s+\w+|class_name\s+\w+|func\s+\w+)", code, re.MULTILINE)
    if m:
        code = code[m.start():].strip()
    # TRAILING-PROSE STRIPPER (2026-09-04): the prefix stripper handles prose
    # BEFORE the code, but qwen3-style models also append narration AFTER it
    # (markdown fences + "Wait, I need to check the reference more carefully..."
    # leaked past the end). The GDScript-validity gate only checks the START, so
    # trailing prose was written verbatim into the .gd file and parse-failed at
    # the tail (e.g. forge-1671.gd line 140: ``` then prose) — the artifact got
    # parked as "wire step failed" even though the wire itself was correct.
    # Cut everything from the first markdown fence after the code, then any
    # trailing natural-language block separated by a blank line.
    fence = code.find("```")
    if fence != -1:
        code = code[:fence].rstrip()
    _lines = code.splitlines()
    _last_code = 0
    # CODE-LINE MATCHER: a line is "code" if it starts with a GDScript keyword,
    # is an assignment (identifier = value, but not ==/!=/<=/>=/=>), is
    # indented (whitespace + identifier — a body line), or ends with a block
    # opener/terminator. The old matcher only caught keyword-start and
    # :/)/;/}/] endings, so a trailing plain assignment like `speed = 200`
    # or `position.x += 1` was treated as prose and truncated — turning valid
    # code into a bare `func _ready():` (parse error) or silently deleting
    # trailing functionality. (kimi review 2026-09-06)
    _assign = re.compile(r"^\s*[A-Za-z_][\w.]*\s*(?:[+\-*/%&|^]?=|->)\s*\S")
    for _i, _ln in enumerate(_lines):
        _s = _ln.strip()
        if not _s:
            continue
        if (re.match(r"^(func|var|const|signal|return|pass|if|for|while|match|class_name|extends|enum|static|break|continue|@)\b", _s)
                or _s.endswith((":", ")", ";", "}", "]", "{"))
                or _assign.match(_ln)
                or re.match(r"^\s+[A-Za-z_]", _ln)):
            _last_code = _i
    if len(_lines) - _last_code - 1 > 2:
        code = "\n".join(_lines[: _last_code + 1]).rstrip()
    code = code.strip()
    # DEGENERATE-OUTPUT GUARD: if the model returns garbage (e.g. a run of
    # '@' symbols) or didn't finish (done=False), the model is in a corrupted
    # generation loop. Unload it to clear the state and retry ONCE. Without
    # this, every task gets parked to blocked and production silently freezes
    # (done frozen, pending just refills) — the 2026-08-30 stall.
    if (not d.get("done", True)) or len(code) < 40 or code.count(code[:1]) > len(code) * 0.5:
        print(f"  WARN call_model: degenerate output (len={len(code)}, done={d.get('done')}) — unloading model + retry", flush=True)
        try:
            unload = json.dumps({"model": model, "keep_alive": 0}).encode()
            urllib.request.urlopen(urllib.request.Request(OLLAMA, data=unload, method="POST"), timeout=30)
        except Exception:
            pass
        req2 = urllib.request.Request(OLLAMA, data=body, method="POST")
        with urllib.request.urlopen(req2, timeout=600) as r2:
            d2 = json.loads(r2.read())
        code2 = d2.get("response", "") or d2.get("thinking", "")
        if "extends" not in code2 and "class_name" not in code2 and "func " not in code2:
            code2 = d2.get("thinking", "") or d2.get("response", "")
        code2 = re.sub(r"^\s*(thinking|response)\s*", "", code2)
        code2 = re.sub(r"^```(gdscript|gd|python)?\s*", "", code2)
        code2 = re.sub(r"\s*```$", "", code2)
        code = code2.strip()
    return code

def clear_godot_cache(project: Path):
    """STALE-CACHE GOTCHA (2026-09-05): Godot caches compiled scripts in .godot/.
    A stale cache won't register a newly-added global class and --import reports
    a false 'Could not parse global class' Parse Error, or masks a real parse
    error (stale bytecode 'passes'). Clear the cache before EVERY Godot check so
    --import/--check-only recompile from source and report the true state."""
    import shutil
    cache = project / ".godot"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)

def validate_godot(project: Path):
    clear_godot_cache(project)
    r = subprocess.run([GODOT, "--headless", "--path", str(project), "--import"],
                       capture_output=True, text=True, timeout=180)
    err = r.stdout + r.stderr
    return not ("SCRIPT ERROR" in err or "Parse Error" in err or "Compile Error" in err or "Cannot open" in err)

def _project_autoloads(project: Path) -> set:
    """Read the project.godot [autoload] section to get registered autoload names."""
    try:
        cfg = (project / "project.godot").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set()
    names, in_autoload = set(), False
    for line in cfg.splitlines():
        s = line.strip()
        if s == "[autoload]":
            in_autoload = True
            continue
        if in_autoload and s.startswith("["):
            in_autoload = False
        if in_autoload and "=" in s and not s.startswith(";"):
            names.add(s.split("=")[0].strip())
    return names

def _is_autoload_missing(line: str, autoloads: set) -> bool:
    """True if the error line is exactly 'Identifier not found: <autoload>' for a
    known autoload — the isolated-gate false positive. Genuine errors return False."""
    m = re.search(r"Identifier not found:\s*(\w+)", line)
    return bool(m and m.group(1) in autoloads)

def validate_script(project: Path, gd_path: Path):
    """Per-file compile gate. Godot --import does NOT compile unreferenced
    scripts (a broken .gd sitting in the tree passes --import with rc=0 and no
    errors; the parse error only surfaces once something references it, e.g.
    the wire step editing main.gd). This is why artifacts graded 73-85 were
    'done' yet failed to wire. --check-only --script <file> forces Godot to
    actually compile THAT file and report its real parse errors.
    Returns (ok, error_lines)."""
    clear_godot_cache(project)
    r = subprocess.run(
        [GODOT, "--headless", "--path", str(project),
         "--check-only", "--script", str(gd_path)],
        capture_output=True, text=True, timeout=120)
    err = r.stdout + r.stderr
    bad = [l.strip() for l in err.splitlines()
           if "SCRIPT ERROR" in l or "Parse Error" in l or "Compile Error" in l]
    if len(bad) == 0:
        return True, []
    # FALSE-POSITIVE GUARD (2026-09-05): the isolated --check-only --script gate
    # does NOT register autoloads (EventBus, GameState, GameSettings...). Any
    # artifact that CALLS an autoload method fails here with "Identifier not
    # found: <autoload>" even though it's perfectly valid — the full-project
    # --import (which registers autoloads) compiles it clean. This was silently
    # rejecting EVERY valid artifact for hours (0 real artifacts produced while
    # GPU burned every 5 min). Accept ONLY when every isolated error is an
    # "Identifier not found: <known-autoload>" — genuine parse errors (type
    # mismatches, bad signatures, etc.) still reject.
    autoloads = _project_autoloads(project)
    # Only the actual error lines matter (SCRIPT ERROR / Parse Error / Compile
    # Error); "at:" and "ERROR: Failed to load" lines are context, not errors.
    err_lines = [l for l in bad if "SCRIPT ERROR" in l or "Parse Error" in l or "Compile Error" in l]
    if autoloads and err_lines and all(_is_autoload_missing(l, autoloads) for l in err_lines):
        print(f"  NOTE {gd_path.name}: isolated check flagged {len(bad)} error(s) "
              f"all = missing autoload ({autoloads}) — false positive, accepting", flush=True)
        return True, []
    return False, bad

def check_resources(code: str, project: Path):
    """Pre-flight: scan generated code for load('res://...')/preload('res://...')
    paths and verify each exists in the project. The model (nemotron-3-nano)
    invents resource paths to non-existent files, which is the #1 integration
    failure. Reject early with the exact missing list so the retry can fix them.
    Returns (ok, missing_list)."""
    missing = []
    for m in re.finditer(r"(?:load|preload)\s*\(\s*['\"](res://[^'\"]+)['\"]\s*\)", code):
        res = m.group(1)
        rel = res[len("res://"):]
        if not (project / rel).exists():
            missing.append(f"{res} (file not found at {rel})")
    return (len(missing) == 0), missing

def grade(gd_path, req, hints, project: Path):
    # PASS 1 — STANDALONE QUALITY. Grade WITHOUT --require-spec so the code is
    # judged on its own merits (Godoter's real standalone quality ~86 avg), not
    # against a strict spec that withholds completeness credit. This is the
    # quality bar: only genuinely good standalone code clears it.
    # Returns (score, breakdown_dict) where breakdown_dict has per-dimension
    # scores + details so the dispatcher can do TARGETED retries (fix only the
    # weak dimensions) instead of regenerating the whole file from scratch.
    cmd = [VENV, GRADE, str(gd_path), "--project-root", str(project), "--json"]
    if req: cmd += ["--req"] + req
    if hints: cmd += ["--hints"] + hints
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    try:
        import json as _json
        data = _json.loads(r.stdout)
        score = int(data.get("score", 0))
        breakdown = data.get("breakdown", {})
        details = data.get("details", {})
        return score, {"breakdown": breakdown, "details": details}
    except Exception:
        m = re.search(r"SCORE:\s*(\d+)", r.stdout)
        return (int(m.group(1)) if m else 0), {}

def validate_integration(dept: str):
    # PASS 2 — INTEGRATION SAFETY NET. Confirm the new code doesn't break the
    # family: (a) Godot --import already ran in build(); (b) run the alpha smoke
    # test scene (exits 0 = all pass); (c) run the static integration analyzer
    # (0 errors = no broken autoload/class/signal refs). Any failure = reject.
    # Returns (ok, errors_list) where errors_list has the specific ERROR-severity
    # messages so the dispatcher can feed them back into the model's retry prompt
    # (the model must fix the exact missing_resource/signal/group errors, not
    # regenerate blindly).
    cfg = PROJECTS[dept]
    project = cfg["project"]
    # NOTE: do NOT clear_godot_cache() here. validate_godot()/grade() already ran
    # --import (building the global class registry) earlier in build(). Clearing
    # the cache again wipes that registry, so the smoke scene can't resolve
    # global classes (e.g. SaveGameManager) and fails with a false "Identifier
    # not declared" — the 2026-09-05 production blocker. Cache is already fresh.
    smoke_ok = True
    smoke_err = ""
    if cfg.get("smoke_script"):
        smoke = subprocess.run(
            [GODOT, "--headless", "--path", str(project),
             "--script", cfg["smoke_script"]],
            capture_output=True, text=True, timeout=180)
        smoke_ok = smoke.returncode == 0
        if not smoke_ok:
            smoke_err = f"smoke test exit {smoke.returncode}"
            print(f"    INTEGRATION FAIL: {smoke_err}", flush=True)
    elif cfg["smoke"]:
        smoke = subprocess.run(
            [GODOT, "--headless", "--path", str(project),
             "--scene", cfg["smoke"]],
            capture_output=True, text=True, timeout=180)
        smoke_ok = smoke.returncode == 0
        if not smoke_ok:
            smoke_err = f"smoke test exit {smoke.returncode}"
            print(f"    INTEGRATION FAIL: {smoke_err}", flush=True)
    # Boss-firing gate (forge/harmony only): SceneTree --script test that forces
    # the boss through its entry phase and asserts it ACTUALLY fires (counts live
    # enemy_bullets group) and survives long enough to be a threat. A boss that
    # silently never fires (the 2026-09-02 bug the old smoke assertion couldn't
    # catch) must block the build. Run after the smoke scene so a broken import
    # is caught first. Same form as sanctuary's smoke_script.
    gate_ok = True
    gate_err = ""
    gate = cfg.get("boss_gate")
    if gate:
        gate_run = subprocess.run(
            [GODOT, "--headless", "--path", str(project), "--script", gate],
            capture_output=True, text=True, timeout=180)
        gate_ok = gate_run.returncode == 0
        if not gate_ok:
            gate_err = f"boss-firing gate exit {gate_run.returncode}"
            print(f"    INTEGRATION FAIL: {gate_err}", flush=True)
    vi_ok = True
    vi_errors = []
    if cfg["validate"]:
        vi = subprocess.run(
            [VENV, str(cfg["validate"]),
             "--root", str(project), "--output", str(project / "validation_report.json")],
            capture_output=True, text=True, timeout=180)
        try:
            import json as _json
            rep = _json.loads(vi.stdout) if vi.stdout.strip().startswith("{") else None
            if rep is None:
                rep = _json.loads((project / "validation_report.json").read_text(encoding="utf-8"))
            # Collect ERROR-severity mismatches (missing_resource, signal_mismatch,
            # group_mismatch, node_path_mismatch) — these are what the model must fix.
            for m in rep.get("mismatches", []):
                if m.get("severity") == "ERROR":
                    vi_errors.append(f"{m.get('category')}: {m.get('message')}")
            vi_ok = rep.get("summary", {}).get("errors", 0) == 0
            if not vi_ok:
                print(f"    INTEGRATION FAIL: {rep['summary']['errors']} errors", flush=True)
        except Exception as e:
            # FAIL CLOSED (2026-09-06): a validator crash / unparseable report
            # must NOT silently pass integration. Previously vi_ok stayed True
            # here, so a broken validator shipped every artifact. Now it's a
            # hard integration failure (the wire step's validate_game already
            # fails closed; the dispatcher's must match).
            vi_ok = False
            vi_errors.append(f"validator_error: could not parse report ({e})")
            print(f"    INTEGRATION FAIL: validator error ({e})", flush=True)
    errors = vi_errors
    if not smoke_ok:
        errors = [smoke_err] + errors
    if not gate_ok:
        errors = [gate_err] + errors
    return smoke_ok and vi_ok and gate_ok, errors

def playtest_gate(project: Path, timeout: int = 600):
    """Run the Galage fun-playtest harness headless (CPU, no GPU) and return
    (ok, summary). This is the 'is the game still FUN + BEATABLE after this
    wire?' gate. Runs LOCAL (no cloud credits) via playtest_runner.py --gate.
    Only meaningful for forge (Galage is the playable game); sanctuary/harmony
    skip it (no playable loop yet)."""
    runner = Path("C:/Users/bluue/AppData/Local/hermes/scripts/playtest_runner.py")
    try:
        r = subprocess.run([VENV, str(runner), "--gate"],
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"GATE overall=([\d.]+) beat=([\d.]+) softlock=([\d.]+) (?:contract=\w+ )?verdict=(\w+)", out)
        if m:
            overall, beat, softlock, verdict = m.groups()
            ok = (verdict == "PASS")
            return ok, f"playtest overall={overall} beat={beat} softlock={softlock} ({verdict})"
        return False, f"playtest gate no verdict (exit {r.returncode}): {out[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "playtest gate timed out"
    except Exception as e:
        return False, f"playtest gate error: {e}"

def live_game_context(dept: str) -> str:
    """Build a compact, current-state summary the model MUST build against.

    The static reference library lists symbols but not the LIVE state of the
    game. This injects: (1) what is queued (so the model does not recreate it),
    (2) what is already done/merged (so it builds on it, not duplicates it),
    (3) what is archived (so it never calls functions that no longer exist),
    (4) the recent git history (what actually shipped into the playable game).
    """
    lines = ["=== LIVE GAME STATE (build against THIS, not assumptions) ==="]
    # 1. Queued work across ALL departments (the model must not recreate these).
    queued = []
    for d in ["forge", "sanctuary", "harmony"]:
        try:
            b = load_json(BOTS / d / "kanban" / "board.json")
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
    # 2. Done work (build on it, don't duplicate).
    try:
        b = load_json(BOTS / dept / "kanban" / "board.json")
        cols = b.get("columns", {})
        done = [t.get("title", "?") for t in cols.get("done", []) if isinstance(t, dict)]
        # Dedupe: the same feature is often marked done many times (retries).
        # Only unique titles matter — the model must not redo a feature that
        # already exists, regardless of how many times it was re-done.
        seen, uniq = set(), []
        for t in done:
            if t not in seen:
                seen.add(t); uniq.append(t)
        if uniq:
            lines.append(f"ALREADY-DONE in {dept} (build on these, do not redo):")
            lines.append("\n  - " + "\n  - ".join(uniq[-40:]))
    except Exception:
        pass
    # 3. Archived (functions/classes that no longer exist — never call them).
    try:
        b = load_json(BOTS / dept / "kanban" / "board.json")
        cols = b.get("columns", {})
        arch = [t.get("title", "?") for t in cols.get("archived_legacy", []) if isinstance(t, dict)]
        if arch:
            lines.append(f"ARCHIVED in {dept} (these were removed — do NOT call their functions):")
            lines.append("\n  - " + "\n  - ".join(arch[-30:]))
    except Exception:
        pass
    # 4. Recent git history (what actually shipped into the playable game).
    try:
        r = subprocess.run(["git", "-C", str(GALAGE), "log", "--oneline", "-15"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            lines.append("RECENT MERGED COMMITS (the live game is at this state):")
            lines.append("\n".join("  " + l for l in r.stdout.strip().splitlines()))
    except Exception:
        pass
    return "\n".join(lines)


def build(dept, task):
    tid = task.get("id"); title = task.get("title")
    print(f"=== {dept} {tid}: {title}", flush=True)
    cfg = PROJECTS[dept]
    project = cfg["project"]
    ref = cfg["ref"]
    system = SYSTEM
    if ref and ref.exists():
        system = system.replace(
            "=== REFERENCE LIBRARY (single source of truth) ===\n" +
            (REFERENCE.read_text(encoding="utf-8") if REFERENCE.exists()
             else "(reference missing — regenerate via scripts/build_reference_library.py)"),
            "=== REFERENCE LIBRARY (single source of truth) ===\n" +
            ref.read_text(encoding="utf-8"))
    # LIVE GAME-STATE CONTEXT (2026-09-03): the model previously saw only the
    # static reference library, so it could not see what was already queued,
    # already merged into the game, or archived. That is why it recreated queued
    # features and called functions that no longer exist. Inject the CURRENT
    # board state + git history so the model builds against reality.
    system += "\n\n" + live_game_context(dept)
    gd = project / task.get("output_rel", f"scripts/{tid.lower()}.gd")
    gd.parent.mkdir(parents=True, exist_ok=True)

    # TARGETED RETRY: if this task has a previous attempt's grade breakdown,
    # feed the weak dimensions back to the model so it fixes ONLY those parts
    # instead of regenerating the whole file from scratch (which is why some
    # tasks fail 40+ times — the model never learns what scored low).
    prev_breakdown = task.get("_last_breakdown", {})
    prev_code = task.get("_last_code", "")

    # POISON-GUARD (2026-09-03): if the stored previous code is itself a prose
    # dump (no GDScript construct) it must NOT be fed back as "previous code to
    # revise". Doing so created an infinite reject-loop: the model read 50-68KB
    # of its own chain-of-thought, extended the narration, emitted more prose,
    # got rejected again, and stored the bigger dump back — forever. Tasks like
    # FORGE-1613/1636, SANCT-1773, HARM-1435 failed 3-64+ times this way and
    # starved the queue (they auto-pick first by priority). A prose dump is
    # noise, not prior art: drop it so the next attempt writes clean code from
    # scratch with the plain prompt instead of hallucinating off the noise.
    poison = prev_code and len(prev_code) > 5000 and not re.search(
        r"^\s*(?:extends\s+\w+|class_name\s+\w+|func\s+\w+)", prev_code, re.MULTILINE)
    if poison:
        prev_code = ""
        prev_breakdown = {}

    if prev_breakdown and prev_code:
        # Build a targeted "fix these weak dimensions" prompt from the breakdown.
        # Each dimension has a different max weight; flag any scoring below ~50%
        # of its max as weak so we fix the genuinely weak parts.
        DIM_MAX = {
            "correctness": 35, "completeness": 15, "idiomatic_quality": 25,
            "architecture_integration": 10, "robustness_style": 10, "anti_slop": 5,
        }
        weak = []
        weak_dims = []
        for dim, score in prev_breakdown.get("breakdown", {}).items():
            if isinstance(score, (int, float)):
                mx = DIM_MAX.get(dim, 10)
                if score < mx * 0.5:
                    weak.append(f"- {dim}: {score}/{mx}")
                    weak_dims.append(dim)
        details = prev_breakdown.get("details", {})
        detail_lines = []
        for dim in weak_dims:  # bare dimension name — 'details' is keyed by it,
                               # not by the formatted weak string (kimi/ultra
                               # sweep #2 2026-09-06: bug made detail_lines
                               # permanently empty, dropping missing-sub-criterion hints).
            d = details.get(dim, {}) if isinstance(details, dict) else {}
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, (int, float)) and v == 0:
                        detail_lines.append(f"  {dim}.{k}: missing/0")
        fix_guide = "\n".join(weak + detail_lines) if weak else "general quality"
        # Also include any integration errors from the previous attempt (e.g.
        # missing_resource: load('res://...') to a non-existent file). These are
        # the concrete reasons the code failed to integrate — the model MUST fix
        # them, not regenerate blindly.
        integ_errors = prev_breakdown.get("integration_errors", [])
        integ_guide = ""
        if integ_errors:
            integ_guide = ("\n=== INTEGRATION ERRORS TO FIX (from the validator) ===\n"
                           + "\n".join(f"- {e}" for e in integ_errors)
                           + "\nFix these EXACTLY: use only resource paths that exist in the project "
                             "(check the reference library for real scene/texture paths). Do NOT invent "
                             "load('res://...') paths to files that don't exist.")
        prompt = (f"{system}\n\nTask: {title}\n\n{task.get('description','')}\n\n"
                  f"=== TARGETED REVISION (attempt {int(task.get('attempts',0))+1}) ===\n"
                  f"The previous version scored below the bar. The rubric flagged these "
                  f"WEAK dimensions — fix ONLY these, keep everything else that works:\n"
                  f"{fix_guide}\n{integ_guide}\n\n"
                  f"Here is the previous code to revise (do not rewrite what already works):\n"
                  f"```gdscript\n{prev_code}\n```\n\n"
                  f"Output the COMPLETE revised GDScript file.")
    else:
        prompt = f"{system}\n\nTask: {title}\n\n{task.get('description','')}\n\nWrite the complete Godot 4 GDScript file."

    try:
        code = call_model(prompt)
        model_used = MODEL
    except Exception as e:
        print(f"  WARN {tid}: model error {e}", flush=True)
        return None
    # FALLBACK (2026-09-04): if the primary model (bluu-nano-1000) emitted a
    # prose dump — no GDScript construct — retry the SAME prompt with the
    # proven Godoter-27B before giving up. This keeps production moving when
    # the primary model degrades into narration instead of code.
    if code and not re.search(r"^\s*(?:extends\s+\w+|class_name\s+\w+|func\s+\w+)", code, re.MULTILINE):
        print(f"  WARN {tid}: {MODEL} emitted prose (no GDScript construct) — retrying with {FALLBACK_MODEL}", flush=True)
        try:
            # keep_alive=0: the fallback model (v4, 22GB) must unload immediately
            # after generating so it never evicts the resident primary (Godoter)
            # from VRAM. Otherwise the next dept's tick sees the primary not
            # resident, cold-loads, and the gpu_safe gate blocks it (starvation).
            code = call_model(prompt, model=FALLBACK_MODEL, keep_alive="0")
            model_used = FALLBACK_MODEL
        except Exception as e:
            print(f"  WARN {tid}: fallback model error {e}", flush=True)
            return None
    if not code or len(code) < 150:
        print(f"  WARN {tid}: short/empty ({len(code)} chars)", flush=True)
        return None
    # PRE-FLIGHT RESOURCE CHECK: the model invents load('res://...') paths to
    # non-existent files (the #1 integration failure). Reject early with the
    # exact missing list so the retry prompt can fix them.
    res_ok, res_missing = check_resources(code, project)
    if not res_ok:
        print(f"  REJECT {tid}: {len(res_missing)} missing resource(s), removing", flush=True)
        task["_last_breakdown"] = task.get("_last_breakdown", {})
        task["_last_breakdown"]["integration_errors"] = (
            [f"missing_resource: load('{m}')" for m in res_missing])
        task["_last_code"] = code
        return None
    # GDScript-VALIDITY GATE (2026-09-02): the model sometimes emits a pure
    # reasoning-dump / prose file (60KB of "EnemyProjectileSpread" loops, zero
    # GDScript) that passes the size+extension+import checks and even scores
    # 75-77 because the rubric reads the prose as "completeness". Require at
    # least one real GDScript construct (extends/class_name/func) so a prose
    # dump can never be marked done. This is the anti-fabrication backstop.
    gdscript_markers = re.findall(r"^\s*(?:extends\s+\w+|class_name\s+\w+|func\s+\w+)", code, re.MULTILINE)
    if not gdscript_markers:
        print(f"  REJECT {tid}: no GDScript construct (extends/class_name/func) — prose dump, removing", flush=True)
        task["_last_breakdown"] = task.get("_last_breakdown", {})
        task["_last_breakdown"]["integration_errors"] = (
            ["no_gdscript: output contains no extends/class_name/func — pure prose, not code"])
        task["_last_code"] = code
        return None
    gd.write_text(code, encoding="utf-8")
    # PER-FILE COMPILE GATE (2026-09-05): Godot --import does NOT compile
    # unreferenced scripts, so a broken artifact passed the old validate_godot
    # silently and only failed at the wire step (when main.gd referenced it).
    # Force Godot to actually compile THIS file now so broken code is caught
    # at build time, not merge time.
    script_ok, script_errs = validate_script(project, gd)
    if not script_ok:
        print(f"  FAIL {tid}: script compile error, removing", flush=True)
        for e in script_errs[:6]:
            print(f"    {e[:160]}", flush=True)
        # COMPILE-FAILURE FALLBACK (2026-09-06): the prose fallback above only
        # fires when the primary model emits NO GDScript construct. But a model
        # can emit real GDScript that still fails Godot 4 import (e.g. bluu-nano-v4
        # wrote Godot 3 syntax: `export` keyword, `?` ternary, enum property
        # access). Those are genuine quality failures the prose gate misses, and
        # they parked every task to blocked. Retry the SAME prompt with the proven
        # FALLBACK_MODEL (Godoter-27B) before giving up, so production keeps
        # moving when the primary model regresses into non-idiomatic code.
        if model_used != FALLBACK_MODEL:
            print(f"  FAIL {tid}: compile error — retrying with {FALLBACK_MODEL}", flush=True)
            try:
                # keep_alive=0: same as prose fallback — v4 must unload immediately
                # so it never evicts the resident primary (Godoter) from VRAM.
                code = call_model(prompt, model=FALLBACK_MODEL, keep_alive="0")
            except Exception as e:
                print(f"  WARN {tid}: fallback model error {e}", flush=True)
                code = None
            if code and len(code) >= 150:
                gd.write_text(code, encoding="utf-8")
                script_ok, script_errs = validate_script(project, gd)
                if script_ok:
                    print(f"  OK {tid}: {FALLBACK_MODEL} passed compile", flush=True)
                else:
                    print(f"  FAIL {tid}: {FALLBACK_MODEL} also failed compile, removing", flush=True)
                    for e in script_errs[:6]:
                        print(f"    {e[:160]}", flush=True)
        if not script_ok:
            task["_last_breakdown"] = task.get("_last_breakdown", {})
            task["_last_breakdown"]["integration_errors"] = script_errs[:6]
            task["_last_code"] = code
            gd.unlink(missing_ok=True)
            return None
    if not validate_godot(project):
        print(f"  FAIL {tid}: import error, removing", flush=True)
        gd.unlink(missing_ok=True)
        return None
    try:
        score, breakdown = grade(gd, task.get("req"), task.get("hints"), project)
    except Exception as e:
        print(f"  WARN {tid}: grade error {e}", flush=True)
        return None
    print(f"  PASS1 GRADE {tid}: {score}/100 (standalone)", flush=True)
    # PASS 1 gate = 70 (standalone quality). Godoter's real standalone quality
    # is ~86 avg, so 70 is the right bar for "genuinely good code". This is the
    # quality gate — only strong standalone work proceeds to integration.
    if score < 70:
        print(f"  REJECT {tid}: <70 standalone, removing", flush=True)
        # Save the breakdown + code so the NEXT attempt does a targeted fix
        # instead of regenerating from scratch.
        task["_last_breakdown"] = breakdown
        task["_last_code"] = code
        gd.unlink(missing_ok=True)
        return None
    # PASS 2 — INTEGRATION SAFETY NET. Confirm the new code doesn't break the
    # family: smoke test + static integration analyzer. This is the "does it
    # integrate with everything else" check the user asked for.
    integ_ok, integ_errors = validate_integration(dept)
    if not integ_ok:
        print(f"  REJECT {tid}: integration failed, removing", flush=True)
        # Save the integration errors + code so the NEXT attempt does a targeted
        # fix of the exact missing_resource/signal/group errors (the model was
        # inventing load('res://...') paths to non-existent resources — this is
        # why Forge tasks kept failing integration). The code is stored in the
        # task dict (NOT written as a .diag.gd file in the tree — that pollutes
        # the validator scan and causes false failures for OTHER tasks).
        task["_last_breakdown"] = task.get("_last_breakdown", {})
        task["_last_breakdown"]["integration_errors"] = integ_errors
        task["_last_code"] = code
        gd.unlink(missing_ok=True)
        return None
    print(f"  ✅ {tid}: PASS1 {score}/100 + integration OK", flush=True)
    return gd, score

# Static seed idea pool per department. This is a FINITE seed: once every idea
# here is on the board, refill_dept calls generate_fresh_ideas() to get NEW
# ideas grounded in the current game state instead of recycling stale ones.
_IDEA_POOL = {
    "forge": [
        ("FORGE", "Enemy projectile spread patterns", "Implement config-driven spread/burst/homing enemy bullet patterns", "medium", ["ai","enemies","projectiles"]),
        ("FORGE", "Level progression difficulty curve", "Define wave difficulty scaling across levels", "medium", ["balance","levels"]),
        ("FORGE", "Player respawn with power loss", "Arcade-style respawn that strips power-ups on death", "medium", ["gameplay","progression"]),
        ("FORGE", "Dual-ship fusion mechanic", "Allow capture of enemy ships to fuse into twin fighter (Galaga staple)", "high", ["gameplay","mechanics"]),
        ("FORGE", "Score combo multiplier system", "Reward consecutive kills with escalating combo multiplier", "medium", ["score","progression"]),
        ("FORGE", "Pause menu with settings", "Add pause menu with volume and difficulty settings", "medium", ["ui","settings"]),
        ("FORGE", "Enemy projectile telegraphs", "Add flash/windup indicators before dangerous attacks", "medium", ["fairness","ai"]),
        ("FORGE", "Carrier Mothership boss", "Multi-phase boss: spawns interceptors, launches homing missiles, exposes core weak point", "high", ["boss","ai","gameplay"]),
        ("FORGE", "Enemy Weaver", "Sinusoidal horizontal motion enemy that fires 3-way burst at player Y", "medium", ["ai","enemies"]),
        ("FORGE", "Kamikaze drone enemy", "Spawns off-screen, accelerates toward player, explodes on death (AoE)", "low", ["ai","enemies"]),
        ("FORGE", "Replay system", "Record inputs + RNG seed; playback for ghost ships and debugging", "medium", ["tools","systems"]),
        ("FORGE", "Enemy bullet homing upgrade", "Add homing behavior to a subset of enemy projectiles for later waves", "medium", ["ai","projectiles"]),
        ("FORGE", "Enemy dive telegraph", "Add a visual flash/warning before an enemy commits to a dive", "medium", ["fairness","ai"]),
        ("FORGE", "Wave clear bonus", "Grant bonus score + a powerup drop when a wave is fully cleared", "low", ["score","progression"]),
        ("FORGE", "Boss defeat reward", "Drop a guaranteed powerup + bonus score when a boss is defeated", "low", ["boss","progression"]),
        ("FORGE", "Enemy speed scaling", "Gradually increase enemy movement speed across waves for difficulty", "medium", ["balance","enemies"]),
        ("FORGE", "Hit flash on enemies", "White flash on enemies when they take damage for feedback", "low", ["fx","enemies"]),
        ("FORGE", "Player death explosion", "Screen shake + particle burst when the player's ship is destroyed", "low", ["fx","gameplay"]),
        ("FORGE", "Wave intro banner", "Show a 'WAVE N' banner with a brief pause before each wave", "low", ["ui","progression"]),
        ("FORGE", "Enemy contact damage", "Enemies that collide with the player deal damage (dive crashes)", "medium", ["enemies","gameplay"]),
        ("FORGE", "Powerup impact feedback", "Each pickup visibly changes the player (sprite tint, size, fire pattern)", "medium", ["powerup","fx"]),
        ("FORGE", "Difficulty curve tuning", "Ensure wave 1 to final boss is winnable in one pass with recovery drops", "high", ["balance","progression"]),
    ],
    "sanctuary": [
        ("SANCT", "Creature idle animation states", "Add idle/bob animation states to creature sprites", "low", ["creatures","animation"]),
        ("SANCT", "Creature hunger system", "Track hunger over time, feed to restore, visual feedback", "medium", ["creatures","needs"]),
        ("SANCT", "Creature happiness meter", "Track happiness from play/feed/clean, affects evolution", "medium", ["creatures","needs"]),
        ("SANCT", "Evolution preview UI", "Show next evolution stage silhouette before evolving", "low", ["ui","evolution"]),
        ("SANCT", "Creature naming system", "Allow player to name creatures, persist to save", "low", ["creatures","save"]),
        ("SANCT", "Sanctuary decoration placement", "Place decorations in sanctuary, affect creature mood", "medium", ["sanctuary","ui"]),
        ("SANCT", "Creature capture mechanic", "Capture wild creatures via bait/trap minigame", "high", ["creatures","gameplay"]),
        ("SANCT", "Creature battle system", "Turn-based battle between creatures with type advantages", "high", ["creatures","combat"]),
        ("SANCT", "Sanctuary weather system", "Day/night + weather cycles affecting creature behavior", "medium", ["sanctuary","systems"]),
        ("SANCT", "Creature codex", "Catalog discovered creatures with stats, lore, evolution tree", "medium", ["ui","codex"]),
        ("SANCT", "Feeding minigame", "Timing-based feeding minigame for happiness boost", "medium", ["gameplay","creatures"]),
        ("SANCT", "Creature friendship levels", "Friendship tiers unlock abilities and evolution paths", "medium", ["creatures","progression"]),
        ("SANCT", "Sanctuary expansion", "Unlockable sanctuary zones as player progresses", "medium", ["sanctuary","progression"]),
        ("SANCT", "Creature stat display", "Show creature stats (hunger, happiness, level) in a UI panel", "low", ["ui","creatures"]),
        ("SANCT", "Creature evolution trigger", "Evolve a creature when it meets level + happiness thresholds", "high", ["creatures","evolution"]),
        ("SANCT", "Creature trait inheritance", "Offspring inherit traits from parents with mutation chance", "high", ["creatures","genetics"]),
        ("SANCT", "Sanctuary save/load", "Persist sanctuary state (creatures, decorations, progress) to disk", "high", ["save","sanctuary"]),
        ("SANCT", "Creature feeding UI", "Interactive feeding interface with food selection", "medium", ["ui","creatures"]),
        ("SANCT", "Creature mood indicator", "Visual mood indicator (happy/sad/angry) on creatures", "low", ["creatures","visual"]),
        ("SANCT", "Sanctuary day/night cycle", "Day/night cycle affecting creature activity and mood", "medium", ["sanctuary","systems"]),
        ("SANCT", "Creature capture minigame", "Bait/trap minigame to capture wild creatures", "high", ["creatures","gameplay"]),
        ("SANCT", "Creature battle type chart", "Type advantages/disadvantages in creature battles", "medium", ["creatures","combat"]),
        ("SANCT", "Sanctuary decoration effects", "Decorations that boost creature mood or growth", "medium", ["sanctuary","systems"]),
        ("SANCT", "Creature naming persistence", "Save player-assigned creature names across sessions", "low", ["creatures","save"]),
        ("SANCT", "Creature hunger decay", "Hunger decreases over time, feeding restores it", "medium", ["creatures","needs"]),
        ("SANCT", "Creature happiness decay", "Happiness decreases without play/feed/clean, affects evolution", "medium", ["creatures","needs"]),
        ("SANCT", "Sanctuary weather effects", "Weather (rain/sun) affecting creature behavior", "medium", ["sanctuary","systems"]),
        ("SANCT", "Creature capture rate", "Capture success rate based on bait quality and creature rarity", "medium", ["creatures","gameplay"]),
        ("SANCT", "Sanctuary visitor system", "NPC tamers visit and interact with your creatures", "medium", ["sanctuary","npc"]),
        ("SANCT", "Creature trading", "Trade creatures with other players or NPCs", "medium", ["creatures","economy"]),
        ("SANCT", "Creature breeding cooldown", "Breeding has a cooldown to prevent infinite offspring", "low", ["creatures","genetics"]),
        ("SANCT", "Sanctuary expansion cost", "Expansion requires currency earned from creature activities", "low", ["sanctuary","progression"]),
    ],
    "harmony": [
        ("HARM", "Shared notification system", "Cross-project in-game notification/toast system", "medium", ["ui","shared"]),
        ("HARM", "Shared timer service", "Centralized timer/cooldown manager for cross-project use", "low", ["systems","shared"]),
    ],
}


def generate_fresh_ideas(dept, existing_titles, count):
    """Ask the model to propose genuinely-NEW ideas for a department, grounded in
    the CURRENT game state + reference library, so the pipeline keeps producing
    fresh forward progress instead of recycling stale tasks into dead code.

    Returns a list of (prefix, title, desc, priority, tags) tuples, or [] on
    failure. The model is told exactly what already exists (existing_titles +
    live game state) so it does not re-propose implemented features.
    """
    try:
        # Use the ACTUAL idea prefix from the pool (FORGE/SANCT/HARM), NOT
        # dept.upper() — harmony would become "HARMONY-" but real IDs are
        # "HARM-", causing mixed ID schemes on the board (HARM-003 + HARMONY-001).
        pool = _IDEA_POOL.get(dept, [])
        prefix = pool[0][0] if pool else dept.upper()
        ctx = live_game_context(dept)
        ref = ""
        try:
            ref_path = GALAGE / "_reference" / "agents.md"
            if ref_path.exists():
                ref = ref_path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except Exception:
            pass
        existing = "\n".join(f"- {t}" for t in sorted(existing_titles)[-60:]) or "- (none)"
        prompt = (
            f"You are the design lead for the '{dept}' department of Bluu Ink Studios.\n"
            f"Propose {count} NEW, concrete, buildable game features for this department.\n\n"
            f"=== CURRENT GAME STATE ===\n{ctx}\n\n"
            f"=== REFERENCE LIBRARY (existing autoloads/classes — reuse these) ===\n{ref}\n\n"
            f"=== ALREADY-EXISTING FEATURES (do NOT re-propose these) ===\n{existing}\n\n"
            f"Rules:\n"
            f"- Each idea must be NEW — not already listed above, not already implemented.\n"
            f"- Each must be a concrete, single, buildable feature (not a vague direction).\n"
            f"- Each must be implementable as a single Godot 4 GDScript file (extends/class_name/func).\n"
            f"- Prefer features that branch from what exists (reuse the reference library).\n"
            f"- Do NOT propose re-implementing something that already exists.\n\n"
            f"Respond with EXACTLY {count} lines, one idea per line, format:\n"
            f"TITLE | one-line description | priority(high/medium/low) | tag1,tag2\n"
            f"No markdown, no numbering, no extra text."
        )
        out = call_model(prompt)
        if not out:
            return []
        ideas = []
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue
            title = parts[0][:80]
            # Sanitize: strip markdown/numbering the model may have added
            # despite the "no markdown, no numbering" instruction (e.g.
            # "1. **Wave Modifier System**" -> "Wave Modifier System").
            import re as _re
            title = _re.sub(r"^\s*\d+[\.\)]\s*", "", title)   # leading "1. " / "1) "
            title = _re.sub(r"\*\*", "", title)                # **bold**
            title = _re.sub(r"^[#\-\*]+\s*", "", title)       # leading # - *
            title = title.strip()[:80]
            if not title:
                continue
            # Reject meta-instruction echoes: the model sometimes parrots the
            # format prompt back as a "feature" (e.g. "We need to output exactly
            # 12 lines, each line format: TITLE"). These are NOT features — drop
            # them so they never become tasks that fail and pollute the board.
            # WHITELIST approach (2026-09-04): a real feature title is a short
            # noun phrase ("Wave Modifier System"), NOT a sentence. Reject any
            # title that reads as meta-instruction or is too long to be a name.
            if is_junk_title(title):
                continue
            desc = parts[1][:200] if len(parts) > 1 else ""
            prio = parts[2].lower() if len(parts) > 2 and parts[2].lower() in ("high","medium","low") else "medium"
            tags = [t.strip() for t in parts[3].split(",")][:4] if len(parts) > 3 else [dept]
            if title.lower() in existing_titles:
                continue  # model re-proposed something that exists — drop it
            ideas.append((prefix, title, desc, prio, tags))
        return ideas[:count]
    except Exception as e:
        print(f"[generate_fresh_ideas] error: {e}")
        return []


def refill_dept(dept, board, cols, count):
    """Create `count` fresh tasks for a department from its idea pool, deduping
    against all existing IDs (backlog/ready/done/blocked). Mirrors bot_base's
    _suggestions_from_ideas so IDs never collide.

    FRESH-IDEA GENERATION (2026-09-03): the static idea pool is a finite seed.
    Once every idea in it is already on the board (done/queued/blocked), the old
    version-bump logic would keep re-adding "X v002/v003" — re-implementing the
    same feature as dead code (the 1,000-orphan problem). Instead, when the pool
    is exhausted we call the model to generate genuinely-NEW ideas grounded in
    the CURRENT game state + reference library, so the pipeline keeps producing
    fresh forward progress instead of recycling stale tasks.
    """
    ideas = _IDEA_POOL[dept]
    all_used = set()
    all_titles = set()
    for c in cols.values():
        for t in (c if isinstance(c, list) else []):
            if isinstance(t, dict):
                all_used.add(t.get("id", ""))
                all_titles.add(t.get("title", "").strip().lower())
    # How many pool ideas are still genuinely unused (not on the board at all)?
    unused = [i for i in ideas if i[1].strip().lower() not in all_titles]
    if len(unused) < count:
        # Pool is running dry — generate fresh ideas from the model, grounded in
        # the current game state, so we don't recycle stale ones into dead code.
        fresh = generate_fresh_ideas(dept, all_titles, count - len(unused))
        if fresh:
            ideas = unused + fresh
        else:
            ideas = unused  # model failed; fall back to whatever's left
    # Use the ACTUAL idea prefix (e.g. "SANCT", "HARM") for the max-ID scan.
    # dept.upper() is wrong: sanctuary -> "SANCTUARY-" but real IDs are "SANCT-",
    # harmony -> "HARMONY-" but real IDs are "HARM-". A wrong prefix makes hi stay
    # low and every generated ID collide with an existing one -> adds 0.
    prefix = ideas[0][0] if ideas else dept.upper()
    hi = 0
    for tid in all_used:
        if tid.startswith(prefix + "-"):
            try:
                num = int(tid.split("-")[-1])
                hi = max(hi, num)
            except ValueError:
                pass
    added = 0
    for prefix, title, desc, prio, tags in ideas:
        if added >= count:
            break
        # SKIP-IF-EXISTS (2026-09-03): if this idea's title already exists
        # ANYWHERE on the board (done/queued/blocked), it is already implemented
        # or already pending — re-adding it as "X v001" is the dead-code path
        # (constantly re-creating the same small tasks). Skip it and let the
        # pool rotate to genuinely-unused ideas; when the pool runs dry,
        # generate_fresh_ideas() produces NEW ones instead.
        base = title.strip().lower()
        if base in all_titles:
            continue
        hi += 1
        tid = f"{prefix}-{hi:03d}"
        if tid in all_used:
            continue
        all_used.add(tid)
        all_titles.add(title.strip().lower())
        cols.setdefault("backlog", []).append({
            "id": tid, "title": title, "description": desc,
            "priority": prio, "status": "backlog", "tags": tags,
            "output_rel": f"scripts/{tid.lower()}.gd",
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        added += 1
    if added:
        save_json(BOTS/dept/"kanban"/"board.json", board)
        print(f"  {dept}: auto-refilled {added} task(s) to backlog", flush=True)

def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🔧 Dispatcher build {now} — model {MODEL}", flush=True)
    # SINGLE-INSTANCE RUN LOCK (2026-09-06 sweep #5): the dispatcher cron fires
    # every 5min but a run can take 30+ min (3 wire steps x 1800s serial). If we
    # don't guard, a second process starts while the first is alive -> two
    # dispatchers hammer Ollama (600s call_model timeout tripped) + both
    # clear_godot_cache/--import the SAME project (GPU/import timeout). Exit
    # immediately if a live peer holds the run lock; skip this cron tick.
    if not acquire_run_lock():
        print("⏭ another dispatcher/build run is in progress — skipping this tick (run.lock held)", flush=True)
        return
    try:
        _main_body()
    finally:
        release_run_lock()


def _main_body():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🔧 Dispatcher build {now} — model {MODEL}", flush=True)
    # AUTO-REFILL: if any department's pending (backlog+ready) is below the
    # minimum, create fresh tasks from the bot_base idea pools so production
    # never runs dry. Without this, once the easy tasks are done the only
    # remaining work is the hard tasks parked in 'blocked' (which live bot
    # launchers resurrect -> infinite loop) and the pipeline silently dies.
    # MIN_PENDING is the LARGE-POOL target: keep >=20 pending per department so
    # the queue never runs dry and boards don't stall for 90+ min. When a pool
    # runs dry (all ideas already on the board), refill_dept calls
    # generate_fresh_ideas() to produce NEW ideas instead of recycling stale ones.
    MIN_PENDING = 20
    for dept in ["forge", "sanctuary", "harmony"]:
        bp = BOTS/dept/"kanban"/"board.json"
        board = load_json(bp); cols = board["columns"]
        # Count ONLY genuinely-pending (non-parked) tasks. Parked/resurrected
        # tasks (parked after / fabricated / wire step failed / cloud review
        # rejected) are dead weight — counting them keeps pending >= MIN_PENDING
        # and prevents refill from ever firing fresh ideas, silently killing
        # production (2026-09-04 Forge stall). The cloud-review park note
        # ("parked: cloud review rejected ...") was MISSING from the marker list,
        # so unblock_parked resurrected scope/API-review tasks into an infinite
        # retry loop (kimi/ultra sweep #2 2026-09-06) — added it.
        pending = [t for t in (cols.get("backlog", []) + cols.get("ready", []))
                   if not any(m in str(t.get("note", "")) for m in PARKED_MARKERS)
                   and not is_junk_title(t.get("title", ""))]
        if len(pending) < MIN_PENDING:
            refill_dept(dept, board, cols, MIN_PENDING - len(pending))
    # JUNK-ARCHIVE (2026-09-04): move any junk-titled tasks (meta-instruction
    # echoes created before the generate_fresh_ideas whitelist landed) out of
    # backlog/ready into blocked so they don't clutter the queue. Runs under the
    # cron's own single-writer context (never hand-edit board.json while live).
    for dept in ["forge", "sanctuary", "harmony"]:
        bp = BOTS/dept/"kanban"/"board.json"
        board = load_json(bp); cols = board["columns"]
        archived = 0
        for colname in ("backlog", "ready"):
            col = cols.get(colname)
            if not col:
                continue
            items = col if isinstance(col, list) else col.get("items", [])
            keep, junk = [], []
            for t in items:
                (junk if is_junk_title(t.get("title", "")) else keep).append(t)
            if junk:
                if isinstance(col, list):
                    cols[colname] = keep
                else:
                    col["items"] = keep
                for t in junk:
                    t["status"] = "blocked"
                    t["note"] = (t.get("note", "") + " | ARCHIVED junk-title (meta-instruction echo, 2026-09-04)").strip()
                    cols.setdefault("blocked", []).append(t)
                archived += len(junk)
        if archived:
            save_json(bp, board)
            print(f"  {dept}: archived {archived} junk-title task(s) -> blocked", flush=True)
    # ensure P0 triple-shot is the forge top task (only if it doesn't exist ANYWHERE)
    bp = BOTS/"forge"/"kanban"/"board.json"
    b = load_json(bp); cols = b["columns"]
    all_titles = [t.get("title","").lower() for c in cols.values()
                  for t in (c if isinstance(c,list) else [])]
    if not any("triple" in t for t in all_titles):
        # Max-ID must span ALL columns (done+backlog+ready+blocked). Scanning only
        # done+backlog undercounts when a higher-numbered task sits in ready/blocked
        # -> new FORGE-{hi+1} collides, and ID-based removal drops BOTH tasks.
        # Sweep #4 (deepseek Finding).
        ids=[int(m.group(1)) for col in cols.values()
             for t in (col if isinstance(col,list) else [])
             if (m:=re.match(r"FORGE-(\d+)", t.get("id","")))]
        hi = max(ids) if ids else 983
        cols.setdefault("backlog",[]).append({
            "id": f"FORGE-{hi+1:03d}", "title": "Triple-Shot Power-up",
            "description": "Spawnable power-up node that upgrades player to fire 3 projectiles (-15/0/+15 deg) for 8 seconds, unique sprite tint, pickup SFX hook, headless-testable activation/deactivation.",
            "priority":"P0","status":"backlog","tags":["powerups","gameplay"],
            "output_rel":"scripts/triple_shot_powerup.gd",
            "req":["_ready","_on_pickup","apply_effect","_on_timeout","queue_free"],
            "hints":["Player","fire","projectile","Sprite2D","Timer"],
            "created_at":datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        save_json(bp, b)
        print("forge: added P0 TripleShot", flush=True)

    produced = 0
    for dept in ["forge","sanctuary","harmony"]:
        # GPU SAFETY GATE: skip this department's whole tick if the GPU lacks
        # VRAM headroom or is already busy. Prevents the 24GB-model-at-100%-GPU
        # TDR stalls. Checked once per dept per tick (not per task).
        if not gpu_safe():
            print(f"  ⛔ {dept}: GPU not safe — skipping tick", flush=True)
            continue
        # Process up to TASKS_PER_TICK tasks per department per tick so the GPU
        # stays busy (Godoter is resident in VRAM via keep_alive) instead of
        # doing 1 task then idling ~13 min until the next 15-min tick.
        for _ in range(TASKS_PER_TICK):
            bp = BOTS/dept/"kanban"/"board.json"
            board = load_json(bp); cols = board["columns"]
            # RECONCILE CLOUD-WIRED TASKS (2026-09-04): the cloud wire worker
            # (cloud_wire_worker.py) wires parked Forge tasks in parallel and records
            # them in a sidecar. It never writes board.json (board-write race). Mark
            # any sidecar task done HERE, inside the dispatcher's own board-write
            # context, so the wire result lands on the board without a concurrent write.
            _sc = Path("C:/Users/bluue/AppData/Local/bluu-ink/state/wired_tasks.json")
            # Reconcile for ALL depts (2026-09-06): the sidecar was forge-only,
            # so sanctuary/harmony cloud-wired tasks were never marked done and
            # stayed parked forever. The worker writes sidecar entries for every
            # dept; the dispatcher must reconcile them all.
            if _sc.exists():
                try:
                    _wired = json.loads(_sc.read_text(encoding="utf-8"))
                    _changed = False
                    _processed_ids = set()
                    for _w in _wired:
                        _tid = _w.get("task_id")
                        if not _tid:
                            continue
                        for _c in ("backlog", "ready", "blocked"):
                            _hit = [t for t in cols.get(_c, []) if t.get("id") == _tid]
                            if _hit:
                                _t = _hit[0]
                                cols[_c] = [t for t in cols.get(_c, []) if t.get("id") != _tid]
                                _t["status"] = "done"
                                _t["note"] = f"cloud-wired {_w.get('feature_id','')} ({_w.get('wired_at','')})"
                                _t["artifact"] = _w.get("artifact", "")
                                _t["completed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                _t["dispatched_by"] = "cloud_wire_worker"
                                cols.setdefault("done", []).append(_t)
                                _changed = True
                                _processed_ids.add(_tid)
                                print(f"  🔌 {dept} {_tid}: cloud-wired -> done ({_w.get('feature_id','')})", flush=True)
                                break
                    if _changed:
                        save_json(bp, board)
                        # clear ONLY the entries actually marked done (others stay for next tick)
                        _remaining = [w for w in _wired if w.get("task_id") not in _processed_ids]
                        # ATOMIC write (tmp + os.replace): the cloud worker appends to this
                        # sidecar concurrently; truncate-in-place risks a half-written JSON.
                        # Sweep #4: pid-unique tmp so both writers never collide on one file.
                        atomic_write_json(_sc, _remaining, indent=1)
                except Exception as _e:
                    print(f"  ⚠ reconcile sidecar failed: {_e}", flush=True)
            pool = cols.get("backlog",[]) + cols.get("ready",[])
            # Exclude tasks already parked — live bot launchers may resurrect them
            # into ready/backlog (board-clobbering race, 2026-09-04 Forge stall: all
            # 14 'ready' tasks were parked-then-resurrected, so this loop re-picked
            # known-bad tasks forever and refill never fired because pending stayed
            # >= MIN_PENDING). Exclude by BOTH blocked-column ID and the note's
            # parked marker, regardless of which column the task sits in.
            blocked_ids = {t.get("id") for t in cols.get("blocked",[]) if isinstance(t,dict)}
            parked_marker = PARKED_MARKERS
            # JUNK-TITLE SKIP (2026-09-04): exclude tasks whose title is a
            # meta-instruction echo (the idea-generator parroting its format
            # prompt). These were created before the generate_fresh_ideas
            # whitelist filter landed; skip them at pick time so they don't
            # get built with real artifacts under junk titles.
            pool = [t for t in pool
                    if t.get("id") not in blocked_ids
                    and not any(m in str(t.get("note","")) for m in parked_marker)
                    and not is_junk_title(t.get("title",""))]
            if not pool:
                print(f"  {dept}: no pending task", flush=True)
                break
            prio = {"P0":0,"P1":1,"P2":2}
            pool.sort(key=lambda t:(prio.get(t.get("priority","P2"),3), t.get("created_at","")))
            task = pool[0]
            res = build(dept, task)
            if not res:
                # Retry cap: park a task to 'blocked' after MAX_ATTEMPTS consecutive
                # failures so the dispatcher rotates to other work (anti-thrash).
                task["attempts"] = int(task.get("attempts", 0)) + 1
                task["note"] = f"build attempt failed/<70 (attempt {task['attempts']}), kept for improvement"
                if task["attempts"] >= MAX_ATTEMPTS:
                    task["status"] = "blocked"
                    task["note"] = (f"parked after {task['attempts']} failed attempts "
                                    "(<70 or import error); needs spec/scope review")
                    for c in ("backlog", "ready"):
                        cols[c] = [t for t in cols.get(c, []) if t.get("id") != task["id"]]
                    cols.setdefault("blocked", []).append(task)
                    print(f"  ⛔ {dept} {task['id']}: parked to blocked after {task['attempts']} attempts", flush=True)
                save_json(bp, board)
                continue
            gd, score = res
            # REAL-API GATE (2026-09-03): reject artifacts that reference fabricated
            # EventBus signals/string-API BEFORE they can be marked done. Grading a
            # standalone class never caught this: forge-*, graded B/A, referenced
            # EventBus.boss_phase_changed etc. which don't exist on the real bus.
            bad_api = api_gate(Path(gd))
            if bad_api:
                task["attempts"] = int(task.get("attempts", 0)) + 1
                task["_fail"] = "; ".join(bad_api)
                task["note"] = f"fabricated EventBus API rejected: {bad_api[0]} (attempt {task['attempts']})"
                if task["attempts"] >= MAX_ATTEMPTS:
                    task["status"] = "blocked"
                    task["note"] = ("parked: fabricated EventBus API "
                                    + "; ".join(bad_api) + " — scope/API review needed")
                    for c in ("backlog", "ready"):
                        cols[c] = [t for t in cols.get(c, []) if t.get("id") != task["id"]]
                    cols.setdefault("blocked", []).append(task)
                    print(f"  ⛔ {dept} {task['id']}: parked (fake EventBus API {bad_api[0]})", flush=True)
                save_json(bp, board)
                continue
            # CLOUD REVIEW PASS (2026-09-05, option 2): semantic review of the
            # artifact against the project's known symbols BEFORE it is wired.
            # Catches invented class_names, wrong method signatures, and stubs
            # that pass grade but would fail the wire step's full-project --import
            # (the #1 source of scrapped work). Uses the FREE deepseek-v4-pro:cloud
            # model — zero credits. On FAIL, feed the reasons back as targeted
            # integration_errors so the next build fixes exactly those.
            rev_ok, rev_reasons = cloud_review(Path(gd), dept, task)
            if not rev_ok:
                task["attempts"] = int(task.get("attempts", 0)) + 1
                task["_fail"] = "; ".join(rev_reasons)
                task["_last_breakdown"] = task.get("_last_breakdown", {})
                task["_last_breakdown"]["integration_errors"] = rev_reasons
                task["_last_code"] = Path(gd).read_text(encoding="utf-8", errors="replace")
                task["note"] = f"cloud review needs-fix: {rev_reasons[0]} (attempt {task['attempts']})"
                if task["attempts"] >= MAX_ATTEMPTS:
                    task["status"] = "blocked"
                    task["note"] = ("parked: cloud review rejected "
                                    + "; ".join(rev_reasons) + " — scope/API review needed")
                    for c in ("backlog", "ready"):
                        cols[c] = [t for t in cols.get(c, []) if t.get("id") != task["id"]]
                    cols.setdefault("blocked", []).append(task)
                    print(f"  ⛔ {dept} {task['id']}: parked (cloud review: {rev_reasons[0]})", flush=True)
                else:
                    print(f"  🔄 {dept} {task['id']}: cloud review rejected ({len(rev_reasons)} reason(s)) — retry", flush=True)
                save_json(bp, board)
                continue
            # WIRE STEP (2026-09-03): commit real game growth, not orphan files.
            # Route the accepted artifact through wire_artifact.py so grading =
            # shipping. Each dept wires into its OWN project (3-repo isolation):
            # forge -> Galage, sanctuary -> Sanctuary, harmony -> Galage (shared).
            # wire_artifact.py is now parameterized by --dept (2026-09-04).
            if PROJECTS[dept].get("wire_hook"):
                # GIT-SAFETY (2026-09-04): the cloud wire worker (cloud_wire_worker.py)
                # also operates on the same repo. Serialize via the shared lock so
                # the two never race on git branches/merges. If the cloud worker holds
                # it, yield this task (leave it in ready) and let the next tick retry.
                # FIXED 2026-09-06: the dispatcher previously only CHECKED the lock
                # (exists+age) but never ACQUIRED it — a TOCTOU race let both the
                # dispatcher and cloud worker run wire_artifact on the same repo
                # concurrently. Now it acquires atomically (O_CREAT|O_EXCL) and
                # releases after the wire subprocess, matching cloud_wire_worker.
                _wl = Path("C:/Users/bluue/AppData/Local/bluu-ink/state/wire.lock")
                _wl.parent.mkdir(parents=True, exist_ok=True)
                _acquired = False
                try:
                    _fd = os.open(str(_wl), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(_fd, f"{os.getpid()} {time.time()}".encode())
                    os.close(_fd)
                    _acquired = True
                except FileExistsError:
                    # held by a live cloud worker -> yield (or steal if stale)
                    try:
                        _age = time.time() - _wl.stat().st_mtime
                        if _age < LOCK_STALE_SECS:  # held by a live wire/cloud worker -> yield
                            print(f"  ⏭ {dept} {task['id']}: wire lock held (cloud worker) — deferring", flush=True)
                            continue
                        else:  # stale lock -> steal
                            _wl.unlink()
                            try:
                                _fd = os.open(str(_wl), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                                os.write(_fd, f"{os.getpid()} {time.time()}".encode())
                                os.close(_fd)
                                _acquired = True
                            except FileExistsError:
                                print(f"  ⏭ {dept} {task['id']}: wire lock re-acquired by peer — deferring", flush=True)
                                continue
                    except OSError:
                        # Lock file vanished between FileExistsError and stat
                        # (peer released/removed). Do NOT run wire unlocked —
                        # defer instead. Sweep #4 (deepseek): `pass` here let a
                        # TOCTOU race proceed with _acquired=False (no lock).
                        print(f"  ⏭ {dept} {task['id']}: wire lock check raced — deferring", flush=True)
                        continue
                wire_hook = PROJECTS[dept]["wire_hook"]
                try:
                    try:
                        wr = subprocess.run([VENV_PY, wire_hook, "--dept", dept,
                                             "--task", task.get("id", ""),
                                             "--artifact", str(gd),
                                             "--title", task.get("title", ""),
                                             "--playtest-gate"],
                                            capture_output=True, text=True, timeout=1800,
                                            env={**os.environ, "WIRE_MODEL": WIRE_MODEL})
                    except subprocess.TimeoutExpired as te:
                        # Sweep #5 (kimi CRITICAL): the 1800s wire timeout on the
                        # CHILD process means the child never runs its own
                        # _drop_wire cleanup — it is SIGKILLed mid-git, leaving the
                        # repo on a feature branch. Before this fix the exception
                        # CRASHED the whole tick (no except) AND the stale branch
                        # leaked to master on the next wire. Clean master + park.
                        proj = PROJECTS[dept]["project"]
                        print(f"  ⛔ {dept} {task['id']}: wire TIMEOUT after 1800s — "
                              f"resetting {proj.name} master clean", flush=True)
                        try:
                            subprocess.run(["git", "-C", str(proj),
                                            "checkout", "-q", "master"], check=False)
                            subprocess.run(["git", "-C", str(proj),
                                            "reset", "--hard", "-q"],
                                           capture_output=True)
                        except OSError:
                            pass
                        task["attempts"] = int(task.get("attempts", 0)) + 1
                        task["note"] = f"wire failed (timeout 1800s, master reset): {str(te)[:180]}"
                        if task["attempts"] >= MAX_ATTEMPTS:
                            task["status"] = "blocked"
                            task["note"] = "parked: wire step failed " + task["note"]
                            for c in ("backlog", "ready"):
                                cols[c] = [t for t in cols.get(c, []) if t.get("id") != task["id"]]
                            cols.setdefault("blocked", []).append(task)
                            print(f"  ⛔ {dept} {task['id']}: parked (wire timeout), {task['note']}", flush=True)
                        save_json(bp, board)
                        continue
                finally:
                    if _acquired:
                        try:
                            _wl.unlink()
                        except OSError:
                            pass
                if wr.returncode != 0:
                    task["attempts"] = int(task.get("attempts", 0)) + 1
                    task["note"] = (f"wire failed (return {wr.returncode}): "
                                    f"{wr.stdout[-300:] or wr.stderr[-300:]}")
                    if task["attempts"] >= MAX_ATTEMPTS:
                        task["status"] = "blocked"
                        task["note"] = "parked: wire step failed " + task["note"]
                        for c in ("backlog", "ready"):
                            cols[c] = [t for t in cols.get(c, []) if t.get("id") != task["id"]]
                        cols.setdefault("blocked", []).append(task)
                        print(f"  ⛔ {dept} {task['id']}: parked (wire step), {task['note']}", flush=True)
                    save_json(bp, board)
                    continue
                print(f"  🔌 {dept} {task['id']}: wired + committed to {PROJECTS[dept]['project'].name}", flush=True)
            for c in ("backlog","ready"):
                cols[c] = [t for t in cols.get(c,[]) if t.get("id")!=task["id"]]
            task["status"]="done"; task["grade"]=score
            task["artifact"]=str(gd); task["completed_at"]=now
            task["dispatched_by"]="a28bec9eccb7"
            cols.setdefault("done",[]).append(task)
            save_json(bp, board)
            print(f"  ✅ {dept} {task['id']}: grade {score}/100 -> done", flush=True)
            # Durable grade ledger: append every graded task so grades + specs
            # survive kanban board clobbering by live bot launchers.
            try:
                from grade_ledger import record_grade
                record_grade(
                    dept, task.get("id",""), title=task.get("title",""),
                    req=task.get("req"), hints=task.get("hints"),
                    artifact=str(gd), score=score,
                    grade=("A" if score>=90 else "B" if score>=75 else
                           "C" if score>=60 else "D" if score>=45 else "F"),
                    note=task.get("note"),
                )
            except Exception as e:
                print(f"  WARN {task['id']}: grade_ledger failed {e}", flush=True)
            # Feed the produced B/A artifact into the training dataset (500+ goal).
            if score >= 70:
                try:
                    subprocess.run(
                        [VENV, "C:/Users/bluue/AppData/Local/hermes/training/grow_dataset.py",
                         "--gd", str(gd), "--task", task.get("title",""), "--score", str(score),
                         "--dept", dept],
                        capture_output=True, text=True, timeout=60)
                    print(f"  📚 {task['id']}: appended to training dataset", flush=True)
                except Exception as e:
                    print(f"  WARN {task['id']}: grow_dataset failed {e}", flush=True)
            produced += 1
    print(f"**Produced {produced} real artifact(s).**", flush=True)

if __name__ == "__main__":
    main()
