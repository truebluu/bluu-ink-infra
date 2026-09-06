#!/usr/bin/env python3
"""
Bluu Ink Studios - GDScript Grading Rubric (Production Gate)
Deterministic, verbosity-neutral rubric for Godot 4 GDScript quality.
Rewards correctness, completeness, and idiomatic Godot 4 patterns.
Penalizes stubs, slop, Godot-3 drift, and hallucinated symbols.
"""

import re
import json
import sys
import subprocess
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, asdict


@dataclass
class RubricResult:
    score: int
    grade: str
    breakdown: Dict[str, int]
    details: Dict[str, any]


class GDScriptRubric:
    # Grade thresholds
    GRADE_THRESHOLDS = {
        'A': 90,
        'B': 75,
        'C': 60,
        'D': 45,
        'F': 0
    }

    # Godot 3 -> 4 migration patterns (drift detection)
    GODOT3_DRIFT_PATTERNS = [
        (r'\byield\s*\(', 'yield() -> await'),
        (r'\bKinematicBody2D\b', 'KinematicBody2D -> CharacterBody2D'),
        (r'\bKinematicBody\b', 'KinematicBody -> CharacterBody3D'),
        (r'\bmove_and_slide\s*\(', 'move_and_slide() signature changed'),
        (r'\bInput\.is_action_pressed\b', 'Input.is_action_pressed() ok but check new InputMap'),
        (r'\bexport\s*\([^)]*\)\s*var', '@export var (new syntax)'),
        (r'\bonready\s+var', 'onready -> @onready (optional)'),
        (r'\bself\b', 'self -> optional in GDScript 4'),
        (r'\bmaster\b', 'master -> owner (deprecated)'),
    ]

    # Known Godot 4 classes in Galage project (for hallucination detection)
    KNOWN_GALAGE_CLASSES = {
        'CreatureGenome', 'CreatureCodex', 'CreatureSpawner', 'CreatureNeeds',
        'CreatureNeedsMood', 'CreatureSickness', 'CreatureDietPreferences',
        'CreatureTraitExpression', 'BreedingManager', 'BreedingGeneticsManager',
        'EvolutionController', 'EvolutionTree', 'HabitatManager', 'HabitatSystem',
        'GameEventBus', 'GameStateMachine', 'GameStatePersistence', 'GameState',
        'InputRemappingService', 'InputActionMap', 'InputActionRegistry',
        'InputRebindUI', 'InputRemapSettingsUI', 'InputRemapper',
        'SceneTransitionManager', 'SceneRegistry', 'SettingsPersistenceService',
        'SettingsRegistry', 'SettingsSync', 'SettingsApplier', 'SharedSettings',
        'SharedAudioManager', 'SharedAudioBus', 'SharedUIThemeResource',
        'ParticleEffectPool', 'ObjectPool', 'HitstopManager', 'Hitstop',
        'ScreenShake', 'CameraBounds', 'ParallaxManager', 'ParallaxStarfield',
        'WaveDirector', 'EnemySpawner', 'EnemyFormation', 'EnemyProjectile',
        'ProjectilePatterns', 'Boss', 'BossCorruptedAlpha', 'BossPhaseTransition',
        'Player', 'Enemy', 'Projectile', 'Powerup', 'PowerupManager', 'PowerupDrop',
        'HUD', 'LifeHUD', 'LifeSystem', 'ScoreMultiplier', 'ScoreMultiplierHUD',
        'ScoreSystem', 'Reputation', 'Analytics', 'DynamicMusic', 'Localization',
        'TimeDilation', 'PersonalityMutation', 'Genetics', 'ProcGenNames',
        'FlockingAI', 'EcosystemSimulation', 'TrainingManager', 'AutoFeeder',
        'PhotoMode', 'CreatureBonding', 'CreatureBondingMinigame', 'SanctuaryPanel',
        'SaveGameManager', 'CloudSave', 'PerformanceMonitor', 'PerformanceGuardrails',
        'PerfBudgetValidation', 'PerfProfiler', 'PerfGate', 'ErrorReporting',
        'GameOverScreen', 'VictoryScreen', 'PauseMenu', 'Main', 'Level01',
        'SkillTreeFramework', 'GameplayEventLogger', 'GameplayEventDefinitions',
        'CreatureSaveLoad', 'DayNightCycle', 'SeasonalCycle', 'TripleShot',
        'Weaver', 'BuildingSystem', 'InputReplay', 'DispatcherInputRemapRunner',
        'LauncherWatchdog', 'SteamworksAuthShim', 'ArenaConfig', 'SpawnZone',
        'TileMapSetup', 'ShaderLibrary', 'SharedAssetPipeline', 'VoidSectorPostLaunchDLC',
        'FeatureFlags', 'CIAssetCheck', 'CIAssetValidation', 'CIBuildGate', 'CIPipeline',
    }

    KNOWN_GALAGE_GROUPS = {
        'creatures', 'enemies', 'projectiles', 'powerups', 'particles',
        'ui', 'hud', 'audio', 'music', 'sfx', 'spawners', 'bosses',
        'players', 'habitats', 'sanctuary', 'wave_director', 'game_state',
        'input', 'settings', 'save_system', 'analytics', 'performance',
    }

    KNOWN_GALAGE_SIGNALS = {
        'creature_spawned', 'creature_died', 'creature_evolved', 'creature_bred',
        'wave_started', 'wave_completed', 'wave_failed', 'boss_spawned',
        'boss_defeated', 'phase_transition', 'player_died', 'player_hit',
        'score_changed', 'health_changed', 'needs_changed', 'mood_changed',
        'sickness_contracted', 'sickness_cured', 'bonding_started', 'bonding_completed',
        'settings_changed', 'save_completed', 'save_failed', 'load_completed',
        'scene_transition_started', 'scene_transition_completed',
        'input_remapped', 'audio_settings_changed', 'graphics_settings_changed',
        'game_paused', 'game_resumed', 'game_over', 'victory',
    }

    def __init__(self, required_funcs: List[str] = None, hints: List[str] = None, project_root: str = None, require_spec: bool = False):
        self.required_funcs = set(required_funcs or [])
        self.hints = set(hints or [])
        self.project_root = Path(project_root) if project_root else Path.cwd()
        # When require_spec is True, do NOT auto-grant completeness credit for
        # missing task specs (used for honest artifact-quality grading where the
        # per-file required functions are unknown).
        self.require_spec = require_spec
        self._load_project_symbols()

    def _load_project_symbols(self):
        """Load actual class/group/signal names from the Galage project for integration checks."""
        self.project_classes = set(self.KNOWN_GALAGE_CLASSES)
        self.project_groups = set(self.KNOWN_GALAGE_GROUPS)
        self.project_signals = set(self.KNOWN_GALAGE_SIGNALS)

        # Scan actual project files for additional symbols
        scripts_dir = self.project_root / 'scripts'
        if scripts_dir.exists():
            for gd_file in scripts_dir.rglob('*.gd'):
                try:
                    content = gd_file.read_text(encoding='utf-8')
                    # Extract class_name
                    class_match = re.search(r'class_name\s+(\w+)', content)
                    if class_match:
                        self.project_classes.add(class_match.group(1))
                    # Extract signal declarations
                    for sig in re.findall(r'signal\s+(\w+)', content):
                        self.project_signals.add(sig)
                    # Extract group usage
                    for grp in re.findall(r'get_nodes_in_group\(["\'](\w+)["\']\)', content):
                        self.project_groups.add(grp)
                    for grp in re.findall(r'add_to_group\(["\'](\w+)["\']\)', content):
                        self.project_groups.add(grp)
                except Exception:
                    pass

    def grade(self, file_path: str) -> RubricResult:
        """Main entry point: grade a GDScript file."""
        path = Path(file_path)
        if not path.exists():
            return RubricResult(0, 'F', {}, {'error': f'File not found: {file_path}'})

        content = path.read_text(encoding='utf-8')
        lines = content.splitlines()

        # Run all dimension checks
        correctness_score, correctness_details = self._check_correctness(content, lines, path)
        completeness_score, completeness_details = self._check_completeness(content, lines)
        idiomatic_score, idiomatic_details = self._check_idiomatic_quality(content, lines)
        architecture_score, architecture_details = self._check_architecture_integration(content, lines)
        robustness_score, robustness_details = self._check_robustness_style(content, lines)
        anti_slop_score, anti_slop_details = self._check_anti_slop(content, lines)

        # HARD PARSE GATE (2026-09-05, grading restructure): a file that does not
        # compile is 0/F regardless of how complete/idiomatic it looks. The old
        # rubric gave only 15/35 for parse, so a syntax-broken artifact could
        # still score 70+ on prose/completeness and be marked done — then fail
        # at the wire step. This is the #1 producer-quality failure mode. Any
        # parse error = 0/100, no exceptions. (Autoload false-positives are
        # already handled inside _godot_parse_check and return parse_ok=True.)
        if not correctness_details.get('parse_ok', False):
            return RubricResult(0, 'F', {
                'correctness': 0, 'completeness': 0, 'idiomatic_quality': 0,
                'architecture_integration': 0, 'robustness_style': 0, 'anti_slop': 0,
            }, {
                'correctness': correctness_details,
                'completeness': completeness_details,
                'idiomatic_quality': idiomatic_details,
                'architecture_integration': architecture_details,
                'robustness_style': robustness_details,
                'anti_slop': anti_slop_details,
                'file': str(path),
                'lines': len(lines),
                'non_empty_lines': len([l for l in lines if l.strip()]),
                'hard_parse_gate': 'FAILED — file does not compile, graded 0/F',
            })

        # Weighted total (sum = 100)
        breakdown = {
            'correctness': correctness_score,      # 35
            'completeness': completeness_score,    # 15
            'idiomatic_quality': idiomatic_score,  # 25
            'architecture_integration': architecture_score,  # 10
            'robustness_style': robustness_score,  # 10
            'anti_slop': anti_slop_score,          # 5
        }

        total = sum(breakdown.values())
        grade = self._score_to_grade(total)

        details = {
            'correctness': correctness_details,
            'completeness': completeness_details,
            'idiomatic_quality': idiomatic_details,
            'architecture_integration': architecture_details,
            'robustness_style': robustness_details,
            'anti_slop': anti_slop_details,
            'file': str(path),
            'lines': len(lines),
            'non_empty_lines': len([l for l in lines if l.strip()]),
        }

        return RubricResult(total, grade, breakdown, details)

    def _check_correctness(self, content: str, lines: List[str], path: Path) -> Tuple[int, Dict]:
        """Correctness (35): Godot headless parse + required functions + no stubs."""
        details = {}
        score = 0

        # 1. Godot headless parse (15 pts)
        parse_ok, parse_msg = self._godot_parse_check(path)
        details['parse_ok'] = parse_ok
        details['parse_message'] = parse_msg
        if parse_ok:
            score += 15

        # 2. Required functions present (15 pts)
        found_funcs = set()
        for func in self.required_funcs:
            # Match func_name( or func_name (with optional whitespace)
            pattern = rf'\bfunc\s+{re.escape(func)}\s*\('
            if re.search(pattern, content):
                found_funcs.add(func)
        details['required_funcs'] = list(self.required_funcs)
        details['found_funcs'] = list(found_funcs)
        details['missing_funcs'] = list(self.required_funcs - found_funcs)
        if self.required_funcs:
            func_ratio = len(found_funcs) / len(self.required_funcs)
            score += int(15 * func_ratio)
        else:
            score += 15 if not self.require_spec else 0  # full credit only if spec not required

        # 3. No stub/pass/empty function bodies (5 pts)
        stub_count = self._count_stub_functions(content)
        details['stub_function_count'] = stub_count
        if stub_count == 0:
            score += 5
        elif stub_count <= 2:
            score += 3
        elif stub_count <= 5:
            score += 1
        # else 0

        return min(score, 35), details

    def _godot_parse_check(self, path: Path) -> Tuple[bool, str]:
        """Run Godot headless --check-only to validate syntax."""
        # Try to find godot executable
        godot_paths = [
            'godot',
            'godot4',
            'C:/Users/bluue/Downloads/Godot_v4.7.1-stable_win64.exe/Godot_v4.7.1-stable_win64_console.exe',
            'C:/Program Files/Godot/Godot_v4.3-stable_win64.exe',
            'C:/Program Files/Godot/Godot_v4.2-stable_win64.exe',
            '/usr/bin/godot',
            '/usr/local/bin/godot',
        ]
        godot_cmd = None
        for gp in godot_paths:
            try:
                result = subprocess.run([gp, '--version'], capture_output=True, timeout=5)
                if result.returncode == 0:
                    godot_cmd = gp
                    break
            except Exception:
                continue

        if not godot_cmd:
            return False, 'Godot executable not found in PATH or common locations'

        try:
            # Compile THIS file directly with --check-only. The old approach
            # (copy to _rubric_probe.gd + --import) did NOT actually compile the
            # file: Godot --import skips unreferenced scripts, so a broken
            # artifact reported parse_ok=True and graded 73-85 yet failed at
            # the wire step. --check-only --script forces Godot to compile the
            # real file and surface its actual parse errors.
            result = subprocess.run(
                [godot_cmd, '--headless', '--path', str(self.project_root),
                 '--check-only', '--script', str(path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            err = result.stdout + result.stderr
            bad = 'SCRIPT ERROR' in err or 'Parse Error' in err or 'Compile Error' in err
            if bad:
                # FALSE-POSITIVE GUARD (2026-09-05): isolated --check-only --script
                # does NOT register autoloads (EventBus, GameState, ...). Any
                # artifact that CALLS an autoload method fails here with
                # "Identifier not found: <autoload>" even though it's valid.
                # Accept ONLY when every error is a missing known-autoload.
                autoloads = self._project_autoloads()
                err_lines = [l for l in err.splitlines()
                             if 'SCRIPT ERROR' in l or 'Parse Error' in l or 'Compile Error' in l]
                if autoloads and err_lines and all(self._is_autoload_missing(l, autoloads)
                                                   for l in err_lines):
                    return True, 'Parse OK (autoload false positive)'
                return False, err.strip()[-300:] or 'Parse failed'
            return True, 'Parse OK'
        except subprocess.TimeoutExpired:
            return False, 'Godot parse timeout'
        except Exception as e:
            return False, f'Godot parse error: {e}'

    def _project_autoloads(self) -> set:
        """Read project.godot [autoload] section for registered autoload names."""
        try:
            cfg = (self.project_root / 'project.godot').read_text(encoding='utf-8', errors='replace')
        except Exception:
            return set()
        names, in_autoload = set(), False
        for line in cfg.splitlines():
            s = line.strip()
            if s == '[autoload]':
                in_autoload = True
                continue
            if in_autoload and s.startswith('['):
                in_autoload = False
            if in_autoload and '=' in s and not s.startswith(';'):
                names.add(s.split('=')[0].strip())
        return names

    def _is_autoload_missing(self, line: str, autoloads: set) -> bool:
        """True if the error line is 'Identifier not found: <known-autoload>'."""
        m = re.search(r'Identifier not found:\s*(\w+)', line)
        return bool(m and m.group(1) in autoloads)

    def _count_stub_functions(self, content: str) -> int:
        """Count functions that are stubs (pass, ..., empty body, just return)."""
        # Find all function definitions
        func_pattern = r'\bfunc\s+(\w+)\s*\([^)]*\)\s*(?:->\s*\w+)?\s*:'
        stub_count = 0

        for match in re.finditer(func_pattern, content):
            func_start = match.end()
            # Find the function body (indented block after colon)
            body_match = re.search(r'\n(\s+)(.+)', content[func_start:])
            if not body_match:
                stub_count += 1
                continue

            indent = body_match.group(1)
            body_lines = []
            remaining = content[func_start + body_match.end():]
            for line in remaining.splitlines():
                if line.strip() == '':
                    body_lines.append(line)
                    continue
                if line.startswith(indent) or line.strip() == '':
                    body_lines.append(line)
                else:
                    break

            # Strip leading blank + comment-only lines: a function that starts
            # with a doc comment is NOT a stub.
            real = []
            for bl in body_lines:
                s = bl.strip()
                if not s or s.startswith('#'):
                    continue
                real.append(bl)
            body_text = '\n'.join(real).strip()
            # Check if body is essentially empty/stub
            if (not body_text or
                body_text == 'pass' or
                body_text == '...' or
                re.match(r'^return\s+(?:null|0|false|""|\{\}|\[\])?\s*$', body_text) or
                re.match(r'^#.*', body_text)):  # Only comments
                stub_count += 1

        return stub_count

    def _check_completeness(self, content: str, lines: List[str]) -> Tuple[int, Dict]:
        """Completeness (15): Required function names present (from --req)."""
        # This overlaps with correctness but focuses on the --req contract
        details = {}
        score = 0

        found_funcs = set()
        for func in self.required_funcs:
            pattern = rf'\bfunc\s+{re.escape(func)}\s*\('
            if re.search(pattern, content):
                found_funcs.add(func)

        details['required_funcs'] = list(self.required_funcs)
        details['found_funcs'] = list(found_funcs)
        details['missing_funcs'] = list(self.required_funcs - found_funcs)

        if self.required_funcs:
            func_ratio = len(found_funcs) / len(self.required_funcs)
            score = int(15 * func_ratio)
        else:
            score = 15 if not self.require_spec else 0

        return min(score, 15), details

    def _check_idiomatic_quality(self, content: str, lines: List[str]) -> Tuple[int, Dict]:
        """Idiomatic Quality (25): Godot 4 patterns, type hints, @export, signals, no drift."""
        details = {}
        score = 0

        # 1. Type hints on functions (5 pts)
        typed_funcs = len(re.findall(r'\bfunc\s+\w+\s*\([^)]*\)\s*->\s*\w+\s*:', content))
        total_funcs = len(re.findall(r'\bfunc\s+\w+\s*\([^)]*\)\s*(?:->\s*\w+)?\s*:', content))
        details['typed_functions'] = typed_funcs
        details['total_functions'] = total_funcs
        if total_funcs > 0:
            type_ratio = typed_funcs / total_funcs
            if type_ratio >= 0.8:
                score += 5
            elif type_ratio >= 0.5:
                score += 3
            elif type_ratio > 0:
                score += 1

        # 2. @export usage (5 pts)
        export_count = len(re.findall(r'@export\b', content))
        details['export_count'] = export_count
        if export_count >= 3:
            score += 5
        elif export_count >= 1:
            score += 3
        elif export_count == 0 and total_funcs <= 2:
            score += 2  # Small scripts may not need exports

        # 3. Signal usage (5 pts)
        signal_decls = len(re.findall(r'\bsignal\s+\w+', content))
        signal_emits = len(re.findall(r'\.emit\s*\(', content))
        details['signal_declarations'] = signal_decls
        details['signal_emits'] = signal_emits
        if signal_decls > 0 and signal_emits > 0:
            score += 5
        elif signal_decls > 0 or signal_emits > 0:
            score += 3

        # 4. Proper group/signal wiring (5 pts)
        group_usage = len(re.findall(r'get_nodes_in_group\s*\(', content))
        connect_oneshot = len(re.findall(r'CONNECT_ONE_SHOT', content))
        call_deferred = len(re.findall(r'\.call_deferred\s*\(', content))
        await_usage = len(re.findall(r'\bawait\s+', content))
        details['get_nodes_in_group'] = group_usage
        details['connect_one_shot'] = connect_oneshot
        details['call_deferred'] = call_deferred
        details['await_usage'] = await_usage
        wiring_score = 0
        if group_usage > 0:
            wiring_score += 2
        if connect_oneshot > 0:
            wiring_score += 1
        if call_deferred > 0:
            wiring_score += 1
        if await_usage > 0:
            wiring_score += 1
        score += min(wiring_score, 5)

        # 5. No Godot-3 drift (5 pts)
        drift_penalties = []
        for pattern, msg in self.GODOT3_DRIFT_PATTERNS:
            matches = len(re.findall(pattern, content))
            if matches > 0:
                drift_penalties.append(f'{msg} ({matches}x)')
        details['godot3_drift'] = drift_penalties
        if not drift_penalties:
            score += 5
        elif len(drift_penalties) <= 2:
            score += 3
        elif len(drift_penalties) <= 4:
            score += 1

        return min(score, 25), details

    def _check_architecture_integration(self, content: str, lines: List[str]) -> Tuple[int, Dict]:
        """Architecture & Integration (10): References real game symbols, no hallucinations."""
        details = {}
        score = 0

        # 1. References actual game classes (5 pts)
        referenced_classes = set()
        # Match ClassName.method() or ClassName.property or new ClassName()
        class_refs = re.findall(r'\b([A-Z][a-zA-Z0-9]*)\s*\.\s*\w+', content)
        class_refs += re.findall(r'\bnew\s+([A-Z][a-zA-Z0-9]*)\s*\(', content)
        class_refs += re.findall(r'\b([A-Z][a-zA-Z0-9]*)\s*\{', content)  # Dictionary literal with class
        for ref in class_refs:
            if ref in self.project_classes:
                referenced_classes.add(ref)

        details['referenced_project_classes'] = list(referenced_classes)
        if len(referenced_classes) >= 3:
            score += 5
        elif len(referenced_classes) >= 1:
            score += 3

        # 2. No hallucinated symbols (5 pts)
        # Check for references to classes/groups/signals NOT in project
        all_refs = set(class_refs)
        # Group references
        group_refs = re.findall(r'get_nodes_in_group\s*\(\s*["\'](\w+)["\']', content)
        group_refs += re.findall(r'add_to_group\s*\(\s*["\'](\w+)["\']', content)
        group_refs += re.findall(r'remove_from_group\s*\(\s*["\'](\w+)["\']', content)
        all_refs.update(group_refs)

        # Signal references
        signal_refs = re.findall(r'\.emit\s*\(\s*["\'](\w+)["\']', content)
        signal_refs += re.findall(r'connect\s*\(\s*["\'](\w+)["\']', content)
        all_refs.update(signal_refs)

        # Godot 4 built-in classes that should NOT be flagged as hallucinated
        GODOT_BUILTINS = {
            'Node', 'Node2D', 'Node3D', 'Control', 'CanvasItem', 'CanvasLayer',
            'Window', 'Viewport', 'Resource', 'RefCounted', 'Object',
            'SceneTree', 'Engine', 'Time', 'OS', 'Input', 'InputMap', 'DisplayServer',
            'RenderingServer', 'PhysicsServer', 'AudioServer', 'TextServer',
            'FileAccess', 'DirAccess', 'ConfigFile', 'JSON', 'HTTPRequest',
            'WebSocketPeer', 'PacketPeer', 'StreamPeer', 'StreamPeerTCP',
            'StreamPeerBuffer', 'StreamPeerGZIP', 'EditorInterface', 'EditorPlugin',
            'Button', 'Label', 'VBoxContainer', 'HBoxContainer', 'BoxContainer',
            'GridContainer', 'FlowContainer', 'ScrollContainer', 'MarginContainer',
            'PanelContainer', 'CenterContainer', 'TabContainer', 'SplitContainer',
            'ColorRect', 'TextureRect', 'Sprite2D', 'AnimatedSprite2D', 'NinePatchRect',
            'ProgressBar', 'Slider', 'SpinBox', 'LineEdit', 'TextEdit', 'RichTextLabel',
            'Tree', 'ItemList', 'TabBar', 'MenuButton', 'PopupMenu', 'OptionButton',
            'CheckBox', 'CheckButton', 'ToggleButton', 'BaseButton', 'LinkButton',
            'Color', 'Rect2', 'Rect2i', 'Vector2', 'Vector2i', 'Vector3', 'Vector3i',
            'Vector4', 'Vector4i', 'Transform2D', 'Transform3D', 'Basis', 'Quaternion',
            'Plane', 'AABB', 'RID', 'Signal', 'Callable', 'Array', 'Dictionary',
            'String', 'StringName', 'PackedByteArray', 'PackedInt32Array', 'PackedInt64Array',
            'PackedFloat32Array', 'PackedFloat64Array', 'PackedStringArray', 'PackedVector2Array',
            'PackedVector3Array', 'PackedColorArray', 'IDs', 'Geometry2D', 'Geometry3D',
            # Common Godot 4 UI nodes
            'ScreenShake2D', 'AnimatedSprite3D', 'Sprite3D', 'GPUParticles2D', 'GPUParticles3D',
            'CPUParticles2D', 'CPUParticles3D', 'Area2D', 'Area3D', 'CharacterBody2D', 'CharacterBody3D',
            'RigidBody2D', 'RigidBody3D', 'StaticBody2D', 'StaticBody3D', 'CollisionShape2D', 'CollisionShape3D',
            'CollisionPolygon2D', 'CollisionPolygon3D', 'RayCast2D', 'RayCast3D', 'ShapeCast2D', 'ShapeCast3D',
        }

        hallucinated = []
        for ref in all_refs:
            if (ref not in self.project_classes and
                ref not in self.project_groups and
                ref not in self.project_signals and
                ref not in GODOT_BUILTINS and
                not ref.isupper()):  # Skip constants
                # Heuristic: looks like a class/signal/group name
                if re.match(r'^[A-Z][a-zA-Z0-9]*$', ref) or '_' in ref:
                    hallucinated.append(ref)

        details['hallucinated_symbols'] = hallucinated
        if not hallucinated:
            score += 5
        elif len(hallucinated) <= 2:
            score += 3
        elif len(hallucinated) <= 5:
            score += 1

        return min(score, 10), details

    def _check_robustness_style(self, content: str, lines: List[str]) -> Tuple[int, Dict]:
        """Robustness & Style (10): Constants, function length, no prints, snake_case."""
        details = {}
        score = 0

        # 1. Constants instead of magic numbers in LOGIC (3 pts)
        # Look for numeric literals in algorithmic code, NOT in UI layout/data setup
        # Exclude: Vector2/Vector3 constructors, Color constructors, Rect2, dictionary literals
        # Exclude lines with: custom_minimum_size, add_theme, Font, Color(, Vector2(, Vector3(, Rect2(
        logic_lines = []
        in_export_dict = False
        export_brace_depth = 0
        for line in lines:
            stripped = line.strip()
            # Track if we're in an export var = { ... } data dictionary
            if not in_export_dict:
                # Match @export var NAME: Type = { or @export var NAME = {
                if re.search(r'@export\s+var\s+\w+\s*(:?\s*\w+)?\s*=', stripped) and '{' in stripped:
                    in_export_dict = True
                    export_brace_depth = stripped.count('{') - stripped.count('}')
            if in_export_dict:
                export_brace_depth += stripped.count('{') - stripped.count('}')
                if export_brace_depth <= 0:
                    in_export_dict = False
                continue
            # Skip UI/layout constants
            if any(skip in stripped for skip in [
                'custom_minimum_size', 'custom_maximum_size',
                'add_theme_color_override', 'add_theme_font_size_override',
                'add_theme_constant_override', 'add_theme_style_override',
                'add_theme_icon_override', 'add_theme_font_override',
                'theme_override', 'Font', 'Color(', 'Vector2(', 'Vector3(',
                'Rect2(', 'Rect2i(', 'AABB(', 'Transform2D(', 'Transform3D(',
                'Basis(', 'Quaternion(', 'Plane(', 'RID(', 'Packed',
                'set(', 'get(', 'clamp(', 'lerp(', 'move_toward(',
                'abs(', 'max(', 'min(', 'pow(', 'sqrt(', 'sin(', 'cos(',
                'tan(', 'asin(', 'acos(', 'atan(', 'atan2(', 'deg_to_rad(',
                'rad_to_deg(', 'lerp_angle(', 'smoothstep(', 'posmod(',
                'fposmod(', 'floor(', 'ceil(', 'round(', 'stepify(', 'sign(',
                'wrapi(', 'wrapf(', 'snapped(', 'rotated(', 'angle(', 'distance_to(',
                'length(', 'length_squared(', 'normalized(', 'is_zero_approx(',
                'is_equal_approx(', 'direction_to(', 'angle_to(', 'cross(',
                'dot(', 'project(', 'slide(', 'reflect(', 'bounce(', 'limit_length(',
            ]):
                continue
            # Skip dictionary/data definition lines
            if re.match(r'^\s*[\w_]+:\s*\d+', stripped):  # key: value in dict
                continue
            if re.match(r'^\s*\d+:\s*', stripped):  # numbered dict entries
                continue
            logic_lines.append(line)
        logic_content = '\n'.join(logic_lines)

        magic_numbers = re.findall(r'(?<![\w.])\b\d{2,}\b(?![\w.])', logic_content)
        # Filter out common safe numbers
        safe_numbers = {'0', '1', '2', '3', '4', '5', '10', '100', '1000', '60', '30', '10000', '600', '800', '1024', '2048'}
        magic_numbers = [n for n in magic_numbers if n not in safe_numbers]
        # Check for constants (CONSTANT_NAME = value)
        const_count = len(re.findall(r'\b[A-Z_][A-Z0-9_]*\s*=\s*\d+', content))
        details['magic_numbers_found'] = magic_numbers[:10]
        details['constant_definitions'] = const_count
        if const_count > 0 and len(magic_numbers) == 0:
            score += 3
        elif const_count > 0 and len(magic_numbers) <= 3:
            score += 2
        elif len(magic_numbers) == 0:
            score += 2
        elif len(magic_numbers) <= 5:
            score += 1

        # 2. Reasonable function length (3 pts)
        func_lengths = self._get_function_lengths(content)
        details['function_lengths'] = func_lengths
        long_funcs = [f for f, l in func_lengths.items() if l > 50]
        very_long_funcs = [f for f, l in func_lengths.items() if l > 100]
        details['long_functions'] = long_funcs
        details['very_long_functions'] = very_long_funcs
        if not very_long_funcs and len(long_funcs) <= 2:
            score += 3
        elif not very_long_funcs and len(long_funcs) <= 5:
            score += 2
        elif not very_long_funcs:
            score += 1

        # 3. No debug prints (2 pts)
        print_count = len(re.findall(r'\bprint\s*\(', content))
        print_debug = len(re.findall(r'print\(.*debug|print\(.*DEBUG', content, re.IGNORECASE))
        details['print_statements'] = print_count
        details['debug_prints'] = print_debug
        if print_count == 0:
            score += 2
        elif print_count <= 2 and print_debug == 0:
            score += 1

        # 4. snake_case naming (2 pts)
        # Check function names, variable names
        func_names = re.findall(r'\bfunc\s+(\w+)', content)
        var_names = re.findall(r'\bvar\s+(\w+)', content)
        all_names = func_names + var_names
        non_snake = [n for n in all_names if not re.match(r'^[a-z][a-z0-9_]*$', n) and '_' not in n and n[0].islower()]
        # Also check for camelCase
        camel_case = [n for n in all_names if re.match(r'^[a-z]+[A-Z]', n)]
        details['non_snake_case'] = non_snake + camel_case
        if not non_snake and not camel_case:
            score += 2
        elif len(non_snake) + len(camel_case) <= 3:
            score += 1

        return min(score, 10), details

    def _get_function_lengths(self, content: str) -> Dict[str, int]:
        """Calculate line count for each function."""
        func_lengths = {}
        lines = content.splitlines()
        in_func = False
        current_func = None
        func_indent = 0
        func_start_line = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Function start
            func_match = re.match(r'^(\s*)func\s+(\w+)\s*\(', line)
            if func_match:
                if in_func and current_func:
                    func_lengths[current_func] = i - func_start_line
                in_func = True
                current_func = func_match.group(2)
                func_indent = len(func_match.group(1))
                func_start_line = i
                continue

            if in_func:
                # Check if we've exited the function (dedent)
                if stripped and not line.startswith(' ' * (func_indent + 1)) and not line.startswith('\t' * (func_indent + 1)):
                    func_lengths[current_func] = i - func_start_line
                    in_func = False
                    current_func = None

        # Handle last function
        if in_func and current_func:
            func_lengths[current_func] = len(lines) - func_start_line

        return func_lengths

    def _check_anti_slop(self, content: str, lines: List[str]) -> Tuple[int, Dict]:
        """Anti-Slop / Artifact Quality (5): No TODO/FIXME, no placeholder returns, minimum viable, no runaway repetition."""
        details = {}
        score = 0

        # 0. RUN-AWAY REPETITION DETECTOR (2026-09-06, user point 4): a model in a
        #    degenerate generation loop emits the SAME line (or near-identical block)
        #    hundreds/thousands of times (harm-078.gd = 1712x `player.stream =
        #    PackedByteArray()`). The old rubric scored such a dump fine on the other
        #    dimensions (it has funcs, no TODO, plenty of lines) — the repetition was
        #    invisible. This is a HARD GATE: if any single non-trivial line repeats
        #    more than REP_MAX times, the artifact is a runaway dump and anti-slop
        #    scores 0 regardless of the other checks.
        REP_MAX = 20
        line_counts = {}
        for ln in lines:
            s = ln.strip()
            # Skip trivial/structural lines that legitimately repeat (braces, blank,
            # single-word keywords like pass/return, comments).
            if not s or s in ('{', '}', 'pass', 'return', 'break', 'continue') or s.startswith('#'):
                continue
            line_counts[s] = line_counts.get(s, 0) + 1
        worst_line, worst_count = (None, 0)
        for s, c in line_counts.items():
            if c > worst_count:
                worst_line, worst_count = s, c
        details['max_line_repetition'] = worst_count
        details['worst_repeated_line'] = (worst_line[:80] if worst_line else None)
        if worst_count > REP_MAX:
            details['runaway_repetition'] = True
            details['reason'] = f"runaway repetition: line repeated {worst_count}x (max {REP_MAX})"
            return 0, details
        details['runaway_repetition'] = False

        # 1. No TODO/FIXME (2 pts)
        todo_count = len(re.findall(r'(TODO|FIXME|XXX|HACK)', content, re.IGNORECASE))
        details['todo_fixme_count'] = todo_count
        if todo_count == 0:
            score += 2
        elif todo_count <= 2:
            score += 1

        # 2. No placeholder returns (2 pts)
        # return 0, return null, return false, return true, return "", return [], return {} 
        # ONLY when they're the ONLY statement in a function that should compute something
        placeholder_returns = 0
        func_returns = re.findall(r'func\s+(\w+)\s*\([^)]*\)\s*(?:->\s*\w+)?\s*:\n((?:\s.*\n)*)', content)
        for func_name, body in func_returns:
            body = body.strip()
            # Check if body is essentially just a placeholder return
            if re.match(r'^return\s+(?:0|null|false|true|""|\'\'|\[\]|\{\})\s*(?:#.*)?$', body):
                placeholder_returns += 1
        details['placeholder_returns'] = placeholder_returns
        if placeholder_returns == 0:
            score += 2
        elif placeholder_returns <= 2:
            score += 1

        # 3. Minimum viable artifact: >= 15 non-empty lines (1 pt)
        non_empty = len([l for l in lines if l.strip() and not l.strip().startswith('#')])
        details['non_empty_lines'] = non_empty
        if non_empty >= 15:
            score += 1

        return min(score, 5), details

    def _score_to_grade(self, score: int) -> str:
        for grade, threshold in sorted(self.GRADE_THRESHOLDS.items(), key=lambda x: -x[1]):
            if score >= threshold:
                return grade
        return 'F'


def main():
    parser = argparse.ArgumentParser(
        description='Bluu Ink Studios GDScript Grading Rubric (Production Gate)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python grade_rubric.py script.gd --req _ready _process --hints Player Enemy
  python grade_rubric.py script.gd --req func1 func2 --project-root C:/path/to/Galage
        """
    )
    parser.add_argument('file', help='GDScript file to grade')
    parser.add_argument('--req', nargs='*', default=[], help='Required function names')
    parser.add_argument('--hints', nargs='*', default=[], help='Hint symbols (classes/groups/signals)')
    parser.add_argument('--project-root', help='Path to Galage project root for integration checks')
    parser.add_argument('--require-spec', action='store_true', help='Do NOT auto-grant completeness credit for missing task specs (honest artifact-quality grading)')
    parser.add_argument('--json', action='store_true', help='Output full JSON breakdown')
    parser.add_argument('--quiet', action='store_true', help='Only output SCORE and GRADE')

    args = parser.parse_args()

    rubric = GDScriptRubric(
        required_funcs=args.req,
        hints=args.hints,
        project_root=args.project_root,
        require_spec=args.require_spec,
    )

    result = rubric.grade(args.file)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    elif args.quiet:
        print(f"SCORE: {result.score}/100 GRADE: {result.grade}")
    else:
        print(f"SCORE: {result.score}/100 GRADE: {result.grade}")
        print("\nBreakdown:")
        for dim, score in result.breakdown.items():
            print(f"  {dim}: {score}")
        print("\nDetails:")
        for dim, details in result.details.items():
            if dim not in ['file', 'lines', 'non_empty_lines']:
                print(f"  {dim}: {details}")

    # Exit code based on grade (for CI gates)
    if result.grade in ('A', 'B'):
        sys.exit(0)
    elif result.grade == 'C':
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == '__main__':
    main()