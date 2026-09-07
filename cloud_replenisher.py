#!/usr/bin/env python3
"""Bluu Ink — Cloud Backlog Replenisher.
Uses the cloud model (nemotron-3-ultra:cloud) to assess the roadmap, the pace of
task completion, and the work already produced, then generates NEW concrete pending
tasks to keep each board's backlog healthy (>= MIN_PENDING). Runs as a no_agent
cron script so there's no fragile agent turn.

Per tick:
  1. Read each board: done count, pending count, recent completed task titles.
  2. Read the roadmap (next_tranche.md) + forward_backlog.md for direction.
  3. Call cloud ultra to propose N new concrete tasks per department, grounded in
     what's already built and the roadmap.
  4. Add them to each board's backlog (dedup by id/title).
"""
import json, re, subprocess, sys, datetime, urllib.request
from pathlib import Path

from bluu_ink_constants import atomic_write_json

BOTS = Path("C:/Users/bluue/AppData/Local/hermes/bots")
ROADMAP = Path("C:/Users/bluue/AppData/Local/hermes/projects/harmony/next_tranche.md")
FORWARD = Path("C:/Users/bluue/AppData/Local/hermes/projects/harmony/forward_backlog.md")
VENV = "C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
HERMES = "C:/Users/bluue/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe"
DEPTS = ["forge", "sanctuary", "harmony"]
MIN_PENDING = 5
TARGET_PER_DEPT = 3  # new tasks to propose per department per tick


def load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(p, data):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def board_summary(dept):
    b = load_json(BOTS / dept / "kanban" / "board.json")
    cols = b.get("columns", {})
    done = cols.get("done", [])
    pending = len(cols.get("backlog", [])) + len(cols.get("ready", []))
    recent = [t.get("title", "")[:60] for t in done[-5:]]
    return {"dept": dept, "done_count": len(done), "pending": pending, "recent_done": recent}


def call_cloud(prompt):
    """Call cloud ultra via the local Ollama API (clean JSON, no CLI noise).
    The cloud model is served locally at 127.0.0.1:11434 as 'nemotron-3-ultra:cloud'."""
    body = json.dumps({
        "model": "nemotron-3-ultra:cloud", "prompt": prompt, "stream": False,
        "options": {"num_predict": 1024, "temperature": 0.6},
    }).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d.get("response", "") or d.get("thinking", "")


def parse_tasks(text, dept):
    """Parse proposed tasks from cloud output. Expect lines like:
    - TITLE: <title> | PRIORITY: P1 | DESC: <description>
    """
    tasks = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        m = re.match(r"-\s*(?:TASK\s*)?(?:ID:\s*\S+\s*)?(?:TITLE:\s*)?(.+?)(?:\s*\|\s*PRIORITY:\s*(P\d))?(?:\s*\|\s*DESC:\s*(.+))?$", line)
        if not m:
            continue
        title = m.group(1).strip()
        prio = m.group(2) or "P1"
        desc = m.group(3) or title
        if title and len(title) > 5:
            tasks.append({"title": title, "priority": prio, "description": desc})
    return tasks


def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = [f"🔧 **Bluu Ink Cloud Replenisher** — {now}"]

    # Gather context
    summaries = {d: board_summary(d) for d in DEPTS}
    roadmap_txt = ""
    for p in (ROADMAP, FORWARD):
        if p.exists():
            roadmap_txt += p.read_text(encoding="utf-8", errors="ignore")[:3000] + "\n"

    for dept in DEPTS:
        s = summaries[dept]
        if s["pending"] >= MIN_PENDING:
            report.append(f"  {dept}: pending={s['pending']} >= {MIN_PENDING}, healthy")
            continue

        prompt = (
            f"You are the Bluu Ink Studios backlog planner. The {dept} department board "
            f"has {s['done_count']} completed tasks and only {s['pending']} pending. "
            f"Recent completed: {s['recent_done']}. "
            f"Roadmap context: {roadmap_txt[:1500]}. "
            f"Propose {TARGET_PER_DEPT} NEW concrete, buildable Godot 4 GDScript tasks for "
            f"the {dept} department that build on what's already done and advance the roadmap. "
            f"Each on its own line starting with '-', format: "
            f"- TITLE: <title> | PRIORITY: P0/P1/P2 | DESC: <one-line description>"
        )
        try:
            out = call_cloud(prompt)
            tasks = parse_tasks(out, dept)
            if not tasks:
                report.append(f"  {dept}: cloud returned no parseable tasks")
                continue
            # add to board
            b = load_json(BOTS / dept / "kanban" / "board.json")
            cols = b.get("columns", {})
            existing = {t.get("id") for col in cols.values() for t in col}
            existing_titles = {t.get("title", "").lower() for col in cols.values() for t in col}
            # Sweep #5: use the REAL board-ID prefix (FORGE/SANCT/HARM), NOT
            # dept.upper() — "sanctuary".upper() is "SANCTUARY-" which never
            # matches the board's "SANCT-" IDs, so max(nums) was always empty and
            # every new task got -001 (dedup was by title only -> duplicate IDs).
            _prefix = {"forge": "FORGE", "sanctuary": "SANCT", "harmony": "HARM"}[dept]
            added = 0
            for t in tasks:
                if t["title"].lower() in existing_titles:
                    continue
                # assign next id
                nums = [int(m.group(1)) for col in cols.values() for t2 in col
                        for m in [re.match(rf"{_prefix}-(\d+)", t2.get("id", ""))] if m]
                nxt = (max(nums) + 1) if nums else 1
                tid = f"{_prefix}-{nxt:03d}"
                cols.setdefault("backlog", []).append({
                    "id": tid, "title": t["title"], "status": "backlog",
                    "priority": t["priority"], "description": t["description"],
                    "created_at": now,
                })
                existing_titles.add(t["title"].lower())
                added += 1
            bp = BOTS / dept / "kanban" / "board.json"
            atomic_write_json(bp, b)
            report.append(f"  {dept}: +{added} new tasks (pending now {s['pending']+added})")
        except Exception as e:
            report.append(f"  {dept}: ERROR {e}")

    print("\n".join(report))


if __name__ == "__main__":
    main()
