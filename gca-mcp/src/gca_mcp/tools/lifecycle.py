"""Lifecycle / discovery tools — workspace open + health/preflight checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .. import playtest_layout as layout
from ..adapters import matting_availability
from ..adapters import rmbg2 as rmbg2_adapter
from ..adapters import seedance as seedance_adapter
from ..adapters.script_runner import VERIFY_RUNNERS
from ..errors import ToolError, is_transient_rmbg_error
from ..evidence import (
    atomic_write_json,
    compute_build_digest,
    new_pipeline_run_id,
    pipeline_state_lock,
    read_pipeline_identity,
    recover_state_transaction,
)
from ..server import ToolCtx, ToolDef
from ..verify_epoch import build_mutation_flock
from ..workspace import (
    open_workspace,
    resolve_workspace_root,
    validated_workspace_root,
)

# The 10 gameplay genres + the `custom` engineering fallback. A misspelled
# genre must fail here, not silently normalize downstream (verify's
# normalizeGenre() already returns null on unknown, but the value should never
# reach state.json in the first place).
GENRE_SLUGS = (
    "platform", "beat_em_up", "bullet_hell", "roguelike", "puzzle",
    "tower_defence", "sandbox", "simulation", "card", "idle", "custom",
)
INTERACTION_PROFILES = ("arcade-gravity", "arcade-no-gravity", "matter", "none")
QUALITY_PROFILES = ("art_directed", "responsive", "pixel_art", "minimal")
LEVEL_MODELS = ("authored", "procedural", "board", "none")
AUDIT_OFFSETS_VERSION = 1
COUNTER_MIGRATION_VERSION = 1
COUNTER_MIGRATION_SOURCE = "legacy_unscoped_audit"
_SCAFFOLD_REPROJECT_RECEIPTS = (
    Path(".playtest") / "scaffold" / "reproject-receipts"
)
_SCAFFOLD_REPROJECT_DECLARATION = (
    Path(".playtest") / "inputs" / "scaffold-reproject.json"
)
_STAGE_FILE_RE = re.compile(r"^stage_\d+\.json$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_RELEASE_WAIVER", f"{field} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            "INVALID_RELEASE_WAIVER",
            f"{field} must be an ISO timestamp.",
        ) from exc
    if parsed.tzinfo is None:
        raise ToolError(
            "INVALID_RELEASE_WAIVER",
            f"{field} must include a timezone.",
        )
    return parsed.astimezone(timezone.utc)


def _bind_release_waivers(
    requested: list[dict[str, Any]],
    *,
    existing: dict[str, Any],
    pipeline_run_id: str,
    build_digest: str,
    now: datetime,
) -> list[dict[str, Any]]:
    previous_by_id = {
        waiver.get("id"): waiver
        for waiver in (existing.get("release_waivers") or [])
        if isinstance(waiver, dict) and isinstance(waiver.get("id"), str)
    }
    prior_update = (
        _parse_utc(existing["updated_at"], field="existing.updated_at")
        if existing.get("updated_at")
        else None
    )
    seen: set[str] = set()
    bound: list[dict[str, Any]] = []
    for waiver in requested:
        waiver_id = waiver["id"]
        if waiver_id in seen:
            raise ToolError(
                "INVALID_RELEASE_WAIVER",
                f"Duplicate release waiver id: {waiver_id!r}.",
            )
        seen.add(waiver_id)
        approved_at = _parse_utc(waiver["approved_at"], field="approved_at")
        if approved_at > now.replace(microsecond=0) + timedelta(minutes=5):
            raise ToolError(
                "INVALID_RELEASE_WAIVER",
                "Release waiver approval is future-dated.",
            )
        previous = previous_by_id.get(waiver_id)
        same_approval = previous is not None and all(
            previous.get(key) == waiver.get(key)
            for key in ("id", "reason", "approved_by", "approved_at")
        )
        same_build = (
            previous is not None
            and previous.get("pipeline_run_id") == pipeline_run_id
            and previous.get("build_digest") == build_digest
        )
        if same_approval and same_build:
            bound.append(dict(previous))
            continue
        approval_floor = prior_update or now - timedelta(minutes=15)
        if previous is not None:
            previous_time = _parse_utc(
                previous.get("approved_at"),
                field="previous approved_at",
            )
            approval_floor = max(approval_floor, previous_time)
        if approved_at <= approval_floor:
            raise ToolError(
                "STALE_RELEASE_WAIVER",
                "A new or changed build requires renewed approval after the "
                "previous state update; old waiver objects cannot be rebound.",
            )
        bound.append({
            **waiver,
            "approval_event_id": f"waiver-{uuid.uuid4()}",
            "pipeline_run_id": pipeline_run_id,
            "build_digest": build_digest,
        })
    return bound


def _resolve_game_dir(ctx: ToolCtx, value: Any) -> Path | None:
    active = (
        validated_workspace_root(ctx.state.workspace)
        if ctx.state.workspace is not None
        else None
    )
    if value:
        path = resolve_workspace_root(str(value), repo_root=ctx.repo_root)
        if active is not None and path != active:
            raise ToolError(
                "WORKSPACE_MISMATCH",
                f"game_dir {path} does not match active workspace "
                f"{active}.",
            )
        return path
    return active


def _result(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, **extra}


def _summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    blockers = [
        c for c in checks
        if not c.get("ok") and c.get("severity", "blocker") == "blocker"
    ]
    warnings = [
        c for c in checks
        if not c.get("ok") and c.get("severity") == "warning"
    ]
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }


# Probe the same shared loader used by every bundled browser producer.
def _check_playwright() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["node", str(VERIFY_RUNNERS / "probe-playwright.mjs")],
            cwd=str(VERIFY_RUNNERS),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return _result(
            "playwright",
            False,
            severity="blocker",
            message="`node` not found on PATH — install Node.js 20+.",
        )
    if proc.returncode == 0:
        return _result(
            "playwright",
            True,
            severity="blocker",
            message="Playwright + Chromium importable from runners/verify",
        )
    tail = (proc.stderr or "").strip().splitlines()
    msg = tail[-1][:300] if tail else "unknown error"
    browser_missing = proc.returncode == 2
    return _result(
        "playwright",
        False,
        severity="blocker",
        message=(
            f"Chromium binary missing ({msg}) — run `npx playwright install chromium`."
            if browser_missing
            else f"Playwright not importable from runners/verify — {msg}"
        ),
    )


def _check_ffmpeg() -> dict[str, Any]:
    missing = [cmd for cmd in ("ffmpeg", "ffprobe") if shutil.which(cmd) is None]
    return _result(
        "ffmpeg",
        not missing,
        severity="blocker",
        message="ffmpeg and ffprobe on PATH" if not missing else f"Missing: {', '.join(missing)}",
    )


def _check_file_access(game_dir: Path | None) -> dict[str, Any]:
    if game_dir is None:
        return _result(
            "file_access",
            False,
            severity="warning",
            message="No game_dir/workspace yet; skip filesystem probe.",
        )
    probe = game_dir / ".playtest" / "preflight" / ".fs-probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        text = probe.read_text(encoding="utf-8")
        probe.unlink(missing_ok=True)
        return _result(
            "file_access",
            text == "ok",
            severity="blocker",
            path=str(probe),
            message="read/write/delete probe OK",
        )
    except OSError as e:
        return _result(
            "file_access",
            False,
            severity="blocker",
            path=str(probe),
            message=f"{type(e).__name__}: {e}",
            hint="On macOS, grant Full Disk Access to the app running Claude Code/MCP or move the repo outside Desktop/Documents.",
        )


def _tiny_png(path: Path) -> Path:
    # 32x32: small enough to stay a cheap probe, but above seedance's
    # 14px minimum dimension (an 8px image is rejected with
    # InvalidParameter when submit_seedance=True).
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 32
    img = Image.new("RGBA", (size, size), (255, 0, 255, 255))
    for x in range(size // 4, size * 3 // 4):
        for y in range(size // 4, size * 3 // 4):
            img.putpixel((x, y), (0, 120, 255, 255))
    img.save(path)
    return path


def _probe_rmbg(ctx: ToolCtx, image_path: Path, *, timeout: int = 15) -> dict[str, Any]:
    from ..providers import get_provider

    auto = ctx.config.assets.matting.backend == "auto"
    route = (
        matting_availability.acquire_rmbg_route(
            ctx.config,
            ctx.config.rmbg2.endpoint,
        )
        if auto
        else None
    )
    if route is not None and route.action != "probe":
        if route.action == "rmbg2":
            return _result(
                "rmbg2",
                True,
                severity="blocker",
                endpoint=ctx.config.rmbg2.endpoint,
                probe_cached=True,
                message="RMBG-2.0 availability reused from the process cache.",
            )
        fallback_available = route.action == "color_key"
        return _result(
            "rmbg2",
            False,
            severity="warning" if fallback_available else "blocker",
            endpoint=ctx.config.rmbg2.endpoint,
            matting_backend=ctx.config.assets.matting.backend,
            fallback_available=fallback_available,
            probe_cached=True,
            code=route.code,
            message=route.message or "Cached RMBG probe failed.",
            hint=route.hint,
        )

    try:
        out, meta = get_provider(ctx.config).rmbg_matte(
            ctx.config,
            image_path.read_bytes(),
            endpoint=ctx.config.rmbg2.endpoint,
            bg_hex="#FF00FF",
            aug=None,
            rmbg_fg_cutoff=ctx.config.assets.rmbg.rmbg_fg_cutoff,
            max_size=64,
            timeout=timeout,
        )
        rmbg2_adapter.validate_rmbg_output(out)
        ok = True
        if route is not None:
            if ok:
                matting_availability.remember_rmbg_success(route)
            else:
                matting_availability.remember_rmbg_error(
                    route,
                    ToolError("BAD_RESPONSE", "RMBG-2.0 probe returned no image."),
                )
        return _result(
            "rmbg2",
            ok,
            severity="blocker",
            endpoint=ctx.config.rmbg2.endpoint,
            response_bytes=len(out),
            server_meta=meta,
            message="RMBG-2.0 tiny PNG probe OK",
        )
    except ToolError as e:
        if route is not None:
            matting_availability.remember_rmbg_error(route, e)
        fallback_eligible_auto_failure = (
            auto and is_transient_rmbg_error(e)
        )
        return _result(
            "rmbg2",
            False,
            severity="warning" if fallback_eligible_auto_failure else "blocker",
            endpoint=ctx.config.rmbg2.endpoint,
            matting_backend=ctx.config.assets.matting.backend,
            fallback_available=fallback_eligible_auto_failure,
            code=e.code,
            message=e.message,
            hint=e.hint,
        )
    except Exception:
        if route is not None:
            matting_availability.cancel_rmbg_probe(route)
        raise


def _probe_color_key(
    ctx: ToolCtx,
    image_path: Path,
    *,
    severity: str = "blocker",
) -> dict[str, Any]:
    """Verify the deterministic fallback removes key pixels but keeps content."""
    from ..runners.assets.color_key import remove_bg_color

    matte_cfg = ctx.config.assets.matting
    try:
        with Image.open(image_path) as image:
            matted = remove_bg_color(
                image,
                key_color="#FF00FF",
                threshold=matte_cfg.color_key_threshold,
                edge_threshold=matte_cfg.color_key_edge_threshold,
            )
        alpha_min, alpha_max = matted.getchannel("A").getextrema()
        ok = alpha_min == 0 and alpha_max > 0
        return _result(
            "color_key_matting",
            ok,
            severity=severity,
            matting_backend=matte_cfg.backend,
            key_color="#FF00FF",
            message=(
                "Color-key tiny PNG fallback probe OK"
                if ok
                else "Color-key fallback produced unusable alpha."
            ),
        )
    except (OSError, ValueError) as exc:
        return _result(
            "color_key_matting",
            False,
            severity=severity,
            matting_backend=matte_cfg.backend,
            key_color="#FF00FF",
            code="COLOR_KEY_MATTING_FAILED",
            message=f"Color-key fallback probe failed: {exc}",
        )


def _matting_preflight_checks(
    ctx: ToolCtx,
    image_path: Path,
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    """Probe only the backends selected by the configured matting policy."""
    backend = str(ctx.config.assets.matting.backend)
    if backend == "rmbg2":
        return [_probe_rmbg(ctx, image_path, timeout=timeout)]
    if backend == "color_key":
        return [_probe_color_key(ctx, image_path)]
    if backend != "auto":
        return [
            _result(
                "matting_config",
                False,
                severity="blocker",
                message=f"Unsupported assets.matting.backend: {backend!r}.",
            )
        ]

    rmbg = _probe_rmbg(ctx, image_path, timeout=timeout)
    # If the primary route works, a broken reserve is a warning. If RMBG is
    # unavailable through a safe fallback-eligible failure, the reserve becomes
    # the required route.
    color_key_severity = "warning" if rmbg.get("ok") else "blocker"
    color_key = _probe_color_key(
        ctx,
        image_path,
        severity=color_key_severity,
    )
    return [rmbg, color_key]


def _probe_image_input(ctx: ToolCtx, image_path: Path) -> dict[str, Any]:
    """Confirm that a local Seedance frame can be encoded as a data URI."""
    from ..providers import get_provider

    try:
        url = get_provider(ctx.config).ensure_image_url(ctx.config, str(image_path))
    except ToolError as e:
        return _result(
            "image_input",
            False,
            severity="blocker",
            code=e.code,
            message=e.message,
            hint=e.hint,
        )

    if not url.startswith("data:image/"):
        return _result(
            "image_input",
            False,
            severity="blocker",
            message="Local Seedance frame was not encoded as an image data URI.",
        )
    return _result(
        "image_input",
        True,
        backend="inline",
        message="Seedance first frame can be encoded inline.",
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def _audit(
    game_dir: Path,
    event: str,
    details: str,
    *,
    identity: dict[str, str] | None = None,
) -> None:
    audit_file = game_dir / ".playtest" / "audit.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    identity = identity or read_pipeline_identity(game_dir) or {}
    entry = {
        "timestamp": int(time.time()),
        "time_iso": _utc_now(),
        "event": event,
        "details": details,
        **identity,
    }
    with audit_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


_GUARDED_COUNTER_EVENTS = {
    "verify_runs": "verify_run",
    "iteration_count": "iteration_complete",
}


def _audit_event_counts(
    game_dir: Path,
    pipeline_run_id: str | None = None,
    *,
    unscoped_only: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    audit_file = game_dir / ".playtest" / "audit.jsonl"
    if not audit_file.is_file():
        return counts
    for line in audit_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_run_id = entry.get("pipeline_run_id")
        if unscoped_only and entry_run_id is not None:
            continue
        if pipeline_run_id is not None and entry_run_id != pipeline_run_id:
            continue
        event = entry.get("event")
        if isinstance(event, str):
            counts[event] = counts.get(event, 0) + 1
    return counts


def _counter_value(record: dict[str, Any], key: str) -> int:
    if key in record:
        value = record[key]
    elif key == "iteration_count":
        value = record.get("iterationCount", 0)
    elif key == "verify_runs":
        pipeline = record.get("pipeline")
        verify = pipeline.get("verify") if isinstance(pipeline, dict) else None
        value = verify.get("run_count", 0) if isinstance(verify, dict) else 0
    else:
        value = 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            f"{key} must be a non-negative integer, got {value!r}.",
        )
    return value


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            f"{field} must be a non-negative integer.",
        )
    return value


def _read_audit_offsets(
    record: dict[str, Any],
    *,
    required: bool,
) -> dict[str, int]:
    raw = record.get("audit_offsets")
    if raw is None and not required:
        return {key: 0 for key in _GUARDED_COUNTER_EVENTS}
    if not isinstance(raw, dict):
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "audit_offsets must be an object initialized by write_pipeline_state.",
        )
    missing = [key for key in _GUARDED_COUNTER_EVENTS if key not in raw]
    if missing:
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "audit_offsets is missing guarded counter(s): " + ", ".join(missing) + ".",
        )
    return {
        key: _non_negative_int(raw[key], field=f"audit_offsets.{key}")
        for key in _GUARDED_COUNTER_EVENTS
    }


def _validate_counter_state_contract(state: dict[str, Any]) -> dict[str, int]:
    """Validate the persisted, immutable counter baseline for one pipeline run."""
    offsets_version = state.get("audit_offsets_version")
    if (
        isinstance(offsets_version, bool)
        or offsets_version != AUDIT_OFFSETS_VERSION
    ):
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "state.json is missing the initialized audit_offsets_version; "
            "legacy offsets cannot be reconstructed after a pipeline_run_id exists.",
        )
    offsets = _read_audit_offsets(state, required=True)
    pipeline_run_id = state.get("pipeline_run_id")
    if not isinstance(pipeline_run_id, str) or not pipeline_run_id:
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "Counter evidence requires a non-empty pipeline_run_id.",
        )

    marker = state.get("counter_migration")
    if marker is None:
        if any(offsets.values()):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "Non-zero audit_offsets require a counter_migration snapshot.",
            )
    else:
        if not isinstance(marker, dict):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "counter_migration must be an object.",
            )
        marker_version = marker.get("version")
        if (
            isinstance(marker_version, bool)
            or marker_version != COUNTER_MIGRATION_VERSION
            or marker.get("source") != COUNTER_MIGRATION_SOURCE
            or marker.get("pipeline_run_id") != pipeline_run_id
            or not isinstance(marker.get("migrated_at"), str)
            or not marker["migrated_at"]
        ):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "counter_migration marker is invalid or belongs to another run.",
            )
        snapshot = marker.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "counter_migration.snapshot must be an object.",
            )
        counters = snapshot.get("counters")
        audit_counts = snapshot.get("unscoped_audit_counts")
        tolerated_missing = snapshot.get("tolerated_missing")
        if not all(
            isinstance(value, dict)
            for value in (counters, audit_counts, tolerated_missing)
        ):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "counter_migration.snapshot is missing counter/audit maps.",
            )
        tolerance = _non_negative_int(
            snapshot.get("audit_tolerance"),
            field="counter_migration.snapshot.audit_tolerance",
        )
        for key, event in _GUARDED_COUNTER_EVENTS.items():
            legacy_counter = _non_negative_int(
                counters.get(key),
                field=f"counter_migration.snapshot.counters.{key}",
            )
            legacy_events = _non_negative_int(
                audit_counts.get(event),
                field=f"counter_migration.snapshot.unscoped_audit_counts.{event}",
            )
            missing_events = _non_negative_int(
                tolerated_missing.get(key),
                field=f"counter_migration.snapshot.tolerated_missing.{key}",
            )
            expected_missing = max(0, legacy_counter - legacy_events)
            if (
                offsets[key] != legacy_counter
                or missing_events != expected_missing
                or missing_events > tolerance
            ):
                raise ToolError(
                    "INVALID_STATE_TRANSITION",
                    f"counter_migration snapshot for {key} does not match "
                    "the frozen audit offset.",
                )

    for key, offset in offsets.items():
        if offset > _counter_value(state, key):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"audit_offsets.{key} cannot exceed {key}.",
            )
    return offsets


def _prepare_legacy_counter_migration(
    existing: dict[str, Any],
    args: dict[str, Any],
    audit_counts: dict[str, int],
    *,
    audit_tolerance: int,
    pipeline_run_id: str,
    migrated_at: str,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Freeze an unscoped legacy baseline exactly once."""
    initialized_fields = {
        "audit_offsets_version",
        "audit_offsets",
        "counter_migration",
    }
    if initialized_fields.intersection(existing):
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "Legacy counter evidence is partially initialized; refusing to "
            "reconstruct or expand audit offsets.",
        )

    tolerance = max(0, audit_tolerance)
    counters: dict[str, int] = {}
    snapshot_counts: dict[str, int] = {}
    tolerated_missing: dict[str, int] = {}
    for key, event in _GUARDED_COUNTER_EVENTS.items():
        old_value = _counter_value(existing, key)
        requested = _counter_value(args, key) if key in args else old_value
        if requested != old_value:
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"{key} must remain {old_value} during legacy counter migration; "
                "log a run-scoped event and increment it on a later write.",
            )
        event_count = _non_negative_int(
            audit_counts.get(event, 0),
            field=f"legacy audit count for {event}",
        )
        missing = max(0, old_value - event_count)
        if missing > tolerance:
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"Legacy {key}={old_value} exceeds {event} audit evidence "
                f"({event_count}) by {missing}, above migration tolerance {tolerance}.",
            )
        counters[key] = old_value
        snapshot_counts[event] = event_count
        tolerated_missing[key] = missing

    return counters, {
        "version": COUNTER_MIGRATION_VERSION,
        "source": COUNTER_MIGRATION_SOURCE,
        "pipeline_run_id": pipeline_run_id,
        "migrated_at": migrated_at,
        "snapshot": {
            "counters": counters,
            "unscoped_audit_counts": snapshot_counts,
            "audit_tolerance": tolerance,
            "tolerated_missing": tolerated_missing,
        },
    }


def _validate_counter_claims(
    record: dict[str, Any],
    audit_counts: dict[str, int],
    audit_offsets: dict[str, int],
) -> None:
    for key, event in _GUARDED_COUNTER_EVENTS.items():
        value = _counter_value(record, key)
        offset = _non_negative_int(
            audit_offsets.get(key),
            field=f"audit_offsets.{key}",
        )
        required_event_count = max(0, value - offset)
        event_count = audit_counts.get(event, 0)
        if event_count < required_event_count:
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"{key}={value} requires at least {required_event_count} "
                f"run-scoped `{event}` audit event(s), found {event_count}. "
                "Log canonical evidence first.",
            )


def _validate_state_transition(
    existing: dict[str, Any],
    args: dict[str, Any],
    audit_counts: dict[str, int],
    *,
    max_increment: int = 1,
    max_iterations: int | None = None,
    audit_offsets: dict[str, int] | None = None,
) -> None:
    for key in _GUARDED_COUNTER_EVENTS:
        old_value = _counter_value(existing, key)
        new_value = _counter_value(args, key) if key in args else old_value
        if new_value < old_value or new_value > old_value + max_increment:
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"{key} must stay unchanged or increment by at most {max_increment} "
                f"(existing={old_value}, requested={new_value}).",
            )
        if (
            key == "iteration_count"
            and max_iterations is not None
            and new_value > old_value
            and new_value > max_iterations
        ):
            raise ToolError(
                "ITERATION_BUDGET_EXHAUSTED",
                f"iteration_count cannot advance beyond pipeline.max_iterations="
                f"{max_iterations} (existing={old_value}, requested={new_value}).",
                "Report the remaining gate and critic blockers as unresolved; "
                "do not dispatch another automatic iteration.",
            )
    requested_state = {
        **existing,
        **{
            key: _counter_value(args, key)
            for key in _GUARDED_COUNTER_EVENTS
            if key in args
        },
    }
    _validate_counter_claims(
        requested_state,
        audit_counts,
        audit_offsets or {key: 0 for key in _GUARDED_COUNTER_EVENTS},
    )

    assets_generated = args.get(
        "assets_generated",
        existing.get("assets_generated", False),
    )
    if assets_generated and audit_counts.get("assets_generated", 0) < 1:
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "assets_generated=true requires an `assets_generated` audit event.",
        )


# -------------------- ping --------------------


def _credentials_present(ctx: ToolCtx) -> bool:
    import os

    return bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("ARK_API_KEY"))


def _credentials_check(ctx: ToolCtx) -> dict[str, Any]:
    import os

    return _result(
        "credentials",
        bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("ARK_API_KEY")),
        severity="blocker",
        provider="external",
        message="Provider credentials (OPENAI_API_KEY + ARK_API_KEY) present",
    )


def _storage_check(ctx: ToolCtx) -> dict[str, Any]:
    return _result(
        "storage_config",
        True,
        severity="info",
        provider="external",
        message="Object storage is not required; local frames are encoded inline.",
    )


def _seedance_credentials_check(ctx: ToolCtx) -> dict[str, Any]:
    import os

    return _result(
        "seedance_credentials",
        bool(os.environ.get("ARK_API_KEY")),
        severity="blocker",
        provider="ark",
        message="Seedance credentials (ARK_API_KEY) present",
    )


def _ping(_args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Health check — agents call this once after spawn to confirm the server is up."""
    info: dict[str, Any] = {
        "ok": True,
        "workspace_open": ctx.state.workspace_open,
        "repo_root": str(ctx.repo_root),
        "provider": "external",
        "credentials_present": _credentials_present(ctx),
        "frame_transport": "inline",
    }
    if ctx.state.workspace is not None:
        info["workspace"] = ctx.state.workspace.to_dict()
    else:
        info["hint"] = "Call init_workspace(path=…, mode='create'|'resume')."
    return info


# -------------------- init_workspace --------------------


def _init_workspace(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    path = args.get("path")
    name = args.get("name")
    mode = args.get("mode")
    if not path:
        raise ToolError(
            "INVALID_PATH",
            "init_workspace requires a non-empty `path` argument.",
        )

    if mode not in {"create", "resume"}:
        raise ToolError(
            "INVALID_CONFIG",
            "init_workspace requires mode='create' or mode='resume'.",
        )

    state = ctx.state
    requested_root = resolve_workspace_root(path, repo_root=ctx.repo_root)
    with state.workspace_mutation():
        # Only the exact same path + mode binding is idempotent.
        if state.workspace is not None:
            existing = state.workspace
            if requested_root == existing.root:
                if mode != existing.mode:
                    raise ToolError(
                        "WORKSPACE_ALREADY_OPEN",
                        f"Workspace {existing.root} is already bound in "
                        f"mode={existing.mode!r}; requested mode={mode!r}.",
                        hint="Close the workspace before changing lifecycle intent.",
                    )
                return {
                    "id": existing.name,
                    "mode": existing.mode,
                    "workspace": existing.to_dict(),
                    "reopened": True,
                }
            raise ToolError(
                "WORKSPACE_ALREADY_OPEN",
                f"Workspace already open at {existing.root}; restart to switch.",
            )

        meta = open_workspace(
            requested_root,
            repo_root=ctx.repo_root,
            mode=mode,
            name=name,
        )
        state.workspace = meta
        return {
            "id": meta.name,
            "mode": meta.mode,
            "workspace": meta.to_dict(),
            "reopened": False,
        }


# -------------------- close_workspace --------------------


def _close_workspace(_args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    workspace = ctx.state.close_current_workspace()
    return {"closed": workspace.to_dict() if workspace is not None else None}


# -------------------- list_tools_summary --------------------


def _list_tools_summary(_args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Return a name → description map of every registered tool.

    Useful when the agent wants a discoverable index without paging the
    full schema.
    """
    from ..server import build_registry

    registry = build_registry()
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description.split("\n", 1)[0],
                "requires_workspace": t.requires_workspace,
            }
            for t in registry.values()
        ]
    }


# -------------------- preflight_check --------------------


def _preflight_check(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = _resolve_game_dir(ctx, args.get("game_dir"))
    checks: list[dict[str, Any]] = [
        _result("mcp", True, message="MCP tool call succeeded"),
        _credentials_check(ctx),
        _storage_check(ctx),
        _check_file_access(game_dir),
        _check_playwright(),
        _check_ffmpeg(),
    ]

    if args.get("check_rmbg", True):
        smoke_dir = (game_dir or ctx.repo_root) / ".playtest" / "preflight"
        tiny = _tiny_png(smoke_dir / "rmbg-smoke.png")
        checks.extend(
            _matting_preflight_checks(
                ctx,
                tiny,
                timeout=int(args.get("timeout", 15)),
            )
        )

    report = {
        "generated_at": _utc_now(),
        "game_dir": str(game_dir) if game_dir else None,
        **_summarize(checks),
    }
    if game_dir:
        out = game_dir / ".playtest" / layout.PREFLIGHT_REPORT
        _write_json(out, report)
        _audit(game_dir, "preflight_check", "ok" if report["ok"] else "blocked")
        report["report_path"] = str(out)
    return report


# -------------------- asset_pipeline_smoke_test --------------------


def _asset_pipeline_smoke_test(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = _resolve_game_dir(ctx, args.get("game_dir"))
    if game_dir is None:
        raise ToolError(
            "NO_WORKSPACE_OPEN",
            "asset_pipeline_smoke_test requires game_dir or an open workspace.",
        )
    # Asset-pipeline smoke shares the pre-pipeline `preflight/` folder.
    smoke_dir = game_dir / ".playtest" / layout.ASSET_SMOKE_DIR
    tiny = _tiny_png(smoke_dir / "seedance-reference.png")

    submit_seedance = bool(args.get("submit_seedance", False))
    image_input = _probe_image_input(ctx, tiny)

    checks: list[dict[str, Any]] = [image_input]
    checks.extend(
        _matting_preflight_checks(
            ctx,
            tiny,
            timeout=int(args.get("rmbg_timeout", 15)),
        )
    )
    checks.append(_seedance_credentials_check(ctx))

    if submit_seedance:
        if image_input.get("ok"):
            try:
                result = seedance_adapter.generate_video(
                    ctx.config,
                    prompt="Asset pipeline smoke test: keep this tiny blue square static on a solid magenta background.",
                    output=smoke_dir / "seedance-smoke.mp4",
                    image=[str(tiny)],
                    duration=int(args.get("duration", 5)),
                    ratio="1:1",
                    resolution=str(args.get("resolution", "720p")),
                    print_url_only=True,
                    server_timeout=int(args.get("seedance_timeout", 600)),
                    download_timeout=60,
                )
                checks.append(_result("seedance_submit", True, **result))
            except ToolError as e:
                checks.append(_result(
                    "seedance_submit",
                    False,
                    severity="blocker",
                    code=e.code,
                    message=e.message,
                    hint=e.hint,
                ))
        else:
            checks.append(_result(
                "seedance_submit",
                False,
                severity="blocker",
                message="Skipped because inline image preparation failed.",
            ))

    report = {
        "generated_at": _utc_now(),
        "game_dir": str(game_dir),
        "seedance_submitted": bool(args.get("submit_seedance", False)),
        **_summarize(checks),
    }
    out = smoke_dir / "asset-smoke-report.json"
    _write_json(out, report)
    _audit(game_dir, "asset_smoke_test", "ok" if report["ok"] else "blocked")
    report["report_path"] = str(out)
    return report


# -------------------- write_pipeline_state --------------------


def _validate_level_files(
    requested: Any,
    *,
    game_dir: Path,
) -> list[str]:
    if not isinstance(requested, list) or not requested:
        raise ToolError(
            "INVALID_LEVEL_FILES",
            "level_files must be a non-empty array for authored level data.",
        )

    levels_dir = game_dir / "levels"
    if levels_dir.is_symlink():
        raise ToolError(
            "INVALID_LEVEL_FILES",
            f"Authored levels directory must not be a symlink: {levels_dir}.",
        )
    try:
        resolved_game_dir = game_dir.resolve(strict=True)
        resolved_levels_dir = levels_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolError(
            "INVALID_LEVEL_FILES",
            f"Authored levels directory is missing or unreadable: {levels_dir}.",
        ) from exc
    if not resolved_levels_dir.is_dir():
        raise ToolError(
            "INVALID_LEVEL_FILES",
            f"Authored levels path is not a directory: {levels_dir}.",
        )
    if resolved_levels_dir.parent != resolved_game_dir:
        raise ToolError(
            "INVALID_LEVEL_FILES",
            f"Authored levels directory escapes the game workspace: {levels_dir}.",
        )

    validated: list[str] = []
    validated_resolved: list[Path] = []
    resolved_seen: set[Path] = set()
    for index, value in enumerate(requested):
        if not isinstance(value, str) or not value:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must be a non-empty absolute path.",
            )
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must be absolute: {value!r}.",
            )
        if not _STAGE_FILE_RE.fullmatch(candidate.name):
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must name stage_<number>.json: {value!r}.",
            )
        if candidate.parent != levels_dir:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must be directly under {levels_dir}: "
                f"{value!r}.",
            )
        if candidate.is_symlink():
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must not be a symlink: {value!r}.",
            )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] is missing or unreadable: {value!r}.",
            ) from exc
        if not resolved.is_file():
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] is not a regular file: {value!r}.",
            )
        if resolved.parent != resolved_levels_dir:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files[{index}] must be directly under {levels_dir}: "
                f"{value!r}.",
            )
        if resolved in resolved_seen:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"level_files contains a duplicate path: {value!r}.",
            )
        resolved_seen.add(resolved)
        validated.append(str(resolved))
        validated_resolved.append(resolved)

    discovered_paths = []
    for path in levels_dir.iterdir():
        if not _STAGE_FILE_RE.fullmatch(path.name):
            continue
        if path.is_symlink():
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"Authored stage inventory contains a symlink: {path}.",
            )
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"Authored stage inventory contains an unreadable entry: {path}.",
            ) from exc
        if not resolved.is_file() or resolved.parent != resolved_levels_dir:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                f"Authored stage inventory contains an invalid entry: {path}.",
            )
        discovered_paths.append(resolved)
    discovered = set(discovered_paths)
    if len(discovered) != len(discovered_paths):
        raise ToolError(
            "INVALID_LEVEL_FILES",
            "Authored stage inventory contains paths resolving to the same file.",
        )
    if resolved_seen != discovered:
        missing = sorted(str(path) for path in discovered - resolved_seen)
        extra = sorted(str(path) for path in resolved_seen - discovered)
        details = []
        if missing:
            details.append(f"missing current stages: {missing}")
        if extra:
            details.append(f"unexpected stages: {extra}")
        raise ToolError(
            "INVALID_LEVEL_FILES",
            "level_files must be the complete current authored stage inventory"
            + (f" ({'; '.join(details)})" if details else "")
            + ".",
        )
    expected_order = sorted(
        discovered_paths,
        key=lambda path: (int(path.stem.removeprefix("stage_")), path.name),
    )
    if validated_resolved != expected_order:
        raise ToolError(
            "INVALID_LEVEL_FILES",
            "level_files must be ordered by authored stage progression.",
        )
    return validated


def _write_pipeline_state(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = _resolve_game_dir(ctx, args.get("game_dir"))
    if game_dir is None:
        raise ToolError(
            "NO_WORKSPACE_OPEN",
            "write_pipeline_state requires game_dir or an open workspace.",
        )
    playtest_dir = game_dir / ".playtest"
    with pipeline_state_lock(playtest_dir):
        return _write_pipeline_state_locked(args, ctx, game_dir)


def _write_pipeline_state_locked(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    genre = args.get("genre")
    if genre is not None and genre not in GENRE_SLUGS:
        raise ToolError(
            "INVALID_GENRE",
            f"genre={genre!r} is not a valid slug. Expected one of: "
            f"{', '.join(GENRE_SLUGS)}.",
        )
    interaction_profile = args.get("interaction_profile")
    if interaction_profile is not None and interaction_profile not in INTERACTION_PROFILES:
        raise ToolError(
            "INVALID_INTERACTION_PROFILE",
            f"interaction_profile={interaction_profile!r} is invalid. Expected one of: "
            f"{', '.join(INTERACTION_PROFILES)}.",
        )
    quality_profile = args.get("quality_profile")
    if quality_profile is not None and quality_profile not in QUALITY_PROFILES:
        raise ToolError(
            "INVALID_QUALITY_PROFILE",
            f"quality_profile={quality_profile!r} is invalid. Expected one of: "
            f"{', '.join(QUALITY_PROFILES)}.",
        )
    level_model = args.get("level_model")
    if level_model is not None and level_model not in LEVEL_MODELS:
        raise ToolError(
            "INVALID_LEVEL_MODEL",
            f"level_model={level_model!r} is invalid. Expected one of: "
            f"{', '.join(LEVEL_MODELS)}.",
        )
    playtest_dir = game_dir / ".playtest"
    recover_state_transaction(playtest_dir)
    state_path = playtest_dir / "state.json"
    resume_path = playtest_dir / "resume.json"

    existing: dict[str, Any] = {}
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                f"Existing state.json is unreadable or malformed: {exc}",
            ) from exc
        if not isinstance(existing, dict):
            raise ToolError(
                "INVALID_STATE_TRANSITION",
                "Existing state.json must contain a JSON object.",
            )

    routing_fields = (
        "genre",
        "interaction_profile",
        "quality_profile",
        "level_model",
    )
    if not existing:
        missing_routing = [key for key in routing_fields if args.get(key) is None]
        if missing_routing:
            raise ToolError(
                "MISSING_ROUTING_METADATA",
                "The first pipeline state write requires "
                + ", ".join(missing_routing)
                + ".",
            )
    elif any(existing.get(key) is None for key in routing_fields):
        missing_routing = [key for key in routing_fields if args.get(key) is None]
        if missing_routing:
            raise ToolError(
                "MISSING_ROUTING_METADATA",
                "Legacy state routing migration requires explicit "
                "genre, interaction_profile, quality_profile, and level_model; missing "
                + ", ".join(missing_routing)
                + ". Legacy booleans and files are not routing authorities.",
            )

    for key, requested in (
        ("genre", genre),
        ("interaction_profile", interaction_profile),
        ("quality_profile", quality_profile),
        ("level_model", level_model),
    ):
        current = existing.get(key)
        if current is not None and requested is not None and requested != current:
            raise ToolError(
                "ROUTING_METADATA_IMMUTABLE",
                f"{key} cannot change within one pipeline run "
                f"({current!r} -> {requested!r}). Start a new run to reproject contracts.",
            )
    if (
        existing.get("design_axes") is not None
        and args.get("design_axes") is not None
        and args["design_axes"] != existing["design_axes"]
    ):
        raise ToolError(
            "ROUTING_METADATA_IMMUTABLE",
            "design_axes cannot change within one pipeline run. "
            "Start a new run to reproject contracts.",
        )

    effective_genre = genre or existing.get("genre")
    effective_interaction_profile = (
        interaction_profile or existing.get("interaction_profile")
    )
    effective_quality_profile = quality_profile or existing.get("quality_profile")
    effective_level_model = level_model or existing.get("level_model")
    if effective_genre not in GENRE_SLUGS:
        raise ToolError(
            "INVALID_GENRE",
            "state.json must resolve to an explicit valid genre.",
        )
    if effective_interaction_profile not in INTERACTION_PROFILES:
        raise ToolError(
            "INVALID_INTERACTION_PROFILE",
            "state.json must resolve to an explicit valid interaction_profile.",
        )
    if effective_quality_profile not in QUALITY_PROFILES:
        raise ToolError(
            "INVALID_QUALITY_PROFILE",
            "state.json must resolve to an explicit valid quality_profile.",
        )
    if effective_level_model not in LEVEL_MODELS:
        raise ToolError(
            "INVALID_LEVEL_MODEL",
            "state.json must resolve to an explicit valid level_model.",
        )
    effective_design_axes = args.get("design_axes", existing.get("design_axes"))
    if (
        isinstance(effective_design_axes, dict)
        and effective_design_axes.get("physics") is not None
        and effective_design_axes.get("physics") != effective_interaction_profile
    ):
        raise ToolError(
            "ROUTING_METADATA_MISMATCH",
            "interaction_profile must equal design_axes.physics.",
        )
    if (
        "level_design_required" in args
        and args["level_design_required"] != (effective_level_model == "authored")
    ):
        raise ToolError(
            "INVALID_LEVEL_MODEL",
            "level_design_required must equal (level_model == 'authored').",
        )
    if (
        effective_level_model != "authored"
        and args.get("has_levels") is True
    ):
        raise ToolError(
            "INVALID_LEVEL_MODEL",
            "has_levels=true is valid only for level_model='authored'.",
        )
    if effective_level_model != "authored" and "level_files" in args:
        raise ToolError(
            "INVALID_LEVEL_FILES",
            "level_files is valid only for level_model='authored'.",
        )
    if (
        effective_level_model == "authored"
        and existing.get("has_levels") is True
        and args.get("has_levels") is False
    ):
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "authored has_levels cannot change from true to false within one "
            "pipeline run.",
        )

    effective_has_levels = args.get("has_levels", existing.get("has_levels"))
    validated_level_files: list[str] | None = None
    if effective_level_model == "authored":
        if "level_files" in existing and effective_has_levels is not True:
            raise ToolError(
                "INVALID_LEVEL_FILES",
                "authored level_files requires has_levels=true.",
            )
        if "level_files" in args:
            if effective_has_levels is not True:
                raise ToolError(
                    "INVALID_LEVEL_FILES",
                    "level_files requires has_levels=true in the same or "
                    "existing authored state.",
                )
            validated_level_files = _validate_level_files(
                args["level_files"],
                game_dir=game_dir,
            )
        elif effective_has_levels is True:
            if "level_files" not in existing:
                raise ToolError(
                    "MISSING_LEVEL_FILES",
                    "Legacy authored state has has_levels=true but no "
                    "level_files inventory. Revalidate the Level Design return "
                    "and call write_pipeline_state with the complete current "
                    "level_files array before resuming downstream agents.",
                )
            validated_level_files = _validate_level_files(
                existing["level_files"],
                game_dir=game_dir,
            )

    existing_run_id = existing.get("pipeline_run_id")
    if existing_run_id is not None and (
        not isinstance(existing_run_id, str) or not existing_run_id
    ):
        raise ToolError(
            "INVALID_STATE_TRANSITION",
            "Existing pipeline_run_id must be a non-empty string.",
        )
    pipeline_run_id = existing_run_id or new_pipeline_run_id()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat().replace("+00:00", "Z")
    counter_migration: dict[str, Any] | None = None
    if existing and existing_run_id is None:
        # Legacy rows had no run identity. Validate their historical shortfall
        # once, then freeze the entire legacy counter as the new run's baseline.
        audit_counts = _audit_event_counts(game_dir, unscoped_only=True)
        audit_offsets, counter_migration = _prepare_legacy_counter_migration(
            existing,
            args,
            audit_counts,
            audit_tolerance=max(0, int(ctx.config.pipeline.audit_tolerance)),
            pipeline_run_id=pipeline_run_id,
            migrated_at=now,
        )
    elif existing:
        audit_offsets = _validate_counter_state_contract(existing)
        audit_counts = _audit_event_counts(game_dir, pipeline_run_id)
        marker = existing.get("counter_migration")
        counter_migration = marker if isinstance(marker, dict) else None
    else:
        audit_offsets = {key: 0 for key in _GUARDED_COUNTER_EVENTS}
        audit_counts = _audit_event_counts(game_dir, pipeline_run_id)
    max_iterations = int(ctx.config.pipeline.max_iterations)
    _validate_state_transition(
        existing,
        args,
        audit_counts,
        max_increment=max(0, int(ctx.config.pipeline.max_counter_increment)),
        max_iterations=max_iterations,
        audit_offsets=audit_offsets,
    )

    transaction_id = f"state-{uuid.uuid4()}"
    build_digest = compute_build_digest(
        game_dir,
        ctx.repo_root / "gca.config.yaml",
    )
    phase = str(args.get("phase") or args.get("current_step") or "unknown")
    status = str(args.get("status") or "in_progress")
    completed = list(existing.get("completed_phases") or [])
    if status == "completed" and phase not in completed:
        completed.append(phase)

    state = {
        **existing,
        "game_dir": str(game_dir),
        "pipeline_run_id": pipeline_run_id,
        "build_digest": build_digest,
        "state_transaction_id": transaction_id,
        "currentStep": args.get("current_step", phase),
        "phase": phase,
        "phase_status": status,
        "completed_phases": completed,
        "updated_at": now,
    }
    for key in _GUARDED_COUNTER_EVENTS:
        state[key] = (
            _counter_value(args, key)
            if key in args
            else _counter_value(existing, key)
        )
    iteration_count = state["iteration_count"]
    iteration_budget = {
        "max_iterations": max_iterations,
        "used_iterations": iteration_count,
        "remaining_iterations": max(0, max_iterations - iteration_count),
        "exhausted": iteration_count >= max_iterations,
    }
    state["iteration_budget"] = iteration_budget
    state["audit_offsets_version"] = AUDIT_OFFSETS_VERSION
    state["audit_offsets"] = dict(audit_offsets)
    if counter_migration is not None:
        state["counter_migration"] = counter_migration
    for key in (
        "genre", "design_axes", "level_design_required", "interaction_profile",
        "quality_profile", "level_model", "physics",
        "has_levels", "scenes",
        "assets_generated", "visual_verified", "playthrough_recorded",
        "first_pass_complete", "tests", "known_issues", "design_debt",
        "verify_runs", "iteration_count",
    ):
        if key in args:
            state[key] = args[key]
    if "release_waivers" in args:
        state["release_waivers"] = _bind_release_waivers(
            args["release_waivers"],
            existing=existing,
            pipeline_run_id=pipeline_run_id,
            build_digest=build_digest,
            now=now_dt,
        )
    state["level_design_required"] = effective_level_model == "authored"
    if effective_level_model == "authored" and effective_has_levels is True:
        state["level_files"] = validated_level_files
    else:
        state.pop("level_files", None)
        if effective_level_model != "authored" and state.get("has_levels") is True:
            state["has_levels"] = False

    resume = {
        "generated_at": now,
        "game_dir": str(game_dir),
        "pipeline_run_id": pipeline_run_id,
        "build_digest": build_digest,
        "state_transaction_id": transaction_id,
        "phase": phase,
        "status": status,
        "genre": state.get("genre"),
        "interaction_profile": state.get("interaction_profile"),
        "quality_profile": state.get("quality_profile"),
        "level_model": state.get("level_model"),
        "next_action": args.get("next_action"),
        "completed_phases": completed,
        "details": args.get("details"),
        "iteration_budget": iteration_budget,
    }
    if "level_files" in state:
        resume["level_files"] = state["level_files"]

    transaction_path = playtest_dir / "state-transaction.pending.json"
    _write_json(
        transaction_path,
        {
            "schema_version": 1,
            "state_transaction_id": transaction_id,
            "state": state,
            "resume": resume,
        },
    )
    _write_json(state_path, state)
    _write_json(resume_path, resume)
    transaction_path.unlink()
    _audit(
        game_dir,
        "pipeline_state",
        f"{phase}:{status}",
        identity={
            "pipeline_run_id": pipeline_run_id,
            "build_digest": build_digest,
        },
    )
    return {
        "state_path": str(state_path),
        "resume_path": str(resume_path),
        "pipeline_run_id": pipeline_run_id,
        "build_digest": build_digest,
        "state_transaction_id": transaction_id,
        "phase": phase,
        "status": status,
        "iteration_budget": iteration_budget,
    }


def _validate_stale_path_list(paths: Any) -> list[str]:
    if (
        not isinstance(paths, list)
        or not paths
        or len(paths) > 64
        or any(not isinstance(item, str) or not item for item in paths)
        or len(set(paths)) != len(paths)
    ):
        raise ToolError(
            "INVALID_CONFIG",
            "`paths` must be a non-empty unique array of at most 64 relative paths.",
        )
    return paths


def _scaffold_stale_target(root: Path, value: str) -> tuple[str, Path]:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or raw == Path("."):
        raise ToolError("INVALID_PATH", f"Stale path must be relative: {value}.")
    relative = Path(*raw.parts)
    parts = relative.parts
    if not (
        (len(parts) >= 3 and parts[:2] == ("src", "scenes"))
        or (len(parts) >= 3 and parts[:2] == (".playtest", "tests"))
        or relative.as_posix()
        in {
            "src/utils/particle-recipes.js",
            "src/utils/character-controller.js",
        }
    ):
        raise ToolError(
            "INVALID_PATH",
            f"Path is not a deletable Scaffold-owned file: {value}.",
        )
    target = root / relative
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        if stat_module.S_ISLNK(metadata.st_mode):
            raise ToolError(
                "INVALID_PATH",
                f"Stale path may not traverse a symlink: {relative.as_posix()}.",
            )
    if target.resolve(strict=False) != target:
        raise ToolError(
            "INVALID_PATH",
            f"Stale path is not canonical: {relative.as_posix()}.",
        )
    return relative.as_posix(), target


def _stale_file_fingerprint(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"exists": False}
    metadata = os.lstat(target)
    if not stat_module.S_ISREG(metadata.st_mode):
        raise ToolError(
            "INVALID_PATH",
            f"Stale path must name a regular file: {target}.",
        )
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "exists": True,
        "sha256": digest,
        "size": metadata.st_size,
        "st_dev": metadata.st_dev,
        "st_ino": metadata.st_ino,
    }


def _seal_scaffold_reproject(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Seal Scaffold's exact stale list against the current build."""
    workspace = getattr(ctx.state, "workspace", None)
    if workspace is None:
        raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
    if args:
        raise ToolError(
            "INVALID_CONFIG",
            "seal_scaffold_reproject takes no caller-supplied paths.",
        )
    root = validated_workspace_root(workspace)
    with build_mutation_flock(root):
        reader = getattr(workspace, "read_bytes_from_pinned", None)
        creator = getattr(workspace, "create_bytes_in_pinned", None)
        if not callable(reader) or not callable(creator):
            raise ToolError(
                "INVALID_WORKSPACE",
                "Workspace does not provide pinned declaration authority.",
            )
        try:
            declaration_bytes = reader(_SCAFFOLD_REPROJECT_DECLARATION)
            declaration = json.loads(declaration_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_SCAFFOLD_DECLARATION",
                "Scaffold reproject declaration is missing or malformed.",
            ) from exc
        if (
            not isinstance(declaration, dict)
            or declaration.get("schema_version") != 1
            or declaration.get("producer") != "scaffold-agent"
            or declaration.get("mode") != "reproject"
            or not isinstance(declaration.get("reproject_id"), str)
            or re.fullmatch(
                r"reproject-[a-f0-9]{32}",
                declaration["reproject_id"],
            )
            is None
        ):
            raise ToolError(
                "INVALID_SCAFFOLD_DECLARATION",
                "Scaffold reproject declaration has an invalid authority envelope.",
            )
        paths = _validate_stale_path_list(
            declaration.get("stale_owned_paths"),
        )
        entries = []
        for value in paths:
            relative, target = _scaffold_stale_target(root, value)
            entries.append(
                {
                    "path": relative,
                    **_stale_file_fingerprint(target),
                }
            )
        identity = read_pipeline_identity(root)
        if identity is None:
            raise ToolError(
                "MISSING_RUN_IDENTITY",
                "Scaffold reproject sealing requires initialized pipeline state.",
            )
        receipt_id = declaration["reproject_id"]
        receipt_relative = _SCAFFOLD_REPROJECT_RECEIPTS / f"{receipt_id}.json"
        receipt_path = root / receipt_relative
        receipt = {
            "schema_version": 1,
            "producer": "gca-mcp.seal-scaffold-reproject",
            "receipt_id": receipt_id,
            "pipeline_run_id": identity["pipeline_run_id"],
            "pre_delete_build_digest": compute_build_digest(root),
            "declaration_sha256": hashlib.sha256(declaration_bytes).hexdigest(),
            "stale_owned_paths": entries,
            "consumed": False,
            "sealed_at": _utc_now(),
        }
        creator(
            receipt_relative,
            (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            ),
        )
    return {
        "receipt_id": receipt_id,
        "receipt_path": str(receipt_path),
        "stale_owned_paths": paths,
    }


def _delete_scaffold_owned_paths(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Consume one identity- and build-bound Scaffold stale-list receipt."""
    workspace = getattr(ctx.state, "workspace", None)
    if workspace is None:
        raise ToolError("NO_WORKSPACE_OPEN", "No active workspace.")
    receipt_id = args.get("receipt_id")
    if not isinstance(receipt_id, str) or re.fullmatch(
        r"reproject-[a-f0-9]{32}",
        receipt_id,
    ) is None:
        raise ToolError("INVALID_CONFIG", "`receipt_id` is invalid.")
    root = validated_workspace_root(workspace)
    receipt_relative = _SCAFFOLD_REPROJECT_RECEIPTS / f"{receipt_id}.json"
    deleted: list[str] = []
    missing: list[str] = []
    with build_mutation_flock(root):
        reader = getattr(workspace, "read_bytes_from_pinned", None)
        replacer = getattr(workspace, "replace_bytes_in_pinned", None)
        if not callable(reader) or not callable(replacer):
            raise ToolError(
                "INVALID_WORKSPACE",
                "Workspace does not provide pinned receipt authority.",
            )
        try:
            receipt = json.loads(reader(receipt_relative))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_RECEIPT",
                f"Scaffold reproject receipt is missing or malformed: {receipt_id}.",
            ) from exc
        identity = read_pipeline_identity(root)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("producer") != "gca-mcp.seal-scaffold-reproject"
            or receipt.get("receipt_id") != receipt_id
            or receipt.get("consumed") is not False
            or identity is None
            or receipt.get("pipeline_run_id") != identity["pipeline_run_id"]
        ):
            raise ToolError(
                "INVALID_RECEIPT",
                "Scaffold reproject receipt is stale, consumed, or cross-run.",
            )
        if compute_build_digest(root) != receipt.get("pre_delete_build_digest"):
            raise ToolError(
                "STALE_RECEIPT",
                "Build inputs changed after the Scaffold stale list was sealed.",
            )
        entries = receipt.get("stale_owned_paths")
        if not isinstance(entries, list) or not entries:
            raise ToolError("INVALID_RECEIPT", "Receipt has no stale-owned entries.")

        unlinker = getattr(workspace, "unlink_from_pinned", None)
        if not callable(unlinker):
            raise ToolError(
                "INVALID_WORKSPACE",
                "Workspace does not provide pinned unlink authority.",
            )
        targets: list[tuple[str, Path, dict[str, Any]]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ToolError(
                    "INVALID_RECEIPT",
                    "Receipt contains an invalid stale-owned entry.",
                )
            relative, target = _scaffold_stale_target(root, entry["path"])
            current = _stale_file_fingerprint(target)
            if current != {key: value for key, value in entry.items() if key != "path"}:
                raise ToolError(
                    "STALE_RECEIPT",
                    f"Stale-owned file changed after sealing: {relative}.",
                )
            expected = {key: value for key, value in entry.items() if key != "path"}
            targets.append((relative, target, expected))

        for relative, _target, expected in targets:
            if expected.get("exists") is False:
                missing.append(relative)
                continue
            if unlinker(Path(relative), expected):
                deleted.append(relative)
            else:
                missing.append(relative)
        receipt["consumed"] = True
        receipt["consumed_at"] = _utc_now()
        receipt["deleted"] = deleted
        receipt["missing"] = missing
        receipt["post_delete_build_digest"] = compute_build_digest(root)
        replacer(
            receipt_relative,
            (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            ),
        )
    return {"deleted": deleted, "missing": missing}


TOOLS = [
    ToolDef(
        name="ping",
        description=(
            "Health check. Returns workspace_open + credential presence "
            "so the agent can confirm the server is reachable before "
            "issuing real work."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_ping,
        requires_workspace=False,
    ),
    ToolDef(
        name="init_workspace",
        description=(
            "Open one workspace at `path` with explicit create/resume intent. "
            "Pins the workspace inode and acquires an exclusive repository-wide "
            "cross-process "
            "lease until close/exit, returns the bound mode, and permits idempotent "
            "reopen only for the same path and mode. "
            "Create rejects existing game content; resume recovers a valid pending "
            "state transaction before requiring state.json. The workspace is the "
            "root of one game (typically `playground/<game-slug>/`). "
            "Tools that take an implicit `game_dir` will read it from the "
            "open workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or repo-relative path to the game directory.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional friendly name; defaults to folder basename.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["create", "resume"],
                    "description": "Required clean-create or explicit resume intent.",
                },
            },
            "required": ["path", "mode"],
            "additionalProperties": False,
        },
        handler=_init_workspace,
        requires_workspace=False,
    ),
    ToolDef(
        name="close_workspace",
        description=(
            "Detach the current workspace and release its exclusive process lease; "
            "subsequent gated tools error until init_workspace runs again."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_close_workspace,
        requires_workspace=False,
    ),
    ToolDef(
        name="list_tools_summary",
        description="Return one-line summaries for every registered MCP tool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_tools_summary,
        requires_workspace=False,
    ),
    ToolDef(
        name="preflight_check",
        description=(
            "Startup preflight: verifies MCP reachability, provider credentials, "
            "file access, Playwright, ffmpeg/ffprobe, and the "
            "RMBG endpoint. Writes .playtest/preflight/preflight-report.json when "
            "game_dir or workspace is available."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "check_rmbg": {"type": "boolean", "default": True},
                "timeout": {"type": "integer", "default": 15},
            },
            "additionalProperties": False,
        },
        handler=_preflight_check,
        requires_workspace=False,
    ),
    ToolDef(
        name="asset_pipeline_smoke_test",
        description=(
            "Pre-asset smoke test: encodes a tiny PNG for Seedance, probes "
            "RMBG-2.0 with that image, verifies ARK credentials, and "
            "optionally submits one tiny Seedance request. Writes "
            ".playtest/preflight/asset-smoke-report.json."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "submit_seedance": {"type": "boolean", "default": False},
                "duration": {"type": "integer", "default": 5},
                "resolution": {"type": "string", "default": "720p"},
                "rmbg_timeout": {"type": "integer", "default": 15},
                "seedance_timeout": {"type": "integer", "default": 600},
            },
            "additionalProperties": False,
        },
        handler=_asset_pipeline_smoke_test,
        requires_workspace=True,
    ),
    ToolDef(
        name="seal_scaffold_reproject",
        description=(
            "Read the fixed Scaffold-owned reproject declaration and seal its exact "
            "non-empty stale list. No caller-supplied paths are accepted; the MCP "
            "binds every file to the current run/build and returns a one-use receipt."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_seal_scaffold_reproject,
        requires_workspace=True,
    ),
    ToolDef(
        name="delete_scaffold_owned_paths",
        description=(
            "Consume one `seal_scaffold_reproject` receipt and delete exactly its "
            "unchanged obsolete scene/helper/test files. Cross-run, stale, reused, "
            "symlinked, unsealed, and broadened deletion requests are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "receipt_id": {"type": "string", "minLength": 42, "maxLength": 42},
            },
            "required": ["receipt_id"],
            "additionalProperties": False,
        },
        handler=_delete_scaffold_owned_paths,
        requires_workspace=True,
    ),
    ToolDef(
        name="write_pipeline_state",
        description=(
            "Transactionally journal and write .playtest/state.json plus "
            "resume.json for crash-safe phase recovery. Generates and preserves one pipeline_run_id, "
            "recomputes the canonical build_digest, gives both projections the "
            "same transaction id, and appends an identity-bound audit event. "
            "The first write, and migration of legacy state missing any routing "
            "field, requires explicit genre, interaction_profile, quality_profile, "
            "and level_model."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "phase": {"type": "string"},
                "current_step": {"type": "string"},
                "status": {"type": "string"},
                "next_action": {"type": "string"},
                "details": {
                    "type": ["object", "array", "string", "number", "boolean", "null"],
                },
                "genre": {
                    "type": "string",
                    "enum": list(GENRE_SLUGS),
                    "description": (
                        "One of the 10 gameplay genre slugs (or `custom`, the "
                        "engineering fallback). A misspelled genre must fail here, "
                        "not silently enter the pipeline."
                    ),
                },
                "design_axes": {"type": ["object", "null"]},
                "level_design_required": {"type": "boolean"},
                "quality_profile": {
                    "type": "string",
                    "enum": list(QUALITY_PROFILES),
                    "description": (
                        "Explicit startup quality profile. Required with "
                        "level_model on a new run or legacy routing migration."
                    ),
                },
                "level_model": {
                    "type": "string",
                    "enum": list(LEVEL_MODELS),
                    "description": (
                        "Level/content model. `authored` is the only value that "
                        "dispatches level-design-agent and requires levels/*.json. "
                        "Required with quality_profile on a new run or legacy "
                        "routing migration; never inferred from legacy booleans."
                    ),
                },
                "release_waivers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "reason": {"type": "string"},
                            "approved_by": {"type": "string"},
                            "approved_at": {"type": "string"},
                        },
                        "required": ["id", "reason", "approved_by", "approved_at"],
                        "additionalProperties": False,
                    },
                },
                "interaction_profile": {
                    "type": "string",
                    "enum": list(INTERACTION_PROFILES),
                    "description": (
                        "Physics seed from Startup routing metadata (arcade-gravity | "
                        "arcade-no-gravity | matter | none). Carried downstream "
                        "so verify can decide whether the interaction_contract "
                        "gate is mandatory or auto-N/A."
                    ),
                },
                "physics": {"type": "string"},
                "has_levels": {"type": "boolean"},
                "level_files": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Complete ordered absolute paths to the current "
                        "levels/stage_<number>.json files. Valid only for "
                        "level_model='authored' after Level Design validation; "
                        "the array length is dynamic."
                    ),
                },
                "scenes": {"type": ["object", "array"]},
                "assets_generated": {"type": "boolean"},
                "visual_verified": {"type": "boolean"},
                "playthrough_recorded": {"type": "boolean"},
                "first_pass_complete": {"type": "boolean"},
                "tests": {"type": "object"},
                "known_issues": {"type": "array"},
                "design_debt": {"type": "array"},
                "verify_runs": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Guarded counter: unchanged or increased only within "
                        "pipeline.max_counter_increment, with prior "
                        "`verify_run` audit event for each claimed run."
                    ),
                },
                "iteration_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Guarded counter: unchanged or increased only within "
                        "pipeline.max_counter_increment, with prior "
                        "`iteration_complete` audit event for each claimed cycle. "
                        "Runtime rejects any advance beyond "
                        "pipeline.max_iterations."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_write_pipeline_state,
        requires_workspace=True,
    ),
]
