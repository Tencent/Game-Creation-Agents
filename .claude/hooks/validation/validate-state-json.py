#!/usr/bin/env python3
"""
Validate state.json integrity — cross-check timestamps and audit log.
Called by enforce-pipeline.sh.

Catches:
- iteration_count bumped without corresponding audit log entries
- verify_runs incremented without actual screenshot files being created
- Timestamps that don't make sense for configured iteration cadence
- video-analysis.json is stale vs last code modification
"""
import json
import sys
from pathlib import Path

QUALITY_PROFILES = {"art_directed", "responsive", "pixel_art", "minimal"}
LEVEL_MODELS = {"authored", "procedural", "board", "none"}
GENRES = {
    "platform",
    "beat_em_up",
    "bullet_hell",
    "roguelike",
    "puzzle",
    "tower_defence",
    "sandbox",
    "simulation",
    "card",
    "idle",
    "custom",
}
INTERACTION_PROFILES = {
    "arcade-gravity",
    "arcade-no-gravity",
    "matter",
    "none",
}
GUARDED_COUNTER_EVENTS = {
    "verify_runs": "verify_run",
    "iteration_count": "iteration_complete",
}
AUDIT_OFFSETS_VERSION = 1
COUNTER_MIGRATION_VERSION = 1
COUNTER_MIGRATION_SOURCE = "legacy_unscoped_audit"


def _first_existing(base, rels):
    """Return the first existing base/rel (current layout → legacy fallbacks),
    or base/rels[0] if none exist."""
    for rel in rels:
        candidate = base / rel
        if candidate.exists():
            return candidate
    return base / rels[0]


def strip_yaml_comment(line):
    quote = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in {"'", '"'}:
            quote = None if quote == ch else ch if quote is None else quote
            continue
        if ch == "#" and quote is None:
            return line[:idx]
    return line


def parse_scalar(value):
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_simple_yaml(text):
    root = {}
    stack = [(-1, root)]
    for raw_line in text.splitlines():
        line = strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"invalid YAML line: {raw_line!r}")
        indent = len(line) - len(line.lstrip(" "))
        key, _, raw_value = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"invalid YAML indentation: {raw_line!r}")
        parent = stack[-1][1]
        if raw_value.strip() == "":
            child = {}
            parent[key.strip()] = child
            stack.append((indent, child))
        else:
            parent[key.strip()] = parse_scalar(raw_value)
    return root


def load_config(repo_root):
    config_path = repo_root / "gca.config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(f"required config authority is missing: {config_path}")
    return parse_simple_yaml(config_path.read_text(encoding="utf-8"))


def config_get(config, dotted_key, default):
    current = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _non_negative_int(value, field, errors):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{field} must be a non-negative integer.")
        return None
    return value


def validate_counter_contract(state, errors):
    counters = {
        key: _non_negative_int(state.get(key, 0), key, errors)
        for key in GUARDED_COUNTER_EVENTS
    }
    offsets_version = state.get("audit_offsets_version")
    if (
        isinstance(offsets_version, bool)
        or offsets_version != AUDIT_OFFSETS_VERSION
    ):
        errors.append(
            "state.json is missing initialized audit_offsets_version; "
            "legacy offsets cannot be reconstructed for an existing run."
        )
    raw_offsets = state.get("audit_offsets")
    if not isinstance(raw_offsets, dict):
        errors.append("audit_offsets must be an initialized object.")
        raw_offsets = {}
    offsets = {
        key: _non_negative_int(
            raw_offsets.get(key),
            f"audit_offsets.{key}",
            errors,
        )
        for key in GUARDED_COUNTER_EVENTS
    }

    marker = state.get("counter_migration")
    if marker is None:
        if any(value not in (None, 0) for value in offsets.values()):
            errors.append("Non-zero audit_offsets require a counter_migration snapshot.")
    elif not isinstance(marker, dict):
        errors.append("counter_migration must be an object.")
    else:
        pipeline_run_id = state.get("pipeline_run_id")
        marker_version = marker.get("version")
        if (
            isinstance(marker_version, bool)
            or marker_version != COUNTER_MIGRATION_VERSION
            or marker.get("source") != COUNTER_MIGRATION_SOURCE
            or marker.get("pipeline_run_id") != pipeline_run_id
            or not isinstance(marker.get("migrated_at"), str)
            or not marker.get("migrated_at")
        ):
            errors.append(
                "counter_migration marker is invalid or belongs to another run."
            )
        snapshot = marker.get("snapshot")
        if not isinstance(snapshot, dict):
            errors.append("counter_migration.snapshot must be an object.")
        else:
            legacy_counters = snapshot.get("counters")
            legacy_audits = snapshot.get("unscoped_audit_counts")
            tolerated_missing = snapshot.get("tolerated_missing")
            tolerance = _non_negative_int(
                snapshot.get("audit_tolerance"),
                "counter_migration.snapshot.audit_tolerance",
                errors,
            )
            if not all(
                isinstance(value, dict)
                for value in (
                    legacy_counters,
                    legacy_audits,
                    tolerated_missing,
                )
            ):
                errors.append(
                    "counter_migration.snapshot is missing counter/audit maps."
                )
            elif tolerance is not None:
                for key, event in GUARDED_COUNTER_EVENTS.items():
                    legacy_counter = _non_negative_int(
                        legacy_counters.get(key),
                        f"counter_migration.snapshot.counters.{key}",
                        errors,
                    )
                    legacy_events = _non_negative_int(
                        legacy_audits.get(event),
                        "counter_migration.snapshot."
                        f"unscoped_audit_counts.{event}",
                        errors,
                    )
                    missing_events = _non_negative_int(
                        tolerated_missing.get(key),
                        f"counter_migration.snapshot.tolerated_missing.{key}",
                        errors,
                    )
                    if None in (legacy_counter, legacy_events, missing_events):
                        continue
                    if (
                        offsets[key] != legacy_counter
                        or missing_events != max(0, legacy_counter - legacy_events)
                        or missing_events > tolerance
                    ):
                        errors.append(
                            f"counter_migration snapshot for {key} does not "
                            "match the frozen audit offset."
                        )

    for key in GUARDED_COUNTER_EVENTS:
        if (
            offsets[key] is not None
            and counters[key] is not None
            and offsets[key] > counters[key]
        ):
            errors.append(f"audit_offsets.{key} cannot exceed {key}.")
    return counters, offsets


def main():
    if len(sys.argv) < 2:
        print("Usage: validate-state-json.py <game_dir>")
        sys.exit(1)

    game_dir = Path(sys.argv[1])
    state_file = game_dir / ".playtest" / "state.json"
    audit_file = game_dir / ".playtest" / "audit.jsonl"
    repo_root = game_dir.parent.parent
    try:
        config = load_config(repo_root)
    except (OSError, ValueError) as exc:
        print(f"FAIL: Invalid config authority: {exc}")
        sys.exit(1)
    min_iteration_gap = int(
        config_get(config, "pipeline.min_iteration_gap_seconds", 120)
    )
    min_screenshots = int(config_get(config, "pipeline.min_screenshots", 3))
    min_unique_screenshots = int(
        config_get(config, "pipeline.min_unique_screenshots", 3)
    )
    max_iterations = int(config_get(config, "pipeline.max_iterations", 2))
    stale_tolerance = int(
        config_get(config, "pipeline.staleness_tolerance_seconds", 60)
    )

    if not state_file.exists():
        print("SKIP: No state.json")
        sys.exit(0)

    try:
        state = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: Invalid state.json: {exc}")
        sys.exit(1)
    if not isinstance(state, dict):
        print("FAIL: Invalid state.json: root must be an object.")
        sys.exit(1)
    errors = []
    if (game_dir / ".playtest" / "state-transaction.pending.json").exists():
        errors.append("state/resume transaction is incomplete; rerun write_pipeline_state.")
    pipeline_run_id = state.get("pipeline_run_id")
    build_digest = state.get("build_digest")
    if not pipeline_run_id or not build_digest:
        errors.append("state.json is missing pipeline_run_id/build_digest.")

    genre = state.get("genre")
    interaction_profile = state.get("interaction_profile")
    quality_profile = state.get("quality_profile")
    level_model = state.get("level_model")
    if (
        genre not in GENRES
        or interaction_profile not in INTERACTION_PROFILES
        or quality_profile not in QUALITY_PROFILES
        or level_model not in LEVEL_MODELS
    ):
        errors.append(
            "state.json must contain valid genre, interaction_profile, "
            "quality_profile, and level_model."
        )
    elif state.get("level_design_required") is not (level_model == "authored"):
        errors.append(
            "level_design_required must equal (level_model == 'authored')."
        )
    design_axes = state.get("design_axes")
    if (
        isinstance(design_axes, dict)
        and design_axes.get("physics") is not None
        and design_axes.get("physics") != interaction_profile
    ):
        errors.append("interaction_profile must equal design_axes.physics.")
    if level_model in LEVEL_MODELS and level_model != "authored":
        if state.get("has_levels") is True:
            errors.append(
                "has_levels=true is valid only for level_model='authored'."
            )

    counters, audit_offsets = validate_counter_contract(state, errors)
    iteration_count = counters["iteration_count"] or 0
    verify_runs = counters["verify_runs"] or 0
    if max_iterations < 1:
        errors.append("pipeline.max_iterations must be a positive integer.")
    elif iteration_count > max_iterations:
        errors.append(
            f"iteration_count={iteration_count} exceeds "
            f"pipeline.max_iterations={max_iterations}."
        )
    expected_iteration_budget = {
        "max_iterations": max_iterations,
        "used_iterations": iteration_count,
        "remaining_iterations": max(0, max_iterations - iteration_count),
        "exhausted": iteration_count >= max_iterations,
    }
    persisted_iteration_budget = state.get("iteration_budget")
    if (
        persisted_iteration_budget is not None
        and persisted_iteration_budget != expected_iteration_budget
    ):
        errors.append(
            "state.json iteration_budget does not match the configured "
            "pipeline.max_iterations and guarded iteration_count."
        )

    # ─── 1. Audit log must have entries matching claimed work ───────────
    audit_entries = []
    if audit_file.exists():
        lines = [
            line
            for line in audit_file.read_text().strip().split("\n")
            if line.strip()
        ]
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                audit_entries.append(entry)
        if pipeline_run_id:
            audit_entries = [
                entry
                for entry in audit_entries
                if entry.get("pipeline_run_id") == pipeline_run_id
            ]

    verify_events = [e for e in audit_entries if e.get("event") == "verify_run"]
    asset_events = [e for e in audit_entries if e.get("event") == "assets_generated"]
    iteration_events = [
        e for e in audit_entries if e.get("event") == "iteration_complete"
    ]
    event_lists = {
        "verify_run": verify_events,
        "iteration_complete": iteration_events,
    }
    for key, event in GUARDED_COUNTER_EVENTS.items():
        counter = counters[key]
        offset = audit_offsets[key]
        if counter is None or offset is None:
            continue
        required_events = max(0, counter - offset)
        actual_events = len(event_lists[event])
        if actual_events < required_events:
            errors.append(
                f"state.json claims {key}={counter} with frozen offset={offset}, "
                f"but this pipeline_run_id only has {actual_events} {event} "
                f"entries; {required_events} required."
            )

    # Check timing: iterations can't happen faster than the configured cadence.
    if len(iteration_events) >= 2:
        timestamps = sorted(e.get("timestamp", 0) for e in iteration_events)
        min_gap = float("inf")
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            if gap < min_gap:
                min_gap = gap
        if min_gap < min_iteration_gap:
            errors.append(
                f"Iterations happening too fast: minimum gap is {int(min_gap)}s. "
                f"Configured minimum is {min_iteration_gap}s."
            )

    if state.get("assets_generated") and len(asset_events) == 0:
        errors.append(
            "state.json says assets_generated=true but no run-scoped "
            "assets_generated event exists."
        )

    # ─── 2. Screenshots must exist and be distinct ─────────────────────
    playtest_dir = game_dir / ".playtest"
    current_run_file = playtest_dir / "verify" / "current-run.json"
    current_run = {}
    if current_run_file.is_file():
        try:
            current_run = json.loads(current_run_file.read_text())
        except (OSError, ValueError):
            current_run = {}
    current_verify_runs = max(
        0,
        verify_runs - (audit_offsets["verify_runs"] or 0),
    )
    if current_verify_runs > 0 and (
        current_run.get("pipeline_run_id") != pipeline_run_id
        or current_run.get("build_digest") != build_digest
        or not current_run.get("verify_run_id")
    ):
        errors.append("verify/current-run.json is missing or cross-run relative to state.json.")
    started_at_ns = int(current_run.get("started_at_ns") or 0)
    screenshot_roots = [
        playtest_dir / "verify" / "test-run" / "screenshots",
        playtest_dir / "verify" / "play" / "frames",
        playtest_dir / "verify" / "visual-diff",
    ]
    screenshots = [
        path
        for root in screenshot_roots
        if root.is_dir()
        for path in root.rglob("*.png")
        if path.stat().st_mtime_ns >= started_at_ns
    ]
    if current_verify_runs > 0 and len(screenshots) < min_screenshots:
        errors.append(
            f"Claims {current_verify_runs} current-run verification(s) but only "
            f"{len(screenshots)} screenshots exist."
        )

    # Check screenshots aren't all identical
    if len(screenshots) >= min_screenshots:
        import hashlib
        hashes = set()
        for s in screenshots:
            h = hashlib.md5(s.read_bytes()).hexdigest()
            hashes.add(h)
        if len(hashes) == 1:
            errors.append(
                "All screenshots are identical (same hash). Agent is taking the same screenshot "
                "repeatedly without actually fixing anything between iterations."
            )
        elif len(hashes) < min(min_unique_screenshots, len(screenshots)):
            errors.append(
                f"Only {len(hashes)} unique screenshots out of {len(screenshots)}. "
                f"Most iterations produced no visible change."
            )

    # ─── 3. Video analysis freshness ──────────────────────────────────
    # Recordings are per-scenario: .playtest/verify/recording/<scenario>/video-analysis.json
    # (plus legacy flat/older locations). Use the NEWEST analysis for the staleness check.
    pt = game_dir / ".playtest"
    # No-video iterate phase: the fix loop runs verify-agent MODE=iterate (no recording
    # pipeline); video belongs to MODE=full release confirmation. During an iterate
    # round a stale earlier full-pass analysis is EXPECTED—do not demand a rerun,
    # because that would defeat the deferral. Detect via the newest report's `mode`.
    # `verify.skip_video: true` disables the recording pipeline for EVERY pass, so it
    # suppresses the video-staleness demand the same way.
    iterate_round = bool(config_get(config, "verify.skip_video", False))
    for rp in (pt / "verify" / "verify-report.json", pt / "verify-report.json"):
        if rp.exists():
            try:
                rmode = str((json.loads(rp.read_text()).get("mode") or "")).lower()
                iterate_round = iterate_round or rmode in ("iterate", "full-novideo")
            except (ValueError, OSError):
                pass
            break
    analyses = list((pt / "verify" / "recording").glob("*/video-analysis.json"))
    for legacy in [
        "verify/recording/video-analysis.json",
        "verify/video/video-analysis.json",
        "video/video-analysis.json",
        "video-analysis.json",
    ]:
        p = pt / legacy
        if p.exists():
            analyses.append(p)
    if analyses and iteration_count > 0 and not iterate_round:
        analysis_mtime = max(a.stat().st_mtime for a in analyses)
        # Find most recent .js file modification in src/
        src_dir = game_dir / "src"
        if src_dir.exists():
            latest_src_mtime = 0
            for js in src_dir.rglob("*.js"):
                mtime = js.stat().st_mtime
                if mtime > latest_src_mtime:
                    latest_src_mtime = mtime
            if latest_src_mtime > analysis_mtime + stale_tolerance:
                errors.append(
                    "Code was modified AFTER the last video analysis. "
                    "Must call mcp__gca__analyze_video again to validate current state."
                )

    # ─── 4. Recordings must exist if playthrough_recorded ─────────────
    if state.get("playthrough_recorded"):
        recordings_dir = _first_existing(pt, ["verify/recording", "verify/recordings", "recordings"])
        # Recurse: recordings live per-scenario under verify/recording/<scenario>/.
        recordings = (list(recordings_dir.rglob("*.webm")) + list(recordings_dir.rglob("*.mp4"))) if recordings_dir.exists() else []
        if not recordings:
            errors.append(
                "state.json says playthrough_recorded=true but no .webm/.mp4 files exist in .playtest/verify/recording/"
            )

        verify_report = _first_existing(pt, ["verify/verify-report.json", "verify-report.json"])
        if not verify_report.exists():
            errors.append(
                "state.json says playthrough_recorded=true but .playtest/verify/verify-report.json is missing."
            )
        else:
            report_mtime = verify_report.stat().st_mtime
            src_dir = game_dir / "src"
            if src_dir.exists():
                latest_src_mtime = 0
                for js in src_dir.rglob("*.js"):
                    mtime = js.stat().st_mtime
                    if mtime > latest_src_mtime:
                        latest_src_mtime = mtime
                if latest_src_mtime > report_mtime + stale_tolerance:
                    errors.append(
                        "Code was modified AFTER verify-report.json. "
                        "Must re-run verification and mcp__gca__write_verify_report."
                    )

    if errors:
        print("FAIL: State validation failed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("OK: State validation passed")
        sys.exit(0)

if __name__ == "__main__":
    main()
