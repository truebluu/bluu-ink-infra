#!/usr/bin/env python3
"""Post-commit class-location audit (2026-09-07 safety).

Prevents recurrence of the creature-in-galage misrouting incident. After a
commit, verify that no committed .gd file declares a class_name belonging to
another project's domain. Run as a post-commit hook or CI gate.

Usage:
  python audit_class_locations.py <repo_root> [--since HEAD~1]

Exit 0 = clean, 1 = a foreign-domain class was committed.
"""
import re, subprocess, sys
from pathlib import Path

# Same domain tokens as dispatcher_singlebuild.routing_domain_assert (single
# source of truth lives there; keep in sync).
CREATURE_DOMAIN_TOKENS = (
    "Creature", "Breeding", "Habitat", "Genome", "Evolution", "Ecosystem",
    "Pet", "Egg", "Taming", "Genetics", "Flock", "Codex", "Bonding",
    "PhotoMode", "Migration", "Needs", "Glimmerwing", "Seasonal",
    "Decoration", "AutoFeeder", "DayNight", "DynamicMusic", "Sanctuary",
)
FORGE_DOMAIN_TOKENS = (
    "Enemy", "Wave", "Boss", "Ship", "Shmup", "Galaga", "Projectile",
    "Powerup", "Spawner", "Formation", "Dive", "Laser", "Turret",
)

def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    ).stdout

def audit(repo: Path, since: str = "HEAD~1") -> int:
    repo = repo.resolve()
    # Which repo is this? Determine its domain by the presence of a marker.
    is_sanctuary = (repo / "scripts" / "CreatureGenome.gd").exists() or \
                   (repo / "scripts" / "sanctuary_demo_test.gd").exists()
    changed = git(repo, "diff", "--name-only", since, "HEAD", "--", "*.gd")
    changed = [l.strip() for l in changed.splitlines() if l.strip()]
    if not changed:
        print(f"audit: no .gd files changed since {since} — clean")
        return 0
    bad = []
    for rel in changed:
        p = repo / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^\s*class_name\s+(\w+)", text, re.MULTILINE)
        if not m:
            continue
        cls = m.group(1)
        if is_sanctuary:
            for tok in FORGE_DOMAIN_TOKENS:
                if re.search(rf"\w*{tok}\w*", cls):
                    bad.append((rel, cls, "forge-domain in sanctuary"))
                    break
        else:
            for tok in CREATURE_DOMAIN_TOKENS:
                if re.search(rf"\w*{tok}\w*", cls):
                    bad.append((rel, cls, "creature-domain in galage"))
                    break
    if bad:
        print("audit: FOREIGN-DOMAIN CLASSES COMMITTED:")
        for rel, cls, why in bad:
            print(f"  {rel}: class_name {cls} ({why})")
        return 1
    print(f"audit: {len(changed)} .gd files, all class_names match repo domain — clean")
    return 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    since = "HEAD~1"
    if len(sys.argv) > 2:
        if sys.argv[2] == "--since" and len(sys.argv) > 3:
            since = sys.argv[3]
        else:
            since = sys.argv[2]
    sys.exit(audit(Path(sys.argv[1]), since))
