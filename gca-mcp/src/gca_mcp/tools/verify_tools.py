"""Verification tools — run_tests, gate_check, write_verify_report, play_game,
visual_diff, capture_layout, materialize_layout_alignment."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import quote

from .. import playtest_layout as layout
from ..adapters.script_runner import run_node_script, runner_path
from ..config import PLAY_WATCHDOG_GRACE_SECONDS
from ..errors import ToolError
from ..evidence import (
    atomic_write_json,
    build_input_files,
    compute_build_digest,
    manifest_entries,
    new_verify_run_id,
    read_pipeline_identity,
    stamp_json_file,
    stamp_jsonl_last,
)
from ..server import ToolCtx, ToolDef
from ..verify_checkpoint import (
    checkpoint_issues,
    load_checkpoints,
    run_checkpointed,
)
from ..verify_epoch import (
    canonical_playtest_path,
    create_runtime_token_receipt,
    identity_from_run,
    load_runtime_token_receipt,
    normalize_epoch_url,
    read_current_run,
    require_current_run,
    runtime_cli_args,
    sha256_file,
    validate_runtime_evidence,
    verify_dag_flock,
    verify_flock,
)
from ..workspace import validated_workspace_root
from ._shared import (
    gdd_slot_context_sha256 as _gdd_slot_context_sha256,
)
from ._shared import (
    parse_json_output as _parse_json_output,
)
from ._shared import (
    resolve_workspace_path as _resolve_workspace_path,
)
from ._shared import (
    slot_contract_sha256 as _slot_contract_sha256,
)
from ._shared import (
    workspace_relative as _rel,
)

_GENRES = {
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
_INTERACTION_PROFILES = {"arcade-gravity", "arcade-no-gravity", "matter", "none"}
_CANONICAL_URL_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")
_VERIFY_SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _resolve_game_dir(ctx: ToolCtx, value: Any) -> str:
    """Resolve exactly the active workspace; verification never targets another tree."""
    workspace = getattr(getattr(ctx, "state", None), "workspace", None)
    if workspace is None:
        raise ToolError(
            "NO_WORKSPACE_OPEN",
            "Open the game workspace via init_workspace before verification.",
        )
    workspace_root = validated_workspace_root(workspace)
    if value:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        path = path.resolve()
        if path != workspace_root:
            raise ToolError(
                "WORKSPACE_MISMATCH",
                f"game_dir {path} does not match the active workspace {workspace_root}.",
            )
        if not path.is_dir():
            raise ToolError(
                "GAME_DIR_NOT_FOUND",
                f"game_dir does not exist: {path}",
            )
        return str(path)
    return str(workspace_root)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _runtime_asset_path_issue(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return "path must be a non-empty string"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "path contains a control character"
    if "\\" in value:
        return "path must use forward slashes"
    if "%" in value:
        return "path must not contain percent-encoding"
    if "?" in value or "#" in value:
        return "path must not contain URL query or fragment delimiters"
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return "path must be relative"
    parts = value.split("/")
    if (
        len(parts) < 2
        or parts[0] != "assets"
        or any(
            not part
            or part in {".", ".."}
            or _CANONICAL_URL_PATH_SEGMENT_RE.fullmatch(part) is None
            for part in parts
        )
    ):
        return "path must be a canonical pathname under assets/**"
    if quote(value, safe="/!$&'()*+,-.:;=@_~") != value:
        return "browser URL canonicalization would change the pathname"
    return None


def _resolve_runtime_manifest_path(
    game_dir: Path,
    value: Any,
) -> tuple[Path | None, str | None]:
    issue = _runtime_asset_path_issue(value)
    if issue:
        return None, issue
    path = (game_dir / value).resolve()
    if not path.is_relative_to(game_dir):
        return None, "resolved path escapes the game workspace"
    return path, None


def _require_runtime_manifest_path(
    game_dir: Path,
    value: Any,
    *,
    label: str,
    must_exist: bool = False,
) -> Path:
    path, issue = _resolve_runtime_manifest_path(game_dir, value)
    if issue or path is None or (must_exist and not path.is_file()):
        detail = issue or "file not found"
        raise ToolError(
            "INVALID_ARTIFACT",
            f"{label} has an unsafe, non-canonical, or missing path: {detail}.",
        )
    return path


def _require_canonical_path(
    game_dir: Path,
    value: Any,
    *,
    field: str,
    canonical_rel: str,
) -> Path:
    path = _resolve_workspace_path(
        game_dir,
        value,
        field=field,
        allowed_roots=(canonical_rel,),
        default=canonical_rel,
    )
    canonical = game_dir.resolve() / canonical_rel
    if canonical.resolve() != canonical:
        raise ToolError(
            "NON_CANONICAL_OUTPUT",
            f"`{field}` canonical path may not traverse symlinks: {canonical_rel}.",
        )
    if path != canonical:
        raise ToolError(
            "NON_CANONICAL_OUTPUT",
            f"`{field}` must be the canonical path {canonical_rel}.",
        )
    return path


def _require_verify_run(
    game_dir: Path,
    ctx: ToolCtx,
    verify_run_id: Any,
    *,
    allowed_statuses: set[str] | None = None,
) -> dict[str, Any]:
    _require_canonical_verify_tree(game_dir)
    pipeline = _require_pipeline_identity(game_dir, ctx)
    run = require_current_run(
        game_dir,
        verify_run_id,
        allowed_statuses=allowed_statuses or {"active"},
        pipeline_identity=pipeline,
    )
    state = _read_json(game_dir / ".playtest" / "state.json")
    if not isinstance(state, dict) or any(
        run.get(key) != state.get(key)
        for key in ("genre", "interaction_profile", "quality_profile", "level_model")
    ):
        raise ToolError(
            "VERIFY_RUN_IDENTITY_MISMATCH",
            "current-run.json routing metadata does not match state.json; "
            "begin a new verify run.",
        )
    load_runtime_token_receipt(game_dir, run)
    return run


def _require_canonical_verify_tree(game_dir: Path) -> None:
    """Reject symlinked verification authorities or output directories."""
    for relative in (
        layout.VERIFY_DIR,
        layout.TEST_RUN_DIR,
        layout.RECORDING_DIR,
        layout.RUNTIME_DIR,
        layout.PLAY_DIR,
        layout.ALIGNMENT_DIR,
        layout.CRITIC_DIR,
        layout.ASSETS_DIR,
        layout.GATE_RESULTS_DIR,
        layout.VERIFY_CHECKPOINTS_DIR,
        layout.VERIFY_MIDDLE_DIR,
        layout.LAYOUT_RENDERED_DIR,
        layout.VISUAL_DIFF_DIR,
        layout.VERIFY_RUN,
        layout.VERIFY_REPORT,
        layout.RUNTIME_TOKEN_RECEIPT,
        layout.RUNTIME_ASSETS_RECEIPT,
        layout.TEST_RESULTS,
        layout.PLAY_REPORT,
        layout.PLAY_HISTORY,
        layout.VISUAL_ALIGNMENT,
        layout.GAMEPLAY_ALIGNMENT,
        layout.ELEMENT_ALIGNMENT,
        layout.PHYSICS_ALIGNMENT,
        layout.LAYOUT_ALIGNMENT,
        layout.ASSET_INTEGRATION_REPORT,
        layout.VISUAL_DIFF_REPORT,
        layout.VISUAL_DIFF_HISTORY,
    ):
        canonical_playtest_path(game_dir, relative)


def _require_verify_identity(
    game_dir: Path,
    ctx: ToolCtx,
    verify_run_id: Any,
    *,
    allowed_statuses: set[str] | None = None,
) -> dict[str, str]:
    return identity_from_run(
        _require_verify_run(
            game_dir,
            ctx,
            verify_run_id,
            allowed_statuses=allowed_statuses,
        )
    )


def _require_epoch_url(args: dict[str, Any], run: dict[str, Any]) -> str:
    url = normalize_epoch_url(args.get("url"))
    if url != run.get("url"):
        raise ToolError(
            "VERIFY_URL_MISMATCH",
            "Producer `url` must equal the normalized URL passed to begin_verify_run.",
        )
    return url


def _checkpointed_verify_handler(
    producer_name: str,
    handler: Callable[[dict[str, Any], ToolCtx], dict[str, Any]],
) -> Callable[[dict[str, Any], ToolCtx], dict[str, Any]]:
    """Wrap an externally invoked producer with an identity-bound checkpoint."""

    def wrapped(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
        with verify_dag_flock(game_dir):
            run = _require_verify_run(game_dir, ctx, args.get("verify_run_id"))
            prepare_retry = _verify_retry_cleanup(
                game_dir,
                producer_name,
                args,
            )
            instance = None
            if producer_name == "run_tests" and prepare_retry is None:
                encoded = json.dumps(
                    args,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                instance = f"scoped-{hashlib.sha256(encoded).hexdigest()[:16]}"
            return run_checkpointed(
                game_dir=game_dir,
                run=run,
                producer_name=producer_name,
                instance=instance,
                arguments=args,
                call=lambda: handler(args, ctx),
                prepare_retry=prepare_retry,
                reuse_completed=True,
            )

    return wrapped


def _require_pipeline_identity(game_dir: Path, ctx: ToolCtx) -> dict[str, str]:
    state_path = canonical_playtest_path(game_dir, layout.STATE)
    resume_path = canonical_playtest_path(game_dir, layout.RESUME)
    pending_path = canonical_playtest_path(
        game_dir,
        "state-transaction.pending.json",
    )
    if pending_path.is_file():
        raise ToolError(
            "STATE_TRANSACTION_PENDING",
            "state/resume transaction is incomplete; call write_pipeline_state.",
        )
    identity = read_pipeline_identity(game_dir)
    if identity is None:
        raise ToolError(
            "PIPELINE_IDENTITY_MISSING",
            "state.json must contain pipeline_run_id and build_digest; call write_pipeline_state.",
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "PIPELINE_IDENTITY_MISSING", f"Invalid state.json: {exc}"
        ) from exc
    if (
        state.get("genre") not in _GENRES
        or state.get("interaction_profile") not in _INTERACTION_PROFILES
        or state.get("quality_profile")
        not in {"art_directed", "responsive", "pixel_art", "minimal"}
        or state.get("level_model") not in {"authored", "procedural", "board", "none"}
    ):
        raise ToolError(
            "ROUTING_METADATA_MISSING",
            "state.json must contain valid genre, interaction_profile, "
            "quality_profile, and level_model; "
            "call write_pipeline_state.",
        )
    level_model = state["level_model"]
    if state.get("level_design_required") is not (level_model == "authored"):
        raise ToolError(
            "ROUTING_METADATA_MISMATCH",
            "level_design_required must equal (level_model == 'authored').",
        )
    design_axes = state.get("design_axes")
    if (
        isinstance(design_axes, dict)
        and design_axes.get("physics") is not None
        and design_axes.get("physics") != state["interaction_profile"]
    ):
        raise ToolError(
            "ROUTING_METADATA_MISMATCH",
            "interaction_profile must equal design_axes.physics.",
        )
    if level_model != "authored" and state.get("has_levels") is True:
        raise ToolError(
            "ROUTING_METADATA_MISMATCH",
            "has_levels=true is valid only for level_model='authored'.",
        )
    from .lifecycle import (
        _audit_event_counts,
        _validate_counter_claims,
        _validate_counter_state_contract,
    )

    try:
        audit_offsets = _validate_counter_state_contract(state)
        _validate_counter_claims(
            state,
            _audit_event_counts(game_dir, identity["pipeline_run_id"]),
            audit_offsets,
        )
    except ToolError as exc:
        raise ToolError(
            "COUNTER_EVIDENCE_MISMATCH",
            f"Invalid guarded counter evidence: {exc.message}",
        ) from exc
    resume = _read_json(resume_path)
    transaction_id = state.get("state_transaction_id")
    if (
        not transaction_id
        or not isinstance(resume, dict)
        or any(
            resume.get(key) != state.get(key)
            for key in (
                "pipeline_run_id",
                "build_digest",
                "state_transaction_id",
                "genre",
                "interaction_profile",
                "quality_profile",
                "level_model",
                "iteration_budget",
            )
        )
    ):
        raise ToolError(
            "STATE_TRANSACTION_MISMATCH",
            "state.json and resume.json must be one identity/transaction snapshot; "
            "call write_pipeline_state.",
        )
    actual_digest = compute_build_digest(game_dir, ctx.repo_root / "gca.config.yaml")
    if actual_digest != identity["build_digest"]:
        raise ToolError(
            "STALE_PIPELINE_STATE",
            "Build inputs changed since the last state write; call write_pipeline_state first.",
        )
    return identity


def _stamp_if_present(path: Path, identity: dict[str, str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return stamp_json_file(path, identity)
    except ValueError as exc:
        raise ToolError("INVALID_ARTIFACT", str(exc)) from exc


def _matches_identity(payload: Any, identity: dict[str, str]) -> bool:
    return isinstance(payload, dict) and all(
        payload.get(key) == value for key, value in identity.items()
    )


def _current_video_receipts(
    game_dir: Path,
    run: dict[str, Any],
) -> list[tuple[Path, dict[str, Any], Path, Path, Path]]:
    base = game_dir / ".playtest" / layout.RECORDING_DIR
    receipts: list[tuple[Path, dict[str, Any], Path, Path, Path]] = []
    if not base.is_dir():
        return receipts
    for receipt_path in sorted(base.glob(f"*/{layout.VIDEO_RECEIPT_NAME}")):
        receipt = _read_json(receipt_path)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("producer") != "gca-mcp.record_with_state"
            or not _matches_identity(receipt, identity_from_run(run))
            or receipt.get("url") != run.get("url")
            or receipt.get("runtime_token_sha256") != run.get("runtime_token_sha256")
            or receipt.get("runtime_receipt_sha256")
            != run.get("runtime_receipt_sha256")
        ):
            continue
        try:
            validate_runtime_evidence(receipt.get("runtime_identity"), run)
            scenario_dir = receipt_path.parent.resolve()
            if scenario_dir.parent != base.resolve():
                continue
            video = (game_dir / str(receipt.get("video_path", ""))).resolve()
            timeline = (game_dir / str(receipt.get("timeline_path", ""))).resolve()
            performance = (
                game_dir / str(receipt.get("performance_path", ""))
            ).resolve()
            if (
                video.parent != scenario_dir
                or timeline.parent != scenario_dir
                or performance.parent != scenario_dir
                or video.name != "recording.webm"
                or timeline.name != "state-timeline.json"
                or performance.name != "performance-summary.json"
                or not all(path.is_file() for path in (video, timeline, performance))
                or sha256_file(video) != receipt.get("video_sha256")
                or sha256_file(timeline) != receipt.get("timeline_sha256")
                or sha256_file(performance) != receipt.get("performance_sha256")
            ):
                continue
        except (OSError, ToolError):
            continue
        receipts.append((receipt_path, receipt, video, timeline, performance))
    return receipts


def _clear_dir_except(directory: Path, keep: set[str] | None = None) -> None:
    if not directory.is_dir():
        return
    keep = keep or set()
    for path in directory.iterdir():
        if path.name in keep:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _verify_retry_cleanup(
    game_dir: Path,
    producer_name: str,
    args: dict[str, Any],
) -> Callable[[], None] | None:
    """Return a safe cleanup for a direct producer retry."""

    def remove(*relative_paths: str) -> None:
        for relative in relative_paths:
            path = canonical_playtest_path(game_dir, relative)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    if producer_name == "run_tests":
        raw_tests = args.get("tests") or f".playtest/{layout.TESTS_DIR}"
        candidate = Path(str(raw_tests)).expanduser()
        if not candidate.is_absolute():
            candidate = game_dir / candidate
        canonical_tests = canonical_playtest_path(game_dir, layout.TESTS_DIR)
        scoped = (
            bool(args.get("solo"))
            or any(args.get(key) is not None for key in ("filter", "id", "changed", "script"))
            or candidate.resolve() != canonical_tests
        )
        if scoped:
            return None
        return lambda: remove(layout.TEST_RUN_DIR, layout.TEST_RESULTS)
    if producer_name == "play_game":
        return lambda: _clear_dir_except(
            canonical_playtest_path(game_dir, layout.PLAY_DIR),
            {Path(layout.PLAY_HISTORY).name},
        )
    if producer_name == "visual_diff":
        def reset_visual() -> None:
            _clear_dir_except(
                canonical_playtest_path(game_dir, layout.VISUAL_DIFF_DIR),
                {Path(layout.VISUAL_DIFF_HISTORY).name},
            )
            remove(layout.VISUAL_ALIGNMENT, layout.ELEMENT_ALIGNMENT)

        return reset_visual
    if producer_name == "capture_layout":
        return lambda: remove(layout.LAYOUT_RENDERED_DIR)
    if producer_name == "capture_runtime_assets":
        return lambda: remove(layout.RUNTIME_ASSETS_RECEIPT)
    alignment_outputs = {
        "materialize_gameplay_alignment": layout.GAMEPLAY_ALIGNMENT,
        "materialize_physics_alignment": layout.PHYSICS_ALIGNMENT,
        "materialize_layout_alignment": layout.LAYOUT_ALIGNMENT,
    }
    if producer_name in alignment_outputs:
        return lambda: remove(alignment_outputs[producer_name])
    return None


def _write_asset_integration_report(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Derive the integration receipt from manifest, files, and runtime source."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=True):
        _require_canonical_verify_tree(game_dir)
        current = read_current_run(game_dir)
        if isinstance(current, dict) and current.get("status") == "active":
            raise ToolError(
                "VERIFY_RUN_ACTIVE",
                "Asset integration cannot be rewritten during an active verify run.",
            )
        return _write_asset_integration_report_locked(args, ctx, game_dir)


def _write_asset_integration_report_locked(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    pipeline = read_pipeline_identity(game_dir)
    if pipeline is None:
        raise ToolError(
            "PIPELINE_IDENTITY_MISSING",
            "write_pipeline_state must establish pipeline_run_id before integration.",
        )
    manifest_path = game_dir / "assets" / "asset-manifest.json"
    manifest = _read_json(manifest_path)
    entries = manifest_entries(manifest)
    valid_manifest_shape = isinstance(manifest, list) or (
        isinstance(manifest, dict)
        and (
            any(
                isinstance(manifest.get(field), list)
                for field in ("assets", "entries", "items")
            )
            or bool(entries)
        )
    )
    if not valid_manifest_shape:
        raise ToolError(
            "INVALID_ARTIFACT",
            "asset-manifest.json must contain asset entries.",
        )
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((game_dir / "src").rglob("*.js"))
        if path.is_file()
    )
    checks: list[dict[str, Any]] = []
    derived_issues: list[dict[str, Any]] = []
    files_checked = 0
    wired_count = 0

    def add_issue(key: str, category: str, description: str) -> None:
        derived_issues.append(
            {
                "severity": "high",
                "category": category,
                "asset_key": key,
                "description": description,
                "route_to": (
                    "assets-agent"
                    if category in {"missing_manifest_path", "bad_metadata"}
                    else "implement-agent"
                ),
                "root_cause": "MCP-derived integration check",
            }
        )

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise ToolError(
                "INVALID_ARTIFACT",
                "Every asset-manifest entry must be an object with a key.",
            )
        key = entry["key"]
        actions = entry.get("actions")
        specs = (
            list(actions.values()) if isinstance(actions, dict) and actions else [entry]
        )
        file_checks: list[dict[str, Any]] = []
        tokens = {key}
        for spec in specs:
            if not isinstance(spec, dict):
                add_issue(key, "contract_gap", "Manifest action is not an object.")
                continue
            path_value = spec.get("path")
            if not isinstance(path_value, str) or not path_value:
                add_issue(
                    key, "missing_manifest_path", "Manifest file path is missing."
                )
                continue
            path, path_issue = _resolve_runtime_manifest_path(game_dir, path_value)
            if path_issue or path is None:
                add_issue(
                    key,
                    "missing_manifest_path",
                    f"Manifest path is unsafe/non-canonical: {path_issue}.",
                )
                continue
            files_checked += 1
            metadata_value = spec.get("metadata")
            metadata_path: Path | None = None
            metadata_issue: str | None = None
            if isinstance(metadata_value, str) and metadata_value:
                metadata_path, metadata_issue = _resolve_runtime_manifest_path(
                    game_dir,
                    metadata_value,
                )
            exists = path.is_file()
            metadata_exists = metadata_value is None or (
                metadata_issue is None
                and metadata_path is not None
                and metadata_path.is_file()
            )
            file_checks.append(
                {
                    "path": path_value,
                    "exists": exists,
                    "metadata": metadata_value,
                    "metadata_exists": metadata_exists,
                }
            )
            if not exists:
                add_issue(key, "missing_manifest_path", f"Missing {path_value}.")
            if not metadata_exists:
                add_issue(
                    key,
                    "bad_metadata",
                    f"Missing or unsafe metadata {metadata_value}: "
                    f"{metadata_issue or 'file not found'}.",
                )
            for token in (spec.get("textureKey"), path_value):
                if isinstance(token, str) and token:
                    tokens.add(token)
        wired = any(
            f"'{token}'" in source or f'"{token}"' in source for token in tokens
        )
        if wired:
            wired_count += 1
        else:
            add_issue(
                key,
                "asset_key_mismatch",
                "No manifest key, texture key, or declared path is referenced by src/.",
            )
        checks.append(
            {
                "key": key,
                "sourceStrategy": entry.get("sourceStrategy"),
                "wired": wired,
                "files": file_checks,
            }
        )

    reported_issues = args.get("issues") or []
    issues = [*derived_issues, *reported_issues]
    blocking = [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and str(issue.get("severity", "")).lower() in {"high", "critical", "blocker"}
    ]
    actual_digest = compute_build_digest(
        game_dir,
        ctx.repo_root / "gca.config.yaml",
    )
    report = {
        "schema_version": 2,
        "producer": "gca-mcp.write_asset_integration_report",
        "generated_at": _utc_now(),
        "pipeline_run_id": pipeline["pipeline_run_id"],
        "build_digest": actual_digest,
        "manifest_sha256": sha256_file(manifest_path),
        "summary": {
            "manifest_entries": len(entries),
            "files_checked": files_checked,
            "assets_wired": wired_count,
            "blocking_issue_count": len(blocking),
        },
        "checks": checks,
        "issues": issues,
        "blocking_issues": blocking,
    }
    output = canonical_playtest_path(game_dir, layout.ASSET_INTEGRATION_REPORT)
    atomic_write_json(output, report)
    return {**report, "report_path": _rel(game_dir, output)}


def _validate_asset_integration_receipt(
    game_dir: Path,
    report: Any,
    identity: dict[str, str],
) -> None:
    manifest_path = game_dir / "assets" / "asset-manifest.json"
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 2
        or report.get("producer") != "gca-mcp.write_asset_integration_report"
        or report.get("pipeline_run_id") != identity["pipeline_run_id"]
        or report.get("build_digest") != identity["build_digest"]
        or not manifest_path.is_file()
        or report.get("manifest_sha256") != sha256_file(manifest_path)
        or not isinstance(report.get("checks"), list)
        or not isinstance(report.get("blocking_issues"), list)
        or report["blocking_issues"]
    ):
        raise ToolError(
            "INVALID_ASSET_INTEGRATION",
            "Asset integration requires a current, clean MCP-owned receipt; "
            "call write_asset_integration_report after integration.",
        )


def _seal_slot_receipts(
    game_dir: Path,
    identity: dict[str, str],
) -> list[str]:
    slot_dir = canonical_playtest_path(game_dir, layout.SLOT_VALIDATION_DIR)
    if not slot_dir.is_dir():
        return []
    manifest_path = game_dir / "assets" / "asset-manifest.json"
    gdd_path = game_dir / "GAME_DESIGN.md"
    manifest = _read_json(manifest_path)
    try:
        gdd_text = gdd_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        gdd_text = ""
    entries = {
        entry.get("key"): entry
        for entry in manifest_entries(manifest)
        if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }
    pending: list[Path] = []
    for report_path in sorted(slot_dir.glob("*.json")):
        if report_path.is_symlink():
            raise ToolError(
                "INVALID_SLOT_VALIDATION",
                f"{report_path.name} may not be a symlink.",
            )
        report = _read_json(report_path)
        key = report.get("key") if isinstance(report, dict) else None
        action = report.get("action") if isinstance(report, dict) else None
        entry = entries.get(key)
        image_value = report.get("image") if isinstance(report, dict) else None
        image_path = (
            (game_dir / image_value).resolve() if isinstance(image_value, str) else None
        )
        description = entry.get("description") if isinstance(entry, dict) else None
        actions = entry.get("actions") if isinstance(entry, dict) else None
        expected_action = (
            entry.get("defaultAction")
            if isinstance(actions, dict) and actions
            else None
        )
        try:
            gdd_slot_context_sha256 = (
                _gdd_slot_context_sha256(
                    gdd_text,
                    key=str(key),
                    description=description,
                    action=(
                        expected_action
                        if isinstance(expected_action, str)
                        else None
                    ),
                )
                if isinstance(description, str)
                else None
            )
        except ValueError:
            gdd_slot_context_sha256 = None
        valid = (
            isinstance(report, dict)
            and report.get("schema_version") == 3
            and report.get("producer") == "gca-mcp.validate_asset_slot"
            and report_path.stem == key
            and isinstance(description, str)
            and isinstance(image_path, Path)
            and image_path.is_relative_to(game_dir)
            and image_path.is_file()
            and gdd_path.is_file()
            and manifest_path.is_file()
            and report.get("image_sha256") == sha256_file(image_path)
            and isinstance(gdd_slot_context_sha256, str)
            and report.get("gdd_slot_context_sha256")
            == gdd_slot_context_sha256
            and report.get("slot_contract_sha256") == _slot_contract_sha256(entry)
            and action == expected_action
            and report.get("slot_description_sha256")
            == hashlib.sha256(description.encode("utf-8")).hexdigest()
        )
        if not valid:
            raise ToolError(
                "INVALID_SLOT_VALIDATION",
                f"{report_path.name} is stale, forged, or not bound to its "
                "current slot contract/GDD description/image.",
            )
        pending.append(report_path)

    # Validate the whole set before mutating any receipt, so begin cannot leave
    # a partially sealed slot set when a later entry is invalid.
    for report_path in pending:
        stamp_json_file(report_path, identity)
    return [_rel(game_dir, report_path) for report_path in pending]


def _begin_verify_run(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """CAS-open one evidence epoch under the workspace verify flock."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    url = normalize_epoch_url(args.get("url"))
    mode = args.get("mode")
    if mode not in {"iterate", "full", "full-novideo"}:
        raise ToolError(
            "INVALID_VERIFY_MODE",
            "begin_verify_run mode must be iterate, full, or full-novideo.",
        )

    with verify_flock(game_dir, exclusive=True):
        _require_canonical_verify_tree(game_dir)
        existing = read_current_run(game_dir)
        if isinstance(existing, dict) and existing.get("status") == "active":
            if args.get("resume_active") is not True:
                raise ToolError(
                    "VERIFY_RUN_ACTIVE",
                    "An active verification run already exists. After confirming "
                    "its previous verifier has stopped, retry begin_verify_run "
                    "with the same mode/URL and resume_active=true.",
                )
            resumed = _require_verify_run(
                game_dir,
                ctx,
                existing.get("verify_run_id"),
            )
            if resumed.get("mode") != mode:
                raise ToolError(
                    "VERIFY_MODE_MISMATCH",
                    "Resumed verification mode must match the active run.",
                )
            if resumed.get("url") != url:
                raise ToolError(
                    "VERIFY_URL_MISMATCH",
                    "Resumed verification URL must match the active run.",
                )
            checkpoints = load_checkpoints(game_dir, resumed)
            return {
                **identity_from_run(resumed),
                "mode": resumed["mode"],
                "url": resumed["url"],
                "run_path": _rel(
                    game_dir,
                    canonical_playtest_path(game_dir, layout.VERIFY_RUN),
                ),
                "runtime_token_receipt": resumed["runtime_token_receipt"],
                "runtime_token_url": resumed["runtime_token_url"],
                "runtime_token_sha256": resumed["runtime_token_sha256"],
                "runtime_receipt_sha256": resumed["runtime_receipt_sha256"],
                "sealed_artifacts": [],
                "producer_checkpoints": [
                    {
                        "producer_name": checkpoint.get("producer_name"),
                        "status": checkpoint.get("status"),
                        "path": checkpoint.get("path"),
                    }
                    for checkpoint in checkpoints
                ],
                "iteration_budget": resumed.get("iteration_budget"),
                "resumed": True,
            }
        if isinstance(existing, dict) and existing.get("status") != "finalized":
            raise ToolError(
                "VERIFY_RUN_STATE_INVALID",
                "The previous current-run.json is neither active nor finalized; "
                "repair or remove it before beginning another run.",
            )

        pipeline = _require_pipeline_identity(game_dir, ctx)
        playtest_dir = canonical_playtest_path(game_dir)
        state = _read_json(playtest_dir / "state.json")
        iteration_count = state.get(
            "iteration_count",
            state.get("iterationCount", 0),
        )
        max_iterations = int(ctx.config.pipeline.max_iterations)
        iteration_budget = {
            "max_iterations": max_iterations,
            "used_iterations": iteration_count,
            "remaining_iterations": max(0, max_iterations - iteration_count),
            "exhausted": iteration_count >= max_iterations,
        }
        persisted_budget = state.get("iteration_budget")
        if persisted_budget is not None and persisted_budget != iteration_budget:
            raise ToolError(
                "STATE_TRANSACTION_MISMATCH",
                "state.json iteration_budget is stale or inconsistent; "
                "call write_pipeline_state before verification.",
            )
        if mode == "iterate" and iteration_budget["exhausted"]:
            raise ToolError(
                "ITERATION_BUDGET_EXHAUSTED",
                "Cannot begin another iterate verification epoch: "
                f"iteration_count={iteration_count}, "
                f"pipeline.max_iterations={max_iterations}.",
                "Report unresolved blockers or run the final full confirmation; "
                "do not start another automatic fix cycle.",
            )
        integration_path = playtest_dir / layout.ASSET_INTEGRATION_REPORT
        if integration_path.is_file():
            integration = _read_json(integration_path)
            _validate_asset_integration_receipt(game_dir, integration, pipeline)
            build_inputs = build_input_files(
                game_dir,
                ctx.repo_root / "gca.config.yaml",
            )
            newest_input = max(
                (path.stat().st_mtime_ns for path in build_inputs if path.is_file()),
                default=0,
            )
            if integration_path.stat().st_mtime_ns < newest_input:
                raise ToolError(
                    "STALE_ASSET_INTEGRATION",
                    "asset-integration-report.json predates a canonical build input; "
                    "rerun asset-integration-agent before verification.",
                )
        else:
            raise ToolError(
                "INVALID_ASSET_INTEGRATION",
                "asset-integration-report.json is missing; call "
                "write_asset_integration_report after integration.",
            )

        for rel in (
            layout.TEST_RUN_DIR,
            layout.RECORDING_DIR,
            layout.RUNTIME_DIR,
            layout.ALIGNMENT_DIR,
            layout.CRITIC_DIR,
            layout.GATE_RESULTS_DIR,
            layout.VERIFY_CHECKPOINTS_DIR,
            layout.LAYOUT_RENDERED_DIR,
        ):
            shutil.rmtree(playtest_dir / rel, ignore_errors=True)
        _clear_dir_except(
            playtest_dir / layout.PLAY_DIR,
            {Path(layout.PLAY_HISTORY).name},
        )
        _clear_dir_except(
            playtest_dir / layout.VISUAL_DIFF_DIR,
            {Path(layout.VISUAL_DIFF_HISTORY).name},
        )
        (playtest_dir / layout.VERIFY_REPORT).unlink(missing_ok=True)

        verify_run_id = new_verify_run_id()
        identity = {
            **pipeline,
            "verify_run_id": verify_run_id,
        }
        generated_at = _utc_now()
        token_receipt, token_path, token_receipt_sha = create_runtime_token_receipt(
            game_dir,
            identity=identity,
            url=url,
            generated_at=generated_at,
        )
        run = {
            "schema_version": 2,
            **identity,
            "genre": state["genre"],
            "interaction_profile": state["interaction_profile"],
            "quality_profile": state["quality_profile"],
            "level_model": state["level_model"],
            "iteration_budget": iteration_budget,
            "mode": mode,
            "url": url,
            "runtime_token_receipt": _rel(game_dir, token_path),
            "runtime_token_url": token_receipt["runtime_token_url"],
            "runtime_token_sha256": token_receipt["runtime_token_sha256"],
            "runtime_receipt_sha256": token_receipt_sha,
            "started_at": generated_at,
            "started_at_ns": time.time_ns(),
            "status": "active",
        }
        run_path = playtest_dir / layout.VERIFY_RUN

        sealed = []
        if _stamp_if_present(integration_path, identity) is not None:
            sealed.append(_rel(game_dir, integration_path))
        sealed.extend(_seal_slot_receipts(game_dir, identity))
        # Publish the active epoch only after every prerequisite is sealed.
        # A failed begin therefore cannot strand an active ID that was never
        # returned to its caller.
        atomic_write_json(run_path, run)
        return {
            **identity,
            "mode": mode,
            "url": url,
            "run_path": _rel(game_dir, run_path),
            "runtime_token_receipt": _rel(game_dir, token_path),
            "runtime_token_url": token_receipt["runtime_token_url"],
            "runtime_token_sha256": token_receipt["runtime_token_sha256"],
            "runtime_receipt_sha256": token_receipt_sha,
            "sealed_artifacts": sealed,
            "producer_checkpoints": [],
            "iteration_budget": iteration_budget,
            "resumed": False,
        }


def _begin_verify_run_serialized(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Serialize direct low-level epoch opens against the canonical DAG."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_dag_flock(game_dir):
        return _begin_verify_run(args, ctx)


def _file_entry(game_dir: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": _rel(game_dir, path),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(path),
        "mtime": datetime.fromtimestamp(
            stat.st_mtime,
            tz=UTC,
        )
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _latest_mtime(paths: list[Path]) -> float | None:
    existing = [p.stat().st_mtime for p in paths if p.is_file()]
    return max(existing) if existing else None


def _latest_src_mtime(game_dir: Path) -> float | None:
    src_dir = game_dir / "src"
    if not src_dir.is_dir():
        return None
    return _latest_mtime([p for p in src_dir.rglob("*.js") if p.is_file()])


def _save_test_results(
    game_dir: Path | None,
    proc: dict[str, Any],
    *,
    result_rel: str,
    scope: dict[str, Any],
    identity: dict[str, str] | None = None,
) -> str | None:
    if game_dir is None:
        return None
    playtest_dir = canonical_playtest_path(game_dir)
    output_path = playtest_dir / result_rel
    output_path.parent.mkdir(parents=True, exist_ok=True)
    parsed = _parse_json_output(proc.get("stdout", ""))
    payload = {
        "source": "test-runner.mjs",
        "run_id": f"tests-{time.time_ns()}",
        "generated_at": _utc_now(),
        "returncode": proc.get("returncode"),
        "result": parsed,
        "scope": scope,
        "stderr_tail": proc.get("stderr", "")[-1000:],
        **(identity or {}),
    }
    if parsed is None:
        payload["stdout_tail"] = proc.get("stdout", "")[-2000:]
    atomic_write_json(output_path, payload)
    return _rel(game_dir, output_path)


def _save_gate_result(
    game_dir: Path,
    result: dict[str, Any],
    identity: dict[str, str] | None = None,
) -> str:
    gate_results_dir = game_dir / ".playtest" / layout.GATE_RESULTS_DIR
    gate_results_dir.mkdir(parents=True, exist_ok=True)
    gate_name = str(result["gate"]).replace("/", "_")
    step = result.get("step")
    suffix = (
        f"-{step}"
        if gate_name == "step_done"
        and isinstance(step, int)
        and not isinstance(step, bool)
        else ""
    )
    output_path = gate_results_dir / f"{gate_name}{suffix}.json"
    payload = {
        "generated_at": _utc_now(),
        **result,
        **(identity or {}),
    }
    atomic_write_json(output_path, payload)
    return _rel(game_dir, output_path)


# -------------------- gate_check --------------------

_VERIFY_EPOCH_GATES = {
    "first_pass_complete",
    "iteration_ready",
    "layout_alignment",
    "materialize_layout_alignment",
    "performance",
    "play_quality",
    "polish_rounds",
    "ready_to_ship",
    "slot_validation",
    "tests_passing",
    "verify_report",
    "visual_diff",
    "critic",
}


def _gate_check(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    gate = args.get("gate")
    if not gate:
        raise ToolError("INVALID_CONFIG", "`gate` is required.")
    raw_step = args.get("step")
    step = (
        int(raw_step)
        if gate == "step_done"
        and isinstance(raw_step, (int, float))
        and not isinstance(raw_step, bool)
        and float(raw_step).is_integer()
        else raw_step
    )
    requires_verify_epoch = gate in _VERIFY_EPOCH_GATES or (
        gate == "step_done" and step == 7
    )
    game_path = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_path, exclusive=False):
        _require_canonical_verify_tree(game_path)
        run: dict[str, Any] | None = None
        if requires_verify_epoch:
            allowed_statuses = (
                {"finalized"}
                if gate == "step_done" and step == 7
                else {"active", "finalized"}
            )
            run = _require_verify_run(
                game_path,
                ctx,
                args.get("verify_run_id"),
                allowed_statuses=allowed_statuses,
            )
            identity = identity_from_run(run)
        else:
            identity = _require_pipeline_identity(game_path, ctx)

        script = runner_path("verify", "gate-check.mjs")
        cli = [
            "--game",
            str(game_path),
            "--gate",
            str(gate),
            "--config",
            str(ctx.repo_root / "gca.config.yaml"),
        ]
        if step is not None:
            cli.extend(["--step", str(step)])
        min_accuracy = args.get(
            "min_accuracy",
            ctx.config.gates.play_quality_min_accuracy,
        )
        cli.extend(["--min-accuracy", str(min_accuracy)])

        proc = run_node_script(script, cli, cwd=game_path, check=False)
        blocked = proc["returncode"] != 0
        out = {
            "gate": gate,
            "game_dir": ".",
            "blocked": blocked,
            "returncode": proc["returncode"],
            "stdout": proc["stdout"],
            "stderr": proc["stderr"],
            **identity,
        }
        if gate == "step_done":
            out["step"] = step
        # P3: merge the structured BLOCKED payload gate-check.mjs prints on
        # stdout (`failures[]`, each with its originating check + axis/agent
        # routing hints) so the main agent can group + dispatch blockers without
        # NLP-parsing the stderr text. The JSON-emitting gates (build_digest /
        # gate_contract / materialize_layout_alignment) print a different shape
        # with no `failures` key and are left untouched. The stderr BLOCKED
        # lines remain the backward-compatible channel.
        parsed = _parse_json_output(proc.get("stdout", ""))
        if isinstance(parsed, dict) and isinstance(parsed.get("failures"), list):
            out["failures"] = parsed["failures"]
        # The gate consumes identity-bound epoch evidence; final/release gates
        # additionally enforce finalization in their deterministic checks. Its
        # own atomic result receipt must still be published for the stop hook.
        # This does not modify current-run.json or any sealed input artifact.
        out["result_path"] = _save_gate_result(game_path, out, identity)
        return out


# -------------------- gate diagnostics (P4) --------------------
# Thin, non-epoch diagnostic wrappers over gate_check for the deterministic
# contract gates verify-agent runs mid-build. They exist purely for early,
# targeted feedback and parallel blocker routing; each delegates to _gate_check
# with a fixed gate and returns the same structured payload (incl. `failures[]`
# with axis/agent routing).
#
# IMPORTANT: these are ADVISORY diagnostics, NOT release authorities. The single
# composite release decision stays
# gate_check({gate:"ready_to_ship", verify_run_id:<VERIFY_RUN_ID>}), and the stop
# hook still trusts only gate-results/ready_to_ship.json. All three gates are
# non-epoch (pipeline-identity only), so no verify_run_id is required.
_DIAGNOSTIC_GATES = ("ui_contract", "td_contract", "interaction_contract")


def _make_gate_diagnostic(gate_name: str):
    def _handler(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        return _gate_check({**args, "gate": gate_name}, ctx)

    return _handler


# -------------------- run_tests --------------------


def _run_tests(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    script = runner_path("verify", "test-runner.mjs")
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    solo = bool(args.get("solo"))
    with verify_flock(game_dir, exclusive=False):
        run = _require_verify_run(
            game_dir,
            ctx,
            args.get("verify_run_id"),
        )
        identity = identity_from_run(run)
        url = _require_epoch_url(args, run)
        cli: list[str] = ["--url", url, *runtime_cli_args(game_dir, run)]

        tests: Path | None = None
        if solo:
            cli.append("--solo")
        else:
            tests = _resolve_workspace_path(
                game_dir,
                args.get("tests"),
                field="tests",
                allowed_roots=(f".playtest/{layout.TESTS_DIR}",),
                default=f".playtest/{layout.TESTS_DIR}",
                must_exist=True,
            )
            cli.extend(["--tests", str(tests)])

        for key in ("filter", "id", "changed", "timeout"):
            value = args.get(key)
            if value is not None:
                cli.extend([f"--{key}", str(value)])
        if args.get("script") is not None:
            script_path = _resolve_workspace_path(
                game_dir,
                args["script"],
                field="script",
                allowed_roots=(
                    f".playtest/{layout.INTERACTIONS_DIR}",
                    f".playtest/{layout.TESTS_DIR}",
                ),
                must_exist=True,
            )
            cli.extend(["--script", str(script_path)])

        screenshots_dir = game_dir / ".playtest" / layout.TEST_SCREENSHOTS_DIR
        recordings_dir = game_dir / ".playtest" / layout.TEST_RECORDINGS_DIR
        cli.extend(
            [
                "--screenshots",
                str(screenshots_dir),
                "--recordings",
                str(recordings_dir),
            ]
        )
        for flag in ("screenshot-each", "record", "headed"):
            py_key = flag.replace("-", "_")
            if flag == "record" and ctx.config.verify.skip_video:
                continue
            if args.get(py_key) or args.get(flag):
                cli.append(f"--{flag}")

        proc = run_node_script(
            script,
            cli,
            cwd=game_dir,
            timeout=float(
                args.get(
                    "timeout_seconds",
                    ctx.config.verify.run_tests_timeout_seconds,
                )
            ),
            check=False,
        )
        parsed = _parse_json_output(proc.get("stdout", ""))
        if isinstance(parsed, dict):
            validate_runtime_evidence(parsed.get("runtime_identity"), run)
            if parsed.get("mode") == "formal":
                test_entries = parsed.get("tests")
                if not isinstance(test_entries, list) or any(
                    not isinstance(entry, dict) for entry in test_entries
                ):
                    raise ToolError(
                        "INVALID_ARTIFACT",
                        "test-runner returned an invalid formal test list.",
                    )
                for entry in test_entries:
                    if entry.get("browser_opened") is True:
                        validate_runtime_evidence(
                            entry.get("runtime_identity"),
                            run,
                        )
        else:
            raise ToolError(
                "INVALID_ARTIFACT",
                "test-runner did not return structured runtime-bound results.",
            )

        canonical_tests = (game_dir / ".playtest" / layout.TESTS_DIR).resolve()
        selectors = {
            key: args.get(key)
            for key in ("filter", "id", "changed")
            if args.get(key) is not None
        }
        full_suite = not solo and tests == canonical_tests and not selectors
        scope = {
            "mode": "solo" if solo else "formal",
            "full_suite": full_suite,
            "tests": _rel(game_dir, tests) if tests is not None else None,
            "selectors": selectors,
            "url": url,
        }
        result_rel = (
            layout.TEST_RESULTS
            if full_suite
            else (
                f"{layout.TEST_RUN_DIR}/solo-results.json"
                if solo
                else f"{layout.TEST_RUN_DIR}/scoped-test-results.json"
            )
        )
        result_path = _save_test_results(
            game_dir,
            proc,
            result_rel=result_rel,
            scope=scope,
            identity=identity,
        )
        return {
            "returncode": proc["returncode"],
            "stdout": proc["stdout"],
            "stderr_tail": proc["stderr"][-1000:],
            "result_path": result_path,
            "scope": scope,
            "runtime_identity": parsed["runtime_identity"],
            **identity,
        }


# -------------------- play_game (intelligent play-agent) --------------------


def _play_game(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    script = runner_path("verify", "play-agent.mjs")
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        run = _require_verify_run(
            game_dir,
            ctx,
            args.get("verify_run_id"),
        )
        identity = identity_from_run(run)
        url = _require_epoch_url(args, run)
        play_dir = _require_canonical_path(
            game_dir,
            args.get("out"),
            field="out",
            canonical_rel=f".playtest/{layout.PLAY_DIR}",
        )
        max_wall = int(
            args["max_wall"]
            if args.get("max_wall") is not None
            else ctx.config.verify.play_max_wall_seconds
        )
        timeout_seconds = float(
            args.get(
                "timeout_seconds",
                ctx.config.verify.play_game_timeout_seconds,
            )
        )
        if max_wall < 30:
            raise ToolError(
                "INVALID_CONFIG",
                "play max_wall must be at least 30 seconds.",
            )
        if timeout_seconds < max_wall + PLAY_WATCHDOG_GRACE_SECONDS:
            raise ToolError(
                "INVALID_CONFIG",
                "play timeout_seconds must be at least max_wall + "
                f"{PLAY_WATCHDOG_GRACE_SECONDS} seconds so the Node watchdog "
                "can seal runtime-bound partial evidence.",
            )
        cli: list[str] = [
            "--url",
            url,
            "--game",
            str(game_dir),
            "--out",
            str(play_dir),
            *runtime_cli_args(game_dir, run),
            "--max-wall",
            str(max_wall),
        ]

        for key in (
            "policy",
            "model",
            "provider",
            "changed",
            "max_steps",
            "seeds",
            "action_delay",
        ):
            value = args.get(key)
            if value is not None:
                cli.extend([f"--{key.replace('_', '-')}", str(value)])
        for key in ("control_map", "objectives"):
            if args.get(key) is None:
                continue
            value = _resolve_workspace_path(
                game_dir,
                args[key],
                field=key,
                allowed_roots=(f".playtest/{layout.INPUTS_DIR}",),
                must_exist=True,
            )
            cli.extend([f"--{key.replace('_', '-')}", str(value)])

        for flag in ("vision_only", "realtime", "headed", "record"):
            if flag == "record" and ctx.config.verify.skip_video:
                continue
            if args.get(flag):
                cli.append(f"--{flag.replace('_', '-')}")

        proc = run_node_script(
            script,
            cli,
            cwd=game_dir,
            timeout=timeout_seconds,
            check=False,
        )

        report_path = game_dir / ".playtest" / layout.PLAY_REPORT
        report: dict[str, Any] | None = None
        if report_path.is_file():
            payload = _read_json(report_path)
            if not isinstance(payload, dict):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "play-agent report is not a JSON object.",
                )
            validate_runtime_evidence(payload.get("runtime_identity"), run)
            try:
                report = stamp_json_file(report_path, identity)
            except (OSError, ValueError) as exc:
                raise ToolError("INVALID_ARTIFACT", str(exc)) from exc
        else:
            trajectory_hint = ""
            traj_path = game_dir / ".playtest" / layout.PLAY_DIR / "trajectory.jsonl"
            if traj_path.is_file():
                try:
                    lines = traj_path.read_text().strip().splitlines()
                    tail = lines[-5:] if len(lines) > 5 else lines
                    trajectory_hint = (
                        f" Trajectory has {len(lines)} step(s); "
                        f"last entries: {'; '.join(tail)}"
                    )
                except (OSError, UnicodeError):
                    trajectory_hint = ""
            raise ToolError(
                "MISSING_ARTIFACT",
                f"play_game did not produce its canonical agent-play-report.json "
                f"(process may have been killed by watchdog timeout or external signal)."
                f"{trajectory_hint}",
            )
        history_path = game_dir / ".playtest" / layout.PLAY_HISTORY
        if history_path.is_file():
            try:
                stamp_jsonl_last(history_path, identity)
            except ValueError as exc:
                raise ToolError("INVALID_ARTIFACT", str(exc)) from exc

        out: dict[str, Any] = {
            "returncode": proc["returncode"],
            "blocked": proc["returncode"] != 0,
            "report_path": _rel(game_dir, report_path),
            "summary": proc["stderr"][-2000:],
            "runtime_identity": report["runtime_identity"],
            **identity,
        }
        for key in (
            "build_health",
            "weighted_accuracy",
            "objective_scores",
            "coverage",
            "regressions",
            "bugs",
        ):
            out[key] = report.get(key)
        return out


def _materialize_gameplay_alignment(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Convert the current play-agent result into the deterministic gameplay axis."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        run = _require_verify_run(
            game_dir,
            ctx,
            args.get("verify_run_id"),
        )
        identity = identity_from_run(run)
        report_path = game_dir / ".playtest" / layout.PLAY_REPORT
        report = _read_json(report_path)
        if not isinstance(report, dict) or not _matches_identity(report, identity):
            raise ToolError(
                "MISSING_ARTIFACT",
                "Current play-agent report is missing or belongs to another verify run.",
            )
        validate_runtime_evidence(report.get("runtime_identity"), run)

        threshold = float(ctx.config.gates.play_quality_min_accuracy)
        weighted_accuracy = report.get("weighted_accuracy")
        build_health = report.get("build_health")
        regressions = report.get("regressions")
        bugs = report.get("bugs")
        issues: list[dict[str, str]] = []
        if not isinstance(build_health, dict) or build_health.get("pass") is not True:
            issues.append(
                {
                    "severity": "high",
                    "description": "Play-agent build health failed",
                    "root_cause": "See .playtest/verify/play/agent-play-report.json",
                }
            )
        if not isinstance(weighted_accuracy, (int, float)):
            issues.append(
                {
                    "severity": "high",
                    "description": "Play-agent produced no verified objective accuracy",
                    "root_cause": "Use executable success predicates in objectives.json",
                }
            )
        elif float(weighted_accuracy) < threshold:
            issues.append(
                {
                    "severity": "high",
                    "description": (
                        f"Play objective accuracy {float(weighted_accuracy):.3f} "
                        f"is below the configured {threshold:.3f} threshold"
                    ),
                    "root_cause": "One or more GDD-derived play objectives did not pass",
                }
            )
        if isinstance(regressions, list) and regressions:
            issues.append(
                {
                    "severity": "high",
                    "description": f"{len(regressions)} gameplay objective regression(s)",
                    "root_cause": "Current objective scores regressed from play history",
                }
            )
        blocking_bugs = [
            bug
            for bug in bugs or []
            if isinstance(bug, dict)
            and (bug.get("severity") == "crash" or bug.get("source") == "judge")
        ]
        for bug in blocking_bugs[:5]:
            issues.append(
                {
                    "severity": "high",
                    "description": str(bug.get("message") or "Blocking play-agent bug"),
                    "root_cause": "Play-agent oracle/judge reported a gameplay defect",
                }
            )

        objective_scores = report.get("objective_scores")
        output_path = game_dir / ".playtest" / layout.GAMEPLAY_ALIGNMENT
        payload = {
            "schema_version": 1,
            "producer": "gca-mcp.materialize_gameplay_alignment",
            "generated_at": _utc_now(),
            **identity,
            "axis": "gameplay",
            "applicable": True,
            "aligned": not issues,
            "checked": (
                [
                    {
                        "name": item.get("id"),
                        "operation": item.get("operation"),
                        "score": item.get("score"),
                        "match": (
                            isinstance(item.get("score"), (int, float))
                            and float(item["score"]) >= threshold
                        ),
                    }
                    for item in objective_scores
                    if isinstance(item, dict)
                ]
                if isinstance(objective_scores, list)
                else []
            ),
            "issues": issues,
            "play_report": _rel(game_dir, report_path),
            "weighted_accuracy": weighted_accuracy,
            "minimum_accuracy": threshold,
            "runtime_identity": report["runtime_identity"],
        }
        atomic_write_json(output_path, payload)
        return {
            **identity,
            "output": _rel(game_dir, output_path),
            "aligned": payload["aligned"],
            "blocked": not payload["aligned"],
            "issues": issues,
        }


def _materialize_physics_alignment(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Project deterministic body/art interaction checks into Axis 4."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        run = _require_verify_run(
            game_dir,
            ctx,
            args.get("verify_run_id"),
        )
        identity = identity_from_run(run)
        applicable = run.get("interaction_profile") != "none"
        issues: list[dict[str, str]] = []
        gate_result: dict[str, Any] | None = None
        if applicable:
            gate_result = _gate_check(
                {
                    "game_dir": str(game_dir),
                    "gate": "interaction_contract",
                },
                ctx,
            )
            for failure in gate_result.get("failures") or []:
                if not isinstance(failure, dict):
                    continue
                issues.append(
                    {
                        "severity": "high",
                        "description": str(
                            failure.get("message")
                            or failure.get("description")
                            or failure.get("check")
                            or "Physics interaction contract failed"
                        ),
                        "root_cause": str(
                            failure.get("check")
                            or "Deterministic body-to-art interaction check"
                        ),
                    }
                )
            if gate_result.get("blocked") is True and not issues:
                issues.append(
                    {
                        "severity": "high",
                        "description": "Physics interaction contract failed",
                        "root_cause": (
                            str(gate_result.get("stderr") or "").strip()
                            or "See the interaction_contract gate result"
                        ),
                    }
                )

        output_path = canonical_playtest_path(game_dir, layout.PHYSICS_ALIGNMENT)
        payload = {
            "schema_version": 1,
            "producer": "gca-mcp.materialize_physics_alignment",
            "generated_at": _utc_now(),
            **identity,
            "axis": "physics",
            "applicable": applicable,
            "aligned": not issues if applicable else None,
            "status": "passed" if applicable and not issues else (
                "failed" if applicable else "not_applicable"
            ),
            "evidence_mode": "deterministic_interaction_contract",
            "issues": issues,
            "interaction_gate_result": (
                gate_result.get("result_path")
                if isinstance(gate_result, dict)
                else None
            ),
            "runtime_asset_receipt": (
                f".playtest/{layout.RUNTIME_ASSETS_RECEIPT}"
                if canonical_playtest_path(
                    game_dir,
                    layout.RUNTIME_ASSETS_RECEIPT,
                ).is_file()
                else None
            ),
            "play_report": (
                f".playtest/{layout.PLAY_REPORT}"
                if canonical_playtest_path(game_dir, layout.PLAY_REPORT).is_file()
                else None
            ),
        }
        atomic_write_json(output_path, payload)
        return {
            **identity,
            "output": _rel(game_dir, output_path),
            "applicable": applicable,
            "aligned": payload["aligned"],
            "blocked": bool(issues),
            "issues": issues,
        }


# -------------------- visual_diff (element-visual-diff.mjs) --------------------


def _visual_diff(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Comprehensive visual verification (Axis 1 + Axis 3).

    Wraps the bundled ``element-visual-diff.mjs`` so the agent never has to
    resolve the runner path by hand (the old ``node <runners>/…`` shell call was
    the source of "scripts not present → hand-author a fake report"). It clips
    each rendered UI element by its measured ``box_px``, pairs it 1:1 with its
    concept crop (paired VLM), pairs each running screen with its mockup, and
    writes the source-tagged alignment files + the evidence-backed
    ``verify_middle/visual/visual-diff-report.json`` sidecar that
    ``gateVisualDiff`` requires. Never hand-author those files.
    """
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        return _visual_diff_active(args, ctx, game_dir)


def _visual_diff_active(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    script = runner_path("verify", "element-visual-diff.mjs")
    run = _require_verify_run(game_dir, ctx, args.get("verify_run_id"))
    identity = identity_from_run(run)
    url = _require_epoch_url(args, run)
    cli: list[str] = [
        "--game",
        str(game_dir),
        "--url",
        url,
        "--config",
        str(ctx.repo_root / "gca.config.yaml"),
        *runtime_cli_args(game_dir, run),
    ]

    for key in ("map", "round", "width", "height", "settle"):
        v = args.get(key)
        if v is not None:
            cli.extend([f"--{key}", str(v)])
    if args.get("headed"):
        cli.append("--headed")

    report_path = game_dir / ".playtest" / layout.VISUAL_DIFF_REPORT
    previous_report = _read_json(report_path) if report_path.is_file() else None
    previous_run_id = (
        previous_report.get("run_id") if isinstance(previous_report, dict) else None
    )
    started_at = time.time()
    proc = run_node_script(
        script,
        cli,
        cwd=game_dir,
        timeout=float(args.get("timeout_seconds", 1800)),
        check=False,
    )

    report = _read_json(report_path) if report_path.is_file() else None
    current_report = (
        isinstance(report, dict)
        and bool(report.get("run_id"))
        and report.get("run_id") != previous_run_id
        and report_path.stat().st_mtime >= started_at - 1.0
    )
    if current_report:
        validate_runtime_evidence(report.get("runtime_identity"), run)
        report = stamp_json_file(report_path, identity)
        history_path = game_dir / ".playtest" / layout.VISUAL_DIFF_HISTORY
        if history_path.is_file():
            try:
                stamp_jsonl_last(
                    history_path,
                    identity,
                    run_id=report.get("run_id"),
                )
            except ValueError as exc:
                raise ToolError("INVALID_ARTIFACT", str(exc)) from exc
        for rel in (layout.VISUAL_ALIGNMENT, layout.ELEMENT_ALIGNMENT):
            artifact_path = game_dir / ".playtest" / rel
            artifact = _read_json(artifact_path)
            if artifact_path.is_file():
                validate_runtime_evidence(
                    artifact.get("runtime_identity")
                    if isinstance(artifact, dict)
                    else None,
                    run,
                )
                _stamp_if_present(artifact_path, identity)
    out: dict[str, Any] = {
        "returncode": proc["returncode"],
        "blocked": proc["returncode"] != 0 or not current_report,
        "report_path": _rel(game_dir, report_path),
        "stdout_tail": proc["stdout"][-2000:],
        "stderr_tail": proc["stderr"][-2000:],
        **identity,
    }
    if isinstance(report, dict) and current_report:
        for key in (
            "applicable",
            "status",
            "aligned",
            "global_aligned",
            "element_aligned",
            "round",
            "max_rounds",
            "max_attempts",
            "unavailable_policy",
            "unavailable_count",
            "run_id",
            "scene_screen_map",
        ):
            if key in report:
                out[key] = report[key]
    else:
        out["report_missing"] = not report_path.is_file()
        out["report_stale_or_reused"] = report_path.is_file()
    return out


# -------------------- capture_layout (capture-layout.mjs) --------------------


def _capture_layout(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Deterministic layout bounds capture (Axis 5).

    Wraps the bundled ``capture-layout.mjs`` so the agent never resolves the
    runner path by hand. It reads ``layout-contract.json``, starts each scene on
    the running game, walks the display list, and dumps every named object's
    center/size to ``verify_middle/layout/<scene>.rendered.json`` — the MEASURED
    side ``gateLayoutAlignment`` consumes.
    """
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        return _capture_layout_active(args, ctx, game_dir)


def _capture_layout_active(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    script = runner_path("verify", "capture-layout.mjs")
    run = _require_verify_run(game_dir, ctx, args.get("verify_run_id"))
    identity = identity_from_run(run)
    url = _require_epoch_url(args, run)
    contract_path = _require_canonical_path(
        game_dir,
        args.get("contract"),
        field="contract",
        canonical_rel=f".playtest/{layout.LAYOUT_CONTRACT}",
    )
    rendered_dir = _require_canonical_path(
        game_dir,
        args.get("output"),
        field="output",
        canonical_rel=f".playtest/{layout.LAYOUT_RENDERED_DIR}",
    )
    cli: list[str] = [
        "--game",
        str(game_dir),
        "--url",
        url,
        "--contract",
        str(contract_path),
        "--output",
        str(rendered_dir),
        *runtime_cli_args(game_dir, run),
    ]

    for key in ("width", "height", "settle"):
        v = args.get(key)
        if v is not None:
            cli.extend([f"--{key}", str(v)])
    if args.get("headed"):
        cli.append("--headed")

    manifest_path = rendered_dir / "_run.json"
    previous_manifest = _read_json(manifest_path) if manifest_path.is_file() else None
    previous_run_id = (
        previous_manifest.get("run_id") if isinstance(previous_manifest, dict) else None
    )
    proc = run_node_script(
        script,
        cli,
        cwd=game_dir,
        timeout=float(args.get("timeout_seconds", 600)),
        check=False,
    )

    manifest = _read_json(manifest_path) if manifest_path.is_file() else None
    current_run = (
        isinstance(manifest, dict)
        and bool(manifest.get("run_id"))
        and manifest.get("run_id") != previous_run_id
    )
    if current_run:
        validate_runtime_evidence(manifest.get("runtime_identity"), run)
        manifest = stamp_json_file(manifest_path, identity)
        for rendered_path in rendered_dir.glob("*.rendered.json"):
            rendered = _read_json(rendered_path)
            validate_runtime_evidence(
                rendered.get("runtime_identity")
                if isinstance(rendered, dict)
                else None,
                run,
            )
            stamp_json_file(rendered_path, identity)
    captured = (
        [_rel(game_dir, p) for p in sorted(rendered_dir.glob("*.rendered.json"))]
        if rendered_dir.is_dir()
        else []
    )
    return {
        "returncode": proc["returncode"],
        "blocked": proc["returncode"] != 0 or not current_run,
        "applicable": bool(captured),
        "run_id": manifest.get("run_id") if current_run else None,
        "result_path": _rel(game_dir, manifest_path) if current_run else None,
        "rendered_dir": _rel(game_dir, rendered_dir),
        "captured_scenes": captured,
        "stdout_tail": proc["stdout"][-2000:],
        "stderr_tail": proc["stderr"][-2000:],
        **identity,
    }


# -------------------- capture_runtime_assets --------------------


_RUNTIME_UUID_TEXTURE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _runtime_texture_binding_key(binding: dict[str, Any]) -> tuple[Any, ...]:
    return (
        binding["scene"],
        binding["texture_key"],
        binding["object_type"],
        binding["name"] or "",
        binding["layout_key"] or "",
        binding["bind"] or "",
        binding["instance"],
        binding["visible"],
    )


def _normalize_runtime_texture_binding(
    value: Any,
    expected_scene: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(
            "INVALID_ARTIFACT",
            "Runtime texture bindings must be objects.",
        )
    scene = value.get("scene")
    texture_key = value.get("texture_key")
    object_type = value.get("object_type")
    instance = value.get("instance")
    visible = value.get("visible")
    optional = {field: value.get(field) for field in ("name", "layout_key", "bind")}
    if (
        not isinstance(scene, str)
        or not scene
        or (expected_scene is not None and scene != expected_scene)
        or not isinstance(texture_key, str)
        or not texture_key
        or not isinstance(object_type, str)
        or not object_type
        or isinstance(instance, bool)
        or not isinstance(instance, int)
        or instance < 0
        or visible is not True
        or any(
            item is not None and (not isinstance(item, str) or not item)
            for item in optional.values()
        )
    ):
        raise ToolError(
            "INVALID_ARTIFACT",
            "capture-runtime-assets returned an invalid display-object "
            "texture binding.",
        )
    return {
        "scene": scene,
        "texture_key": texture_key,
        "object_type": object_type,
        **optional,
        "instance": instance,
        "visible": True,
    }


def _canonical_runtime_texture_bindings(
    values: Any,
    expected_scene: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ToolError(
            "INVALID_ARTIFACT",
            "Runtime display-object texture bindings must be an array.",
        )
    normalized = [
        _normalize_runtime_texture_binding(value, expected_scene) for value in values
    ]
    keys = [_runtime_texture_binding_key(binding) for binding in normalized]
    if len(set(keys)) != len(keys):
        raise ToolError(
            "INVALID_ARTIFACT",
            "Runtime display-object texture bindings contain duplicate rows.",
        )
    return sorted(normalized, key=_runtime_texture_binding_key)


def _validate_runtime_texture_evidence(
    payload: dict[str, Any],
) -> tuple[dict[str, bool], list[dict[str, Any]], list[dict[str, Any]]]:
    game_config = payload.get("game_config")
    if (
        not isinstance(game_config, dict)
        or set(game_config) != {"pixelArt"}
        or not isinstance(game_config.get("pixelArt"), bool)
    ):
        raise ToolError(
            "INVALID_ARTIFACT",
            "capture-runtime-assets did not report boolean game.config.pixelArt.",
        )
    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        raise ToolError(
            "INVALID_ARTIFACT",
            "capture-runtime-assets returned invalid per-scene evidence.",
        )
    normalized_scenes: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []
    for scene in scenes:
        if (
            not isinstance(scene, dict)
            or not isinstance(scene.get("scene"), str)
            or not scene["scene"]
            or not isinstance(scene.get("reachable"), bool)
            or not isinstance(scene.get("texture_keys"), list)
            or any(not isinstance(key, str) or not key for key in scene["texture_keys"])
        ):
            raise ToolError(
                "INVALID_ARTIFACT",
                "capture-runtime-assets returned invalid per-scene evidence.",
            )
        bindings = _canonical_runtime_texture_bindings(
            scene.get("texture_bindings"),
            scene["scene"],
        )
        derived_keys = sorted({binding["texture_key"] for binding in bindings})
        if sorted(set(scene["texture_keys"])) != derived_keys:
            raise ToolError(
                "INVALID_ARTIFACT",
                "Runtime scene texture keys disagree with display-object bindings.",
            )
        flattened.extend(bindings)
        normalized_scenes.append(
            {
                **scene,
                "texture_keys": derived_keys,
                "texture_bindings": bindings,
            }
        )
    flattened = sorted(flattened, key=_runtime_texture_binding_key)
    if len({_runtime_texture_binding_key(row) for row in flattened}) != len(flattened):
        raise ToolError(
            "INVALID_ARTIFACT",
            "Runtime display-object texture bindings are duplicated across scenes.",
        )
    top_level = _canonical_runtime_texture_bindings(
        payload.get("display_object_texture_bindings"),
    )
    if top_level != flattened:
        raise ToolError(
            "INVALID_ARTIFACT",
            "Top-level runtime texture bindings disagree with per-scene evidence.",
        )
    displayed = payload.get("display_list_texture_keys")
    derived_displayed = sorted({row["texture_key"] for row in flattened})
    if (
        not isinstance(displayed, list)
        or any(not isinstance(key, str) or not key for key in displayed)
        or sorted(set(displayed)) != derived_displayed
    ):
        raise ToolError(
            "INVALID_ARTIFACT",
            "Runtime display texture keys disagree with display-object bindings.",
        )
    return (
        {"pixelArt": game_config["pixelArt"]},
        normalized_scenes,
        flattened,
    )


def _sprite_action_requires_animation(
    game_dir: Path,
    owner: str,
    action: str,
    spec: dict[str, Any],
) -> bool:
    metadata_path = _require_runtime_manifest_path(
        game_dir,
        spec.get("metadata"),
        label=f"Sprite {owner!r} action {action!r} metadata",
        must_exist=True,
    )
    metadata = _read_json(metadata_path)
    frame_count = metadata.get("frameCount") if isinstance(metadata, dict) else None
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count < 1
    ):
        raise ToolError(
            "INVALID_ARTIFACT",
            f"Sprite {owner!r} action {action!r} metadata must declare a "
            "positive integer frameCount.",
        )
    return frame_count > 1


def _capture_runtime_assets(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Capture actual Phaser texture loading/use under the current runtime token."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        run = _require_verify_run(game_dir, ctx, args.get("verify_run_id"))
        identity = identity_from_run(run)
        url = _require_epoch_url(args, run)
        script = runner_path("verify", "capture-runtime-assets.mjs")
        cli = [
            "--url",
            url,
            *runtime_cli_args(game_dir, run),
        ]
        for key in ("width", "height", "settle"):
            if args.get(key) is not None:
                cli.extend([f"--{key}", str(args[key])])
        if args.get("headed"):
            cli.append("--headed")
        proc = run_node_script(
            script,
            cli,
            cwd=game_dir,
            timeout=float(args.get("timeout_seconds", 600)),
            check=False,
        )
        if proc["returncode"] != 0:
            raise ToolError(
                "RUNTIME_CAPTURE_FAILED",
                "capture-runtime-assets failed.",
                hint=proc["stderr"][-1000:],
            )
        captured = _parse_json_output(proc.get("stdout", ""))
        if not isinstance(captured, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                "capture-runtime-assets did not return a JSON object.",
            )
        runtime_identity = validate_runtime_evidence(
            captured.get("runtime_identity"),
            run,
        )
        key_fields = (
            "loaded_texture_keys",
            "animation_frame_texture_keys",
            "display_list_texture_keys",
            "active_scene_keys",
            "registered_scene_keys",
            "reachable_scene_keys",
        )
        for field in key_fields:
            if not isinstance(captured.get(field), list) or any(
                not isinstance(value, str) for value in captured[field]
            ):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    f"capture-runtime-assets returned invalid `{field}`.",
                )
        game_config, scenes, display_bindings = _validate_runtime_texture_evidence(
            captured
        )
        registered_scenes = set(captured["registered_scene_keys"])
        reachable_scenes = set(captured["reachable_scene_keys"])
        scene_names = {scene["scene"] for scene in scenes}
        derived_reachable = {scene["scene"] for scene in scenes if scene["reachable"]}
        if (
            scene_names != registered_scenes
            or len(scene_names) != len(scenes)
            or reachable_scenes != derived_reachable
            or not set(captured["active_scene_keys"]).issubset(registered_scenes)
        ):
            raise ToolError(
                "INVALID_ARTIFACT",
                "Runtime scene registry and per-scene evidence disagree.",
            )

        manifest_path = game_dir / "assets" / "asset-manifest.json"
        manifest = _read_json(manifest_path)
        entries = manifest_entries(manifest)
        valid_manifest_shape = isinstance(manifest, list) or (
            isinstance(manifest, dict)
            and (
                any(
                    isinstance(manifest.get(field), list)
                    for field in ("assets", "entries", "items")
                )
                or bool(entries)
            )
        )
        if not valid_manifest_shape or not manifest_path.is_file():
            raise ToolError(
                "INVALID_ARTIFACT",
                "asset-manifest.json must contain asset entries.",
            )
        loaded = set(captured["loaded_texture_keys"])
        animated = set(captured["animation_frame_texture_keys"])
        displayed = set(captured["display_list_texture_keys"])
        manifest_assets: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Every asset-manifest entry must be an object with a key.",
                )
            declared = {entry["key"]}
            specs = [entry]
            actions = entry.get("actions")
            action_contracts: list[tuple[str, bool]] = []
            if isinstance(actions, dict):
                if not actions:
                    raise ToolError(
                        "INVALID_ARTIFACT",
                        f"Sprite {entry['key']!r} actions must not be empty.",
                    )
                _require_runtime_manifest_path(
                    game_dir,
                    entry.get("path"),
                    label=f"Sprite {entry['key']!r} default runtime file",
                )
                _require_runtime_manifest_path(
                    game_dir,
                    entry.get("metadata"),
                    label=f"Sprite {entry['key']!r} default metadata",
                )
                for action, value in actions.items():
                    if not isinstance(value, dict):
                        raise ToolError(
                            "INVALID_ARTIFACT",
                            f"Sprite {entry['key']!r} action {action!r} must be "
                            "an object.",
                        )
                    texture_key = value.get("textureKey")
                    if not isinstance(texture_key, str) or not texture_key:
                        raise ToolError(
                            "INVALID_ARTIFACT",
                            f"Sprite {entry['key']!r} action {action!r} is "
                            "missing textureKey.",
                        )
                    _require_runtime_manifest_path(
                        game_dir,
                        value.get("path"),
                        label=(
                            f"Sprite {entry['key']!r} action {action!r} runtime file"
                        ),
                    )
                    action_contracts.append(
                        (
                            texture_key,
                            _sprite_action_requires_animation(
                                game_dir,
                                entry["key"],
                                str(action),
                                value,
                            ),
                        )
                    )
                    specs.append(value)
            else:
                _require_runtime_manifest_path(
                    game_dir,
                    entry.get("path"),
                    label=f"Asset {entry['key']!r} runtime file",
                )
                metadata_value = entry.get("metadata")
                if metadata_value is not None:
                    _require_runtime_manifest_path(
                        game_dir,
                        metadata_value,
                        label=f"Asset {entry['key']!r} metadata",
                    )
            for spec in specs:
                texture_key = spec.get("textureKey")
                if isinstance(texture_key, str) and texture_key:
                    declared.add(texture_key)
            loaded_state = bool(declared & loaded)
            used_state = bool(declared & (animated | displayed))
            if action_contracts:
                loaded_state = all(
                    texture_key in loaded for texture_key, _required in action_contracts
                )
                used_state = loaded_state and all(
                    not animation_required or texture_key in animated
                    for texture_key, animation_required in action_contracts
                )
            manifest_assets.append(
                {
                    "key": entry["key"],
                    "declared_texture_keys": sorted(declared),
                    "loaded_texture_keys": sorted(declared & loaded),
                    "animation_frame_texture_keys": sorted(declared & animated),
                    "display_list_texture_keys": sorted(declared & displayed),
                    "loaded": loaded_state,
                    "used": used_state,
                }
            )

        output_path = game_dir / ".playtest" / layout.RUNTIME_ASSETS_RECEIPT
        receipt = {
            "schema_version": 1,
            "producer": "gca-mcp.capture_runtime_assets",
            "generated_at": _utc_now(),
            **identity,
            "url": url,
            "runtime_token_sha256": run["runtime_token_sha256"],
            "runtime_receipt_sha256": run["runtime_receipt_sha256"],
            "runtime_identity": runtime_identity,
            "manifest": _rel(game_dir, manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "loaded_texture_keys": sorted(loaded),
            "animation_frame_texture_keys": sorted(animated),
            "display_list_texture_keys": sorted(displayed),
            "display_object_texture_bindings": display_bindings,
            "game_config": game_config,
            "active_scene_keys": sorted(set(captured["active_scene_keys"])),
            "registered_scene_keys": sorted(set(captured["registered_scene_keys"])),
            "reachable_scene_keys": sorted(set(captured["reachable_scene_keys"])),
            "scenes": scenes,
            "manifest_assets": manifest_assets,
            "summary": {
                "loaded_texture_count": len(loaded),
                "animation_texture_count": len(animated),
                "display_texture_count": len(displayed),
                "display_object_texture_binding_count": len(display_bindings),
                "runtime_uuid_texture_count": sum(
                    1 for key in displayed if _RUNTIME_UUID_TEXTURE_RE.fullmatch(key)
                ),
                "registered_scene_count": len(set(captured["registered_scene_keys"])),
                "reachable_scene_count": len(set(captured["reachable_scene_keys"])),
                "manifest_asset_count": len(manifest_assets),
                "manifest_loaded_count": sum(
                    item["loaded"] for item in manifest_assets
                ),
                "manifest_used_count": sum(item["used"] for item in manifest_assets),
            },
        }
        atomic_write_json(output_path, receipt)
        return {
            **identity,
            "url": url,
            "receipt": _rel(game_dir, output_path),
            "summary": receipt["summary"],
            "runtime_identity": runtime_identity,
        }


# -------------------- materialize_layout_alignment --------------------


def _materialize_layout_alignment(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Non-blocking layout alignment materializer (Axis 5).

    Reuses the bundled layout evaluator to convert capture_layout's rendered
    bounds dumps into ``verify/alignment/layout/alignment.json`` without making
    a pipeline gate decision. The returned ``blocked`` flag summarizes the
    evaluator's blocking messages for report status, but the tool itself exits
    successfully so verify-agent can always continue to write_verify_report.
    """
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=False):
        return _materialize_layout_alignment_active(args, ctx, game_dir)


def _materialize_layout_alignment_active(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    run = _require_verify_run(game_dir, ctx, args.get("verify_run_id"))
    identity = identity_from_run(run)
    script = runner_path("verify", "gate-check.mjs")
    proc = run_node_script(
        script,
        [
            "--game",
            str(game_dir),
            "--gate",
            "materialize_layout_alignment",
            "--config",
            str(ctx.repo_root / "gca.config.yaml"),
        ],
        cwd=game_dir,
        timeout=float(args.get("timeout_seconds", 300)),
        check=False,
    )
    parsed = _parse_json_output(proc.get("stdout", ""))
    alignment_path = game_dir / ".playtest" / layout.LAYOUT_ALIGNMENT
    out: dict[str, Any] = {
        "returncode": proc["returncode"],
        "blocked": proc["returncode"] != 0,
        "alignment_path": _rel(game_dir, alignment_path),
        "stdout_tail": proc["stdout"][-2000:],
        "stderr_tail": proc["stderr"][-2000:],
        **identity,
    }
    if isinstance(parsed, dict):
        out.update(parsed)
        out["blocked"] = (
            bool(parsed.get("blocked"))
            or parsed.get("wrote") is not True
            or proc["returncode"] != 0
        )
    else:
        out["blocked"] = True
        out["parse_error"] = True
    if (
        alignment_path.is_file()
        and isinstance(parsed, dict)
        and parsed.get("wrote") is True
    ):
        alignment = _read_json(alignment_path)
        if isinstance(alignment, dict):
            alignment = stamp_json_file(alignment_path, identity)
            for key in (
                "applicable",
                "aligned",
                "status",
                "coverage",
                "reason",
                "run_id",
            ):
                if key in alignment:
                    out[key] = alignment[key]
            out["issue_count"] = len(alignment.get("issues") or [])
    else:
        out["alignment_missing"] = not alignment_path.is_file()
        out["alignment_not_current"] = True
        out["blocked"] = True
    return out


# -------------------- run_verify_dag --------------------


def _verify_scenarios(game_dir: Path, requested: Any) -> list[dict[str, Any]]:
    interactions = game_dir / ".playtest" / layout.INTERACTIONS_DIR
    seeds = game_dir / ".playtest" / layout.SCENARIOS_DIR
    requested_names = (
        [str(item) for item in requested] if isinstance(requested, list) else []
    )
    if len(requested_names) != len(set(requested_names)):
        raise ToolError(
            "INVALID_CONFIG",
            "Verify scenario names must be unique.",
        )
    discovered = (
        {
            path.stem: path
            for path in sorted(interactions.glob("*.mjs"))
            if path.is_file()
        }
        if interactions.is_dir()
        else {}
    )
    unsafe = [
        name for name in discovered if _VERIFY_SCENARIO_RE.fullmatch(name) is None
    ]
    if unsafe:
        raise ToolError(
            "INVALID_CONFIG",
            "Verify scenario filenames must use lowercase slug names "
            f"([a-z0-9_-]): {', '.join(unsafe)}.",
        )
    if requested_names:
        missing = [name for name in requested_names if name not in discovered]
        if missing:
            raise ToolError(
                "INPUT_FILE_NOT_FOUND",
                f"Unknown verify scenario(s): {', '.join(missing)}.",
            )
        names = requested_names
    elif discovered:
        names = list(discovered)
    else:
        legacy = game_dir / ".playtest" / layout.INTERACT_SCRIPT
        return [
            {
                "name": "default",
                "script": _rel(game_dir, legacy) if legacy.is_file() else None,
                "seed": None,
            }
        ]
    return [
        {
            "name": name,
            "script": _rel(game_dir, discovered[name]),
            "seed": (
                _rel(game_dir, seeds / f"{name}.json")
                if (seeds / f"{name}.json").is_file()
                else None
            ),
        }
        for name in names
    ]


def _run_verify_dag(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    """Run one resumable verification epoch as a checkpointed parallel DAG."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_dag_flock(game_dir):
        return _run_verify_dag_locked(args, ctx, game_dir)


def _run_verify_dag_locked(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    """Run the DAG while holding its cross-process single-flight lock."""
    mode = args.get("mode")
    if mode not in {"iterate", "full", "full-novideo"}:
        raise ToolError(
            "INVALID_VERIFY_MODE",
            "run_verify_dag mode must be iterate, full, or full-novideo.",
        )
    url = normalize_epoch_url(args.get("url"))
    no_video = mode in {"iterate", "full-novideo"} or ctx.config.verify.skip_video
    scenarios = [] if no_video else _verify_scenarios(game_dir, args.get("scenarios"))

    supplied_run_id = args.get("verify_run_id")
    if supplied_run_id:
        run = _require_verify_run(
            game_dir,
            ctx,
            supplied_run_id,
            allowed_statuses={"active", "finalized"},
        )
        if run.get("mode") != mode:
            raise ToolError(
                "VERIFY_MODE_MISMATCH",
                "run_verify_dag mode must match the active verify epoch.",
            )
        if run.get("url") != url:
            raise ToolError(
                "VERIFY_URL_MISMATCH",
                "run_verify_dag URL must match the selected verify epoch.",
            )
        if run.get("status") == "finalized":
            report_path = canonical_playtest_path(game_dir, layout.VERIFY_REPORT)
            report = _read_json(report_path)
            identity = identity_from_run(run)
            if (
                not _matches_identity(report, identity)
                or not report_path.is_file()
                or run.get("report") != _rel(game_dir, report_path)
                or run.get("report_sha256") != sha256_file(report_path)
            ):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Finalized verify epoch does not have a valid identity-bound report.",
                )
            checkpoints = load_checkpoints(game_dir, run)
            producer_results = {
                str(checkpoint.get("checkpoint_name")): {
                    "status": checkpoint.get("status"),
                    "checkpoint": checkpoint.get("path"),
                    **(
                        {"result": checkpoint.get("result")}
                        if checkpoint.get("status") == "completed"
                        else {"error": checkpoint.get("error")}
                    ),
                }
                for checkpoint in checkpoints
            }
            gate_result = _gate_check(
                {
                    "game_dir": str(game_dir),
                    "gate": args.get("gate") or "verify_report",
                    "verify_run_id": run["verify_run_id"],
                },
                ctx,
            )
            return {
                **identity,
                "mode": mode,
                "url": run["url"],
                "resumed": True,
                "replayed_finalized": True,
                "status": report.get("status"),
                "run_status": "finalized",
                "producer_results": producer_results,
                "verify_report": {
                    "output": _rel(game_dir, report_path),
                    **identity,
                    "status": report.get("status"),
                    "run_status": "finalized",
                    "report_sha256": run["report_sha256"],
                    "summary": report.get("summary"),
                    "issue_count": len(report.get("issues") or []),
                },
                "gate": gate_result,
            }
        begin_result = {
            **identity_from_run(run),
            "mode": mode,
            "url": url,
            "resumed": True,
        }
    else:
        begin_result = _begin_verify_run(
            {
                "game_dir": str(game_dir),
                "mode": mode,
                "url": url,
                "resume_active": args.get("resume_active") is True,
            },
            ctx,
        )
        run = _require_verify_run(
            game_dir,
            ctx,
            begin_result["verify_run_id"],
        )

    identity = identity_from_run(run)
    common = {
        "game_dir": str(game_dir),
        "url": run["url"],
        "verify_run_id": run["verify_run_id"],
    }
    max_workers = max(1, int(ctx.config.verify.max_parallel_producers))
    provider_slots = max(
        1,
        min(max_workers, int(ctx.config.verify.max_parallel_provider_producers)),
    )
    provider_semaphore = BoundedSemaphore(provider_slots)
    producer_results: dict[str, dict[str, Any]] = {}
    finalizer_issues: list[dict[str, str]] = []
    orchestration_failed = False

    def remove_outputs(*relative_paths: str) -> Callable[[], None]:
        def remove() -> None:
            for relative in relative_paths:
                path = canonical_playtest_path(game_dir, relative)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

        return remove

    def reset_play_outputs() -> None:
        play_dir = canonical_playtest_path(game_dir, layout.PLAY_DIR)
        _clear_dir_except(play_dir, {Path(layout.PLAY_HISTORY).name})

    def reset_visual_outputs() -> None:
        visual_dir = canonical_playtest_path(game_dir, layout.VISUAL_DIFF_DIR)
        _clear_dir_except(visual_dir, {Path(layout.VISUAL_DIFF_HISTORY).name})
        remove_outputs(
            layout.VISUAL_ALIGNMENT,
            layout.ELEMENT_ALIGNMENT,
        )()

    def execute(
        producer_name: str,
        producer_args: dict[str, Any],
        handler: Callable[[dict[str, Any], ToolCtx], dict[str, Any]],
        *,
        provider_backed: bool = False,
        instance: str | None = None,
        prepare_retry: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def invoke() -> dict[str, Any]:
            return run_checkpointed(
                game_dir=game_dir,
                run=run,
                producer_name=producer_name,
                instance=instance,
                arguments=producer_args,
                call=lambda: handler(producer_args, ctx),
                prepare_retry=prepare_retry,
                reuse_completed=True,
            )

        try:
            if provider_backed:
                with provider_semaphore:
                    result = invoke()
            else:
                result = invoke()
            return {"status": "completed", "result": result}
        except ToolError as exc:
            return {
                "status": "failed",
                "error": {
                    "code": str(exc.code),
                    "message": exc.message,
                    "hint": exc.hint,
                },
            }
        except Exception as exc:  # noqa: BLE001 - every producer must checkpoint
            return {
                "status": "failed",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                    "hint": None,
                },
            }

    recorded_scenarios: set[str] = set()
    if not no_video:
        from . import video_tools

        for scenario in scenarios:
            record_args: dict[str, Any] = {
                "url": run["url"],
                "verify_run_id": run["verify_run_id"],
                "output": f".playtest/{layout.RECORDING_DIR}/{scenario['name']}",
            }
            if scenario["script"]:
                record_args["script"] = scenario["script"]
            if scenario["seed"]:
                record_args["seed"] = scenario["seed"]
            if args.get("record_duration") is not None:
                record_args["duration"] = args["record_duration"]
            record_result = execute(
                "record_with_state",
                record_args,
                video_tools._record_with_state,
                instance=scenario["name"],
                prepare_retry=remove_outputs(
                    f"{layout.RECORDING_DIR}/{scenario['name']}"
                ),
            )
            producer_results[f"record:{scenario['name']}"] = record_result
            record_payload = record_result.get("result")
            if (
                record_result.get("status") == "completed"
                and isinstance(record_payload, dict)
                and record_payload.get("skipped") is not True
                and isinstance(record_payload.get("video_receipt"), str)
            ):
                recorded_scenarios.add(scenario["name"])

    parallel_specs: list[
        tuple[
            str,
            str,
            dict[str, Any],
            Callable[[dict[str, Any], ToolCtx], dict[str, Any]],
            bool,
            str | None,
            Callable[[], None] | None,
        ]
    ] = []
    test_args: dict[str, Any] = {
        **common,
        "tests": f".playtest/{layout.TESTS_DIR}",
        "screenshot_each": True,
    }
    if args.get("run_tests_timeout_seconds") is not None:
        test_args["timeout_seconds"] = args["run_tests_timeout_seconds"]
    parallel_specs.append(
        (
            "tests",
            "run_tests",
            test_args,
            _run_tests,
            False,
            None,
            remove_outputs(layout.TEST_RUN_DIR, layout.TEST_RESULTS),
        )
    )

    play_args: dict[str, Any] = {
        **common,
        "max_wall": int(
            args["play_max_wall"]
            if args.get("play_max_wall") is not None
            else ctx.config.verify.play_max_wall_seconds
        ),
    }
    for source, target in (
        ("play_policy", "policy"),
        ("play_max_steps", "max_steps"),
        ("play_timeout_seconds", "timeout_seconds"),
        ("changed", "changed"),
    ):
        if args.get(source) is not None:
            play_args[target] = args[source]
    parallel_specs.append(
        (
            "play",
            "play_game",
            play_args,
            _play_game,
            True,
            None,
            reset_play_outputs,
        )
    )

    visual_args: dict[str, Any] = {
        **common,
        "round": int(args.get("visual_round") or 1),
    }
    if args.get("visual_timeout_seconds") is not None:
        visual_args["timeout_seconds"] = args["visual_timeout_seconds"]
    parallel_specs.extend(
        [
            (
                "visual",
                "visual_diff",
                visual_args,
                _visual_diff,
                True,
                None,
                reset_visual_outputs,
            ),
            (
                "layout",
                "capture_layout",
                {**common},
                _capture_layout,
                False,
                None,
                remove_outputs(layout.LAYOUT_RENDERED_DIR),
            ),
            (
                "runtime_assets",
                "capture_runtime_assets",
                {**common},
                _capture_runtime_assets,
                False,
                None,
                remove_outputs(layout.RUNTIME_ASSETS_RECEIPT),
            ),
        ]
    )

    if not no_video:
        from . import video_tools

        rubric_path = game_dir / ".playtest" / layout.RUBRIC
        for scenario in scenarios:
            if scenario["name"] not in recorded_scenarios:
                continue
            analysis_args: dict[str, Any] = {
                "video": (
                    f".playtest/{layout.RECORDING_DIR}/"
                    f"{scenario['name']}/recording.webm"
                ),
                "verify_run_id": run["verify_run_id"],
            }
            if rubric_path.is_file():
                analysis_args["rubric"] = _rel(game_dir, rubric_path)
            parallel_specs.append(
                (
                    f"analyze:{scenario['name']}",
                    "analyze_video",
                    analysis_args,
                    video_tools._analyze_video,
                    True,
                    scenario["name"],
                    remove_outputs(
                        f"{layout.RECORDING_DIR}/{scenario['name']}/video-analysis.json",
                        f"{layout.RECORDING_DIR}/{scenario['name']}/evidence",
                    ),
                )
            )

    try:
        with ThreadPoolExecutor(
            max_workers=min(max_workers, max(1, len(parallel_specs))),
            thread_name_prefix="gca-verify",
        ) as executor:
            futures = {
                executor.submit(
                    execute,
                    producer_name,
                    producer_args,
                    handler,
                    provider_backed=provider_backed,
                    instance=instance,
                    prepare_retry=prepare_retry,
                ): key
                for (
                    key,
                    producer_name,
                    producer_args,
                    handler,
                    provider_backed,
                    instance,
                    prepare_retry,
                ) in parallel_specs
            }
            for future in as_completed(futures):
                producer_results[futures[future]] = future.result()

        producer_results["gameplay_alignment"] = execute(
            "materialize_gameplay_alignment",
            {
                "game_dir": str(game_dir),
                "verify_run_id": run["verify_run_id"],
            },
            _materialize_gameplay_alignment,
            prepare_retry=remove_outputs(layout.GAMEPLAY_ALIGNMENT),
        )
        producer_results["physics_alignment"] = execute(
            "materialize_physics_alignment",
            {
                "game_dir": str(game_dir),
                "verify_run_id": run["verify_run_id"],
            },
            _materialize_physics_alignment,
            prepare_retry=remove_outputs(layout.PHYSICS_ALIGNMENT),
        )
        producer_results["layout_alignment"] = execute(
            "materialize_layout_alignment",
            {
                "game_dir": str(game_dir),
                "verify_run_id": run["verify_run_id"],
            },
            _materialize_layout_alignment,
            prepare_retry=remove_outputs(layout.LAYOUT_ALIGNMENT),
        )
    except Exception as exc:  # noqa: BLE001 - fan-in must still finalize
        orchestration_failed = True
        finalizer_issues.append(
            {
                "severity": "high",
                "description": "Verify DAG orchestration failed",
                "root_cause": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        failed = any(
            result.get("status") == "failed"
            for result in producer_results.values()
            if isinstance(result, dict)
        )
        soft_blocked = False
        for producer_result in producer_results.values():
            if not isinstance(producer_result, dict):
                continue
            payload = producer_result.get("result")
            if not isinstance(payload, dict):
                continue
            blocked = (
                payload.get("blocked") is True
                or payload.get("returncode") not in (None, 0)
            )
            if not blocked:
                continue
            soft_blocked = True
        report_args: dict[str, Any] = {
            "game_dir": str(game_dir),
            "mode": mode,
            "url": run["url"],
            "verify_run_id": run["verify_run_id"],
            "issues": finalizer_issues,
        }
        if failed or orchestration_failed:
            report_args["status"] = "partial"
        elif soft_blocked:
            report_args["status"] = "blocked"
        try:
            report_result = _write_verify_report(report_args, ctx)
        except Exception as exc:  # noqa: BLE001 - emergency seal is the fail-safe
            finalizer_issues.append(
                {
                    "severity": "high",
                    "description": "Primary verify report finalizer failed",
                    "root_cause": f"{type(exc).__name__}: {exc}",
                }
            )
            try:
                report_result = _seal_emergency_verify_report(
                    game_dir=game_dir,
                    run=run,
                    mode=mode,
                    issues=finalizer_issues,
                )
            except Exception as emergency_exc:  # noqa: BLE001 - gate must still run
                finalizer_issues.append(
                    {
                        "severity": "high",
                        "description": "Emergency verify report seal failed",
                        "root_cause": (
                            f"{type(emergency_exc).__name__}: {emergency_exc}"
                        ),
                    }
                )
                report_result = {
                    **identity,
                    "status": "finalizer_failed",
                    "run_status": "active",
                    "issues": finalizer_issues,
                }

    gate_name = args.get("gate") or "verify_report"
    try:
        gate_result = _gate_check(
            {
                "game_dir": str(game_dir),
                "gate": gate_name,
                "verify_run_id": run["verify_run_id"],
            },
            ctx,
        )
    except Exception as exc:  # noqa: BLE001 - return structured continuation state
        gate_result = {
            "gate": gate_name,
            "blocked": True,
            "returncode": None,
            "error": {
                "code": "GATE_EXECUTION_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            },
            **identity,
        }
    return {
        **identity,
        "mode": mode,
        "url": run["url"],
        "resumed": bool(begin_result.get("resumed")),
        "replayed_finalized": False,
        "status": report_result["status"],
        "run_status": report_result["run_status"],
        "producer_results": producer_results,
        "verify_report": report_result,
        "gate": gate_result,
    }


def _seal_emergency_verify_report(
    *,
    game_dir: Path,
    run: dict[str, Any],
    mode: str,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    """Best-effort minimal finalization when aggregate report assembly fails."""
    with verify_flock(game_dir, exclusive=True):
        current = read_current_run(game_dir)
        if not isinstance(current, dict) or any(
            current.get(key) != run.get(key)
            for key in ("pipeline_run_id", "build_digest", "verify_run_id")
        ):
            raise ToolError(
                "VERIFY_RUN_MISMATCH",
                "Cannot emergency-seal a different or missing verify epoch.",
            )
        report_path = canonical_playtest_path(game_dir, layout.VERIFY_REPORT)
        if current.get("status") == "finalized":
            report = _read_json(report_path)
            if not isinstance(report, dict):
                raise ToolError(
                    "VERIFY_REPORT_INVALID",
                    "Finalized epoch has no readable verify report.",
                )
            return {
                "output": _rel(game_dir, report_path),
                **identity_from_run(current),
                "status": report.get("status"),
                "run_status": "finalized",
                "report_sha256": current.get("report_sha256"),
                "summary": report.get("summary"),
                "issue_count": len(report.get("issues") or []),
                "emergency_seal": False,
            }
        if current.get("status") != "active":
            raise ToolError(
                "VERIFY_RUN_NOT_ACTIVE",
                "Emergency report sealing requires the matching active epoch.",
            )

        identity = identity_from_run(current)
        checkpoints = load_checkpoints(game_dir, current)
        combined_issues = [*issues, *checkpoint_issues(checkpoints)]
        state_path = canonical_playtest_path(game_dir, layout.STATE)
        token_path = canonical_playtest_path(game_dir, layout.RUNTIME_TOKEN_RECEIPT)
        runtime_assets_path = canonical_playtest_path(
            game_dir,
            layout.RUNTIME_ASSETS_RECEIPT,
        )
        runtime_assets = _read_json(runtime_assets_path)
        generated_at = _utc_now()
        missing = ["aggregate verify evidence unavailable: primary finalizer failed"]
        report = {
            "schema_version": 4,
            "run_id": identity["verify_run_id"],
            **identity,
            "generated_at": generated_at,
            "game_dir": ".",
            "mode": mode,
            "genre": current.get("genre"),
            "interaction_profile": current.get("interaction_profile"),
            "quality_profile": current.get("quality_profile"),
            "level_model": current.get("level_model"),
            "url": current.get("url"),
            "runtime_token_sha256": current.get("runtime_token_sha256"),
            "runtime_receipt_sha256": current.get("runtime_receipt_sha256"),
            "status": "partial",
            "summary": {
                "producer_checkpoints": {
                    "total": len(checkpoints),
                    "completed": sum(
                        item.get("status") == "completed" for item in checkpoints
                    ),
                    "failed": sum(
                        item.get("status") == "failed" for item in checkpoints
                    ),
                    "interrupted": sum(
                        item.get("status") == "running" for item in checkpoints
                    ),
                },
                "missing_artifacts": missing,
            },
            "artifacts": {
                "state": (
                    _file_entry(game_dir, state_path)
                    if state_path.is_file()
                    else None
                ),
                "runtime_token_receipt": (
                    _file_entry(game_dir, token_path)
                    if token_path.is_file()
                    else None
                ),
                "runtime_asset_registry": (
                    _file_entry(game_dir, runtime_assets_path)
                    if runtime_assets_path.is_file()
                    else None
                ),
                "producer_checkpoints": [
                    _file_entry(game_dir, game_dir / str(item["path"]))
                    for item in checkpoints
                ],
            },
            "runtime_asset_registry": (
                runtime_assets if isinstance(runtime_assets, dict) else None
            ),
            "producer_checkpoints": checkpoints,
            "issues": combined_issues,
            "emergency_seal": True,
        }
        atomic_write_json(report_path, report)
        finalized = {
            **current,
            "status": "finalized",
            "result_status": "partial",
            "completed_at": generated_at,
            "report": _rel(game_dir, report_path),
            "report_sha256": sha256_file(report_path),
            "emergency_seal": True,
        }
        atomic_write_json(
            canonical_playtest_path(game_dir, layout.VERIFY_RUN),
            finalized,
        )
        return {
            "output": _rel(game_dir, report_path),
            **identity,
            "status": "partial",
            "run_status": "finalized",
            "report_sha256": finalized["report_sha256"],
            "summary": report["summary"],
            "issue_count": len(combined_issues),
            "emergency_seal": True,
        }


# -------------------- write_verify_report --------------------


def _write_verify_report(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_flock(game_dir, exclusive=True):
        return _write_verify_report_active(args, ctx, game_dir)


def _write_verify_report_serialized(
    args: dict[str, Any],
    ctx: ToolCtx,
) -> dict[str, Any]:
    """Serialize direct fallback finalization against any live canonical DAG."""
    game_dir = Path(_resolve_game_dir(ctx, args.get("game_dir"))).resolve()
    with verify_dag_flock(game_dir):
        return _write_verify_report(args, ctx)


def _write_verify_report_active(
    args: dict[str, Any],
    ctx: ToolCtx,
    game_dir: Path,
) -> dict[str, Any]:
    current_run = _require_verify_run(
        game_dir,
        ctx,
        args.get("verify_run_id"),
    )
    identity = identity_from_run(current_run)
    producer_checkpoints = load_checkpoints(game_dir, current_run)
    url = _require_epoch_url(args, current_run)
    playtest_dir = canonical_playtest_path(game_dir)
    playtest_dir.mkdir(parents=True, exist_ok=True)
    current_run_path = playtest_dir / layout.VERIFY_RUN
    if args.get("mode") != current_run.get("mode"):
        raise ToolError(
            "VERIFY_MODE_MISMATCH",
            "write_verify_report mode must match begin_verify_run for this epoch.",
        )
    started_at = (
        float(current_run.get("started_at_ns", 0)) / 1_000_000_000
        if isinstance(current_run, dict)
        else 0.0
    )

    def current_file(path: Path) -> bool:
        return path.is_file() and path.stat().st_mtime >= started_at - 1.0

    output_path = _require_canonical_path(
        game_dir,
        args.get("output"),
        field="output",
        canonical_rel=f".playtest/{layout.VERIFY_REPORT}",
    )

    # New layout, with legacy-path fallback for partially-migrated games.
    state_path = playtest_dir / layout.STATE
    test_results_path = layout.resolve_read(playtest_dir, layout.TEST_RESULTS)
    runtime_token_path = playtest_dir / layout.RUNTIME_TOKEN_RECEIPT
    runtime_assets_path = playtest_dir / layout.RUNTIME_ASSETS_RECEIPT
    video_receipt_entries = _current_video_receipts(game_dir, current_run)
    video_receipt_paths = [entry[0] for entry in video_receipt_entries]
    video_analyses: list[Path] = []
    for receipt_path, receipt, video, timeline, _performance in video_receipt_entries:
        analysis_path = receipt_path.parent / "video-analysis.json"
        analysis = _read_json(analysis_path)
        runtime_bound = False
        if isinstance(analysis, dict):
            try:
                validate_runtime_evidence(
                    analysis.get("runtime_identity"),
                    current_run,
                )
                runtime_bound = True
            except ToolError:
                pass
        if (
            current_file(analysis_path)
            and _matches_identity(analysis, identity)
            and isinstance(analysis, dict)
            and isinstance(analysis.get("schema_version"), int)
            and analysis["schema_version"] >= 1
            and analysis.get("producer") == "gca-mcp.analyze_video"
            and analysis.get("url") == url
            and analysis.get("runtime_token_sha256")
            == current_run.get("runtime_token_sha256")
            and analysis.get("runtime_receipt_sha256")
            == current_run.get("runtime_receipt_sha256")
            and runtime_bound
            and analysis.get("video_receipt_sha256") == sha256_file(receipt_path)
            and analysis.get("video_sha256") == receipt.get("video_sha256")
            and analysis.get("video_path") == _rel(game_dir, video)
            and analysis.get("timeline_path") == _rel(game_dir, timeline)
            and analysis.get("timeline_sha256") == receipt.get("timeline_sha256")
            and analysis.get("analysis_status") == "completed"
            and analysis.get("analysis_errors") == []
            and isinstance(analysis.get("analysis"), str)
            and bool(analysis["analysis"].strip())
            and not analysis["analysis"].lstrip().lower().startswith("analysis failed")
            and isinstance(analysis.get("segment_analyses"), list)
            and bool(analysis["segment_analyses"])
            and all(
                isinstance(item, dict)
                and not item.get("error")
                and isinstance(item.get("analysis"), str)
                and bool(item["analysis"].strip())
                for item in analysis["segment_analyses"]
            )
        ):
            video_analyses.append(analysis_path)
    video_analysis_path = (
        video_analyses[0] if video_analyses else (playtest_dir / layout.VIDEO_ANALYSIS)
    )
    play_report_path = layout.resolve_read(playtest_dir, layout.PLAY_REPORT)
    critic_report_path = layout.resolve_read(playtest_dir, layout.CRITIC_REPORT)
    asset_integration_report_path = layout.resolve_read(
        playtest_dir, layout.ASSET_INTEGRATION_REPORT
    )
    visual_alignment_path = layout.resolve_read(playtest_dir, layout.VISUAL_ALIGNMENT)
    gameplay_alignment_path = layout.resolve_read(
        playtest_dir, layout.GAMEPLAY_ALIGNMENT
    )
    element_alignment_path = layout.resolve_read(playtest_dir, layout.ELEMENT_ALIGNMENT)
    physics_alignment_path = layout.resolve_read(playtest_dir, layout.PHYSICS_ALIGNMENT)
    layout_alignment_path = layout.resolve_read(playtest_dir, layout.LAYOUT_ALIGNMENT)
    visual_diff_path = layout.resolve_read(playtest_dir, layout.VISUAL_DIFF_REPORT)
    gate_results_dir = layout.resolve_read(playtest_dir, layout.GATE_RESULTS_DIR)

    screenshot_roots = [
        playtest_dir / layout.TEST_RUN_DIR,
        playtest_dir / layout.PLAY_DIR,
        playtest_dir / layout.VISUAL_DIFF_DIR,
    ]
    screenshots = sorted(
        [
            path
            for root in screenshot_roots
            if root.is_dir()
            for path in root.rglob("*.png")
            if current_file(path)
        ],
        key=lambda path: str(path),
    )
    recordings = [entry[2] for entry in video_receipt_entries]
    timelines = [entry[3] for entry in video_receipt_entries]
    performance_summaries = [entry[4] for entry in video_receipt_entries]
    gate_results = (
        sorted(
            [
                p
                for p in gate_results_dir.glob("*.json")
                if current_file(p) and _matches_identity(_read_json(p), identity)
            ],
            key=lambda p: str(p),
        )
        if gate_results_dir.is_dir()
        else []
    )

    test_results = _read_json(test_results_path)
    test_payload = (
        test_results.get("result") if isinstance(test_results, dict) else None
    )
    test_summary = (
        test_payload.get("summary") if isinstance(test_payload, dict) else None
    )

    state = _read_json(state_path)
    # Representative analysis (first scenario) for back-compat; full set below.
    video_analysis = _read_json(video_analysis_path) if video_analyses else None
    play_report = _read_json(play_report_path)
    critic_report = _read_json(critic_report_path)
    asset_integration_report = _read_json(asset_integration_report_path)
    runtime_assets = _read_json(runtime_assets_path)
    visual_alignment = _read_json(visual_alignment_path)
    gameplay_alignment = _read_json(gameplay_alignment_path)
    element_alignment = _read_json(element_alignment_path)
    physics_alignment = _read_json(physics_alignment_path)
    layout_alignment = _read_json(layout_alignment_path)
    visual_diff = _read_json(visual_diff_path)
    performance_summary = (
        _read_json(performance_summaries[-1]) if performance_summaries else None
    )
    gate_result_payloads = [
        payload
        for payload in (_read_json(p) for p in gate_results)
        if payload is not None
    ]
    identity_mismatches: list[str] = []

    def accept_current(label: str, payload: Any) -> Any:
        if payload is None:
            return None
        if not _matches_identity(payload, identity):
            identity_mismatches.append(label)
            return None
        return payload

    test_results = accept_current("test results", test_results)
    test_payload = (
        test_results.get("result") if isinstance(test_results, dict) else None
    )
    test_summary = (
        test_payload.get("summary") if isinstance(test_payload, dict) else None
    )
    asset_integration_report = accept_current(
        "asset integration report",
        asset_integration_report,
    )
    runtime_assets = accept_current("runtime asset registry", runtime_assets)
    if isinstance(runtime_assets, dict):
        try:
            validate_runtime_evidence(
                runtime_assets.get("runtime_identity"),
                current_run,
            )
            _validate_runtime_texture_evidence(runtime_assets)
            manifest_path = game_dir / "assets" / "asset-manifest.json"
            if (
                runtime_assets.get("schema_version") != 1
                or runtime_assets.get("producer") != "gca-mcp.capture_runtime_assets"
                or runtime_assets.get("url") != url
                or runtime_assets.get("runtime_token_sha256")
                != current_run.get("runtime_token_sha256")
                or runtime_assets.get("runtime_receipt_sha256")
                != current_run.get("runtime_receipt_sha256")
                or not manifest_path.is_file()
                or runtime_assets.get("manifest_sha256") != sha256_file(manifest_path)
            ):
                raise ToolError(
                    "RUNTIME_IDENTITY_MISMATCH",
                    "Runtime asset registry binding is invalid.",
                )
        except ToolError:
            identity_mismatches.append("runtime asset registry")
            runtime_assets = None
    video_analysis = accept_current("video analysis", video_analysis)
    play_report = accept_current("play report", play_report)
    critic_report = accept_current("critic report", critic_report)
    visual_alignment = accept_current("visual alignment", visual_alignment)
    gameplay_alignment = accept_current("gameplay alignment", gameplay_alignment)
    element_alignment = accept_current("element alignment", element_alignment)
    physics_alignment = accept_current("physics alignment", physics_alignment)
    layout_alignment = accept_current("layout alignment", layout_alignment)
    visual_diff = accept_current("visual diff", visual_diff)
    performance_summary = accept_current("performance summary", performance_summary)

    latest_src = _latest_src_mtime(game_dir)
    # Freshness uses the newest scenario analysis (at least one recorded post-change).
    video_mtime = max(
        (p.stat().st_mtime for p in video_analyses if p.is_file()), default=None
    )
    play_report_mtime = (
        play_report_path.stat().st_mtime if play_report_path.is_file() else None
    )
    test_results_mtime = (
        test_results_path.stat().st_mtime if test_results_path.is_file() else None
    )
    performance_summary_mtime = (
        performance_summaries[-1].stat().st_mtime if performance_summaries else None
    )

    # No-video iteration passes (MODE=iterate) intentionally skip the recording
    # pipeline (record_with_state + analyze_video + performance) — that runs ONCE at
    # the end. So don't flag those artifacts as "missing" here, or the iterate report
    # would be falsely `partial` and gateVerifyReport (in iteration_ready) would block.
    # `verify.skip_video: true` disables the recording pipeline for EVERY pass
    # (including the final MODE=full), so it forces no_video regardless of mode.
    no_video = (
        str(args.get("mode") or "").lower() in ("iterate", "full-novideo")
        or ctx.config.verify.skip_video
    )

    missing = []
    if not runtime_token_path.is_file():
        missing.append(f".playtest/{layout.RUNTIME_TOKEN_RECEIPT}")
    if not isinstance(runtime_assets, dict):
        missing.append(f".playtest/{layout.RUNTIME_ASSETS_RECEIPT}")
    if not test_results_path.is_file():
        missing.append(f".playtest/{layout.TEST_RESULTS}")
    if not screenshots:
        missing.append(".playtest/**/*.png")
    if not no_video and not recordings:
        missing.append(f".playtest/{layout.RECORDINGS_DIR}/*.webm")
    if not no_video and not video_receipt_paths:
        missing.append(
            f".playtest/{layout.RECORDING_DIR}/<scenario>/{layout.VIDEO_RECEIPT_NAME}"
        )
    if not no_video and not performance_summaries:
        missing.append(".playtest/**/performance-summary.json")
    if not asset_integration_report_path.is_file():
        missing.append(f".playtest/{layout.ASSET_INTEGRATION_REPORT}")
    if not no_video and not video_analyses:
        missing.append(
            f".playtest/{layout.RECORDING_DIR}/<scenario>/video-analysis.json"
        )
    if not play_report_path.is_file():
        missing.append(f".playtest/{layout.PLAY_REPORT}")

    has_mockups = (game_dir / "design").is_dir() and any(
        (game_dir / "design").glob("*.png")
    )
    layout_contract = _read_json(
        layout.resolve_read(playtest_dir, layout.LAYOUT_CONTRACT)
    )
    layout_required = (
        has_mockups
        and isinstance(layout_contract, dict)
        and bool(layout_contract.get("scenes"))
    )
    visual_axes = (
        visual_diff.get("axes")
        if isinstance(visual_diff, dict) and isinstance(visual_diff.get("axes"), dict)
        else {}
    )
    visual_required = has_mockups and visual_axes.get("global") is not False
    element_required = has_mockups and visual_axes.get("element") is not False
    required_evidence = [
        ("gameplay alignment", gameplay_alignment_path, gameplay_alignment, True),
        ("visual alignment", visual_alignment_path, visual_alignment, visual_required),
        (
            "element alignment",
            element_alignment_path,
            element_alignment,
            element_required,
        ),
        ("visual diff", visual_diff_path, visual_diff, has_mockups),
        ("layout alignment", layout_alignment_path, layout_alignment, layout_required),
    ]
    required_evidence_missing = []
    for label, path, payload, required in required_evidence:
        if required and not isinstance(payload, dict):
            rel = _rel(game_dir, path)
            required_evidence_missing.append((label, rel))
            if rel not in missing:
                missing.append(rel)

    test_returncode = (
        test_results.get("returncode") if isinstance(test_results, dict) else None
    )
    test_total = test_summary.get("total") if isinstance(test_summary, dict) else None
    test_fail = test_summary.get("fail") or 0 if isinstance(test_summary, dict) else 0
    blocking_evidence = bool(required_evidence_missing or identity_mismatches)
    partial_evidence = False

    play_bugs = []
    if isinstance(play_report, dict):
        play_bugs = play_report.get("bugs") or []
    blocking_bugs = [
        bug
        for bug in play_bugs
        if isinstance(bug, dict)
        and (bug.get("severity") == "crash" or bug.get("source") == "judge")
    ]

    issues = list(args.get("issues") or [])
    issues.extend(checkpoint_issues(producer_checkpoints))
    if not isinstance(runtime_assets, dict):
        blocking_evidence = True
        issues.append(
            {
                "severity": "high",
                "description": "Current runtime asset registry receipt is missing or invalid",
                "root_cause": (
                    "Run capture_runtime_assets with this verify_run_id and epoch URL"
                ),
            }
        )
    for label in identity_mismatches:
        issues.append(
            {
                "severity": "high",
                "description": f"{label} belongs to a different or untagged verify run",
                "root_cause": (
                    f"Expected verify_run_id={identity['verify_run_id']} and "
                    f"build_digest={identity['build_digest']}"
                ),
            }
        )
    for label, rel in required_evidence_missing:
        issues.append(
            {
                "severity": "high",
                "description": f"Required {label} is missing or invalid",
                "root_cause": f"See {rel}",
            }
        )
    if isinstance(state, dict):
        issues.extend(state.get("known_issues") or [])
    if isinstance(asset_integration_report, dict):
        asset_blocking_issues = asset_integration_report.get("blocking_issues") or []
        issues.extend(asset_blocking_issues)
        if asset_blocking_issues:
            blocking_evidence = True
    if test_results_path.is_file() and (
        not isinstance(test_summary, dict)
        or not isinstance(test_total, (int, float))
        or test_total <= 0
    ):
        blocking_evidence = True
        issues.append(
            {
                "severity": "high",
                "description": "Formal test suite executed zero tests or produced no valid summary",
                "root_cause": f"See .playtest/{layout.TEST_RESULTS}",
            }
        )
    if test_results_path.is_file() and (test_fail > 0 or test_returncode != 0):
        blocking_evidence = True
        issues.append(
            {
                "severity": "high",
                "description": f"{test_fail} Playwright test(s) failed (returncode {test_returncode})",
                "root_cause": f"See .playtest/{layout.TEST_RESULTS}",
            }
        )
    for bug in blocking_bugs[:5]:
        blocking_evidence = True
        issues.append(
            {
                "severity": "high",
                "description": bug.get("message", "blocking play-agent bug"),
                "root_cause": "play-agent oracle/judge reported a blocking bug",
            }
        )
    # Fold EVERY alignment axis (not just visual + gameplay) so the report's issue list
    # is a complete picture for the fixer — element (per-UI-element looks-like), physics
    # (bodies line up), and layout (lays-out-like) each carry their own `issues`. Missing
    # any axis here used to leave the report incomplete, so a fixer dispatched from it
    # couldn't address element/layout problems. (The authoritative FULL blocking set is
    # still `gate_check(iteration_ready)`, which also recomputes the deterministic UI /
    # crop / aspect / interaction gates that are not stored as issues pre-gate.)
    _axis_hint = {
        "visual": "the concept mockups (design/<screen>.png)",
        "gameplay": "GDD §0.2 mechanics",
        "element": "each §2E UI element's concept crop",
        "physics": "the collision bodies vs the visible art",
        "layout": "each element's §2.0.2 position",
    }
    for axis_name, align, align_path in (
        ("visual", visual_alignment, visual_alignment_path),
        ("gameplay", gameplay_alignment, gameplay_alignment_path),
        ("element", element_alignment, element_alignment_path),
        ("physics", physics_alignment, physics_alignment_path),
        ("layout", layout_alignment, layout_alignment_path),
    ):
        if not isinstance(align, dict) or align.get("applicable") is False:
            continue
        axis_issues = []
        for item in align.get("issues") or []:
            if isinstance(item, dict):
                axis_issues.append(item)
            elif isinstance(item, str):
                axis_issues.append(
                    {
                        "severity": "high"
                        if align.get("aligned") is False
                        else "medium",
                        "description": item,
                        "root_cause": (
                            f"see .playtest/{layout.ALIGNMENT_DIR}/{axis_name}/alignment.json"
                        ),
                    }
                )
        issues.extend(axis_issues)
        if align.get("status") == "unavailable":
            partial_evidence = True
        elif align.get("status") == "error":
            blocking_evidence = True
        if align.get("aligned") is False:
            blocking_evidence = True
        if align.get("aligned") is False and not axis_issues:
            issues.append(
                {
                    "severity": "high",
                    "description": f"{axis_name} alignment failed — running game does not match {_axis_hint.get(axis_name, 'the design')}",
                    "root_cause": f"see .playtest/{layout.ALIGNMENT_DIR}/{axis_name}/alignment.json",
                }
            )
        if (
            latest_src is not None
            and align_path.is_file()
            and align_path.stat().st_mtime < latest_src
        ):
            blocking_evidence = True
            issues.append(
                {
                    "severity": "high",
                    "description": f"{axis_name} alignment is stale relative to source changes",
                    "root_cause": f"rerun {axis_name} verification",
                }
            )
    # NOTE: the visual-diff sidecar (verify_middle/visual/visual-diff-report.json) is the
    # raw union of the visual + element axes already folded above (element-visual-diff.mjs
    # writes all three), so it is intentionally NOT folded again here — that would
    # double-list every per-element mismatch. `visual_diff` is still surfaced in the
    # report body (below) as an artifact reference for the fixer to open.

    if isinstance(visual_diff, dict):
        if visual_diff.get("status") == "unavailable":
            partial_evidence = True
            if not any(
                isinstance(issue, dict)
                and "visual comparison unavailable"
                in str(issue.get("description", "")).lower()
                for issue in issues
            ):
                issues.append(
                    {
                        "severity": "warning",
                        "description": (
                            "Visual comparison unavailable after bounded VLM retries; "
                            "pipeline continued because unavailable_policy=warn"
                        ),
                        "root_cause": f"See .playtest/{layout.VISUAL_DIFF_REPORT}",
                    }
                )
        elif visual_diff.get("status") in ("failed", "error"):
            blocking_evidence = True

    requested_status = args.get("status")
    if requested_status == "partial":
        report_status = "partial"
    elif blocking_evidence or requested_status == "blocked":
        report_status = "blocked"
    elif missing or partial_evidence:
        report_status = "partial"
    else:
        report_status = requested_status or "done"

    report = {
        "schema_version": 4,
        "run_id": identity["verify_run_id"],
        **identity,
        "generated_at": _utc_now(),
        "game_dir": ".",
        "mode": args.get("mode"),
        "genre": state["genre"],
        "interaction_profile": state["interaction_profile"],
        "quality_profile": state["quality_profile"],
        "level_model": state["level_model"],
        "url": url,
        "runtime_token_sha256": current_run["runtime_token_sha256"],
        "runtime_receipt_sha256": current_run["runtime_receipt_sha256"],
        "status": report_status,
        "summary": {
            "tests": test_summary,
            "screenshot_count": len(screenshots),
            "recording_count": len(recordings),
            "video_receipt_count": len(video_receipt_paths),
            "runtime_asset_registry_exists": isinstance(runtime_assets, dict),
            "runtime_loaded_texture_count": (
                runtime_assets.get("summary", {}).get("loaded_texture_count")
                if isinstance(runtime_assets, dict)
                else None
            ),
            "runtime_manifest_used_count": (
                runtime_assets.get("summary", {}).get("manifest_used_count")
                if isinstance(runtime_assets, dict)
                else None
            ),
            "performance_summary_exists": bool(performance_summaries),
            "asset_integration_report_exists": asset_integration_report_path.is_file(),
            "asset_integration_blocking_count": (
                len(asset_integration_report.get("blocking_issues") or [])
                if isinstance(asset_integration_report, dict)
                else None
            ),
            "fps_p50": (
                performance_summary.get("fps", {}).get("p50")
                if isinstance(performance_summary, dict)
                else None
            ),
            "fps_min": (
                performance_summary.get("fps", {}).get("min")
                if isinstance(performance_summary, dict)
                else None
            ),
            "boot_seconds": (
                performance_summary.get("timings", {}).get("boot_seconds")
                if isinstance(performance_summary, dict)
                else None
            ),
            "video_analysis_exists": bool(video_analyses),
            "video_analysis_count": len(video_analyses),
            "video_skipped": ctx.config.verify.skip_video,
            "play_report_exists": play_report_path.is_file(),
            "play_weighted_accuracy": (
                play_report.get("weighted_accuracy")
                if isinstance(play_report, dict)
                else None
            ),
            "blocking_play_bug_count": len(blocking_bugs),
            "visual_aligned": (
                visual_alignment.get("aligned")
                if isinstance(visual_alignment, dict)
                else None
            ),
            "gameplay_aligned": (
                gameplay_alignment.get("aligned")
                if isinstance(gameplay_alignment, dict)
                else None
            ),
            "element_aligned": (
                element_alignment.get("aligned")
                if isinstance(element_alignment, dict)
                else None
            ),
            "physics_aligned": (
                physics_alignment.get("aligned")
                if isinstance(physics_alignment, dict)
                else None
            ),
            "layout_aligned": (
                layout_alignment.get("aligned")
                if isinstance(layout_alignment, dict)
                else None
            ),
            "visual_diff_aligned": (
                visual_diff.get("aligned") if isinstance(visual_diff, dict) else None
            ),
            "visual_diff_status": (
                visual_diff.get("status") if isinstance(visual_diff, dict) else None
            ),
            "visual_unavailable_policy": (
                visual_diff.get("unavailable_policy")
                if isinstance(visual_diff, dict)
                else None
            ),
            "producer_checkpoints": {
                "total": len(producer_checkpoints),
                "completed": sum(
                    checkpoint.get("status") == "completed"
                    for checkpoint in producer_checkpoints
                ),
                "failed": sum(
                    checkpoint.get("status") == "failed"
                    for checkpoint in producer_checkpoints
                ),
                "interrupted": sum(
                    checkpoint.get("status") == "running"
                    for checkpoint in producer_checkpoints
                ),
            },
            "missing_artifacts": missing,
        },
        "artifacts": {
            "state": _file_entry(game_dir, state_path)
            if state_path.is_file()
            else None,
            "runtime_token_receipt": (
                _file_entry(game_dir, runtime_token_path)
                if runtime_token_path.is_file()
                else None
            ),
            "runtime_asset_registry": (
                _file_entry(game_dir, runtime_assets_path)
                if runtime_assets_path.is_file()
                else None
            ),
            "test_results": (
                _file_entry(game_dir, test_results_path)
                if test_results_path.is_file()
                else None
            ),
            "screenshots": [_file_entry(game_dir, p) for p in screenshots],
            "recordings": [_file_entry(game_dir, p) for p in recordings],
            "video_receipts": [_file_entry(game_dir, p) for p in video_receipt_paths],
            "state_timelines": [_file_entry(game_dir, p) for p in timelines],
            "performance_summaries": [
                _file_entry(game_dir, p) for p in performance_summaries
            ],
            "performance_summary": (
                _file_entry(game_dir, performance_summaries[-1])
                if performance_summaries
                else None
            ),
            "asset_integration_report": (
                _file_entry(game_dir, asset_integration_report_path)
                if asset_integration_report_path.is_file()
                else None
            ),
            "video_analyses": [_file_entry(game_dir, p) for p in video_analyses],
            "video_analysis": (
                _file_entry(game_dir, video_analysis_path) if video_analyses else None
            ),
            "play_report": (
                _file_entry(game_dir, play_report_path)
                if play_report_path.is_file()
                else None
            ),
            "critic_report": (
                _file_entry(game_dir, critic_report_path)
                if critic_report_path.is_file()
                else None
            ),
            "visual_alignment": (
                _file_entry(game_dir, visual_alignment_path)
                if visual_alignment_path.is_file()
                else None
            ),
            "gameplay_alignment": (
                _file_entry(game_dir, gameplay_alignment_path)
                if gameplay_alignment_path.is_file()
                else None
            ),
            "element_alignment": (
                _file_entry(game_dir, element_alignment_path)
                if element_alignment_path.is_file()
                else None
            ),
            "physics_alignment": (
                _file_entry(game_dir, physics_alignment_path)
                if physics_alignment_path.is_file()
                else None
            ),
            "layout_alignment": (
                _file_entry(game_dir, layout_alignment_path)
                if layout_alignment_path.is_file()
                else None
            ),
            "visual_diff": (
                _file_entry(game_dir, visual_diff_path)
                if visual_diff_path.is_file()
                else None
            ),
            "producer_checkpoints": [
                _file_entry(
                    game_dir,
                    game_dir / str(checkpoint["path"]),
                )
                for checkpoint in producer_checkpoints
            ],
            "gate_results": [_file_entry(game_dir, p) for p in gate_results],
        },
        "freshness": {
            "latest_src_mtime": latest_src,
            "test_results_fresh": (
                latest_src is None
                or test_results_mtime is not None
                and test_results_mtime >= latest_src
            ),
            "performance_summary_fresh": (
                latest_src is None
                or performance_summary_mtime is not None
                and performance_summary_mtime >= latest_src
            ),
            "video_analysis_fresh": (
                # None (not stale) on no-video passes — there is nothing to be fresh.
                None
                if no_video
                else (
                    latest_src is None
                    or video_mtime is not None
                    and video_mtime >= latest_src
                )
            ),
            "play_report_fresh": (
                latest_src is None
                or play_report_mtime is not None
                and play_report_mtime >= latest_src
            ),
        },
        "state": state,
        "asset_integration_report": asset_integration_report,
        "runtime_asset_registry": runtime_assets,
        "video_receipts": [entry[1] for entry in video_receipt_entries],
        "performance_summary": performance_summary,
        "video_analysis": video_analysis,
        "play_report": play_report,
        "critic_report": critic_report,
        "visual_alignment": visual_alignment,
        "gameplay_alignment": gameplay_alignment,
        "element_alignment": element_alignment,
        "physics_alignment": physics_alignment,
        "layout_alignment": layout_alignment,
        "visual_diff": visual_diff,
        "producer_checkpoints": producer_checkpoints,
        "gate_results": gate_result_payloads,
        "issues": issues,
    }

    atomic_write_json(output_path, report)
    finalized_run = {
        **current_run,
        "status": "finalized",
        "result_status": report_status,
        "completed_at": report["generated_at"],
        "report": _rel(game_dir, output_path),
        "report_sha256": sha256_file(output_path),
    }
    atomic_write_json(current_run_path, finalized_run)
    return {
        "output": _rel(game_dir, output_path),
        **identity,
        "status": report["status"],
        "run_status": "finalized",
        "report_sha256": finalized_run["report_sha256"],
        "summary": report["summary"],
        "issue_count": len(issues),
    }


_GATE_CHECK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "gate": {
            "type": "string",
            "description": (
                "Gate name: scaffold_done | step_done | "
                "first_pass_complete (composite) | iteration_ready "
                "(composite) | play_quality | td_contract | "
                "interaction_contract | ui_contract | layout_alignment "
                "| visual_diff | slot_validation | tests_passing | "
                "polish_rounds | "
                "verify_report | crop_coverage | performance | "
                "ready_to_ship (composite)."
            ),
        },
        "game_dir": {"type": "string"},
        "verify_run_id": {
            "type": "string",
            "description": (
                "Required for verify-epoch gates, including step_done "
                "phase 7; exact ID returned by run_verify_dag/begin_verify_run. "
                "Final/read-only gates may consume a finalized run."
            ),
        },
        "step": {
            "type": "integer",
            "description": (
                "Required for step_done. Current cumulative top-level "
                "pipeline phase: 2 Scaffold, 3 implementation plus "
                "asset/source completion, 4 integration plus measured "
                "entity/UI ledgers and sprite slot-fit receipts, 5 "
                "verification entry, 6 visual review, 7 full or approved "
                "full-novideo verification."
            ),
        },
        "min_accuracy": {
            "type": "number",
            "description": (
                "play_quality only: min play-agent weighted accuracy (default 0.8)."
            ),
        },
    },
    "required": ["gate"],
    "additionalProperties": False,
}

# Claude's public tool schema rejects root-level composition keywords. Keep the
# MCP-facing schema flat while retaining the complete Draft 2020-12 conditions
# for server-side argument validation.
_GATE_CHECK_VALIDATION_SCHEMA = {
    **_GATE_CHECK_INPUT_SCHEMA,
    "allOf": [
        {
            "if": {
                "properties": {
                    "gate": {"enum": sorted(_VERIFY_EPOCH_GATES)},
                },
                "required": ["gate"],
            },
            "then": {"required": ["verify_run_id"]},
        },
        {
            "if": {
                "properties": {"gate": {"const": "step_done"}},
                "required": ["gate"],
            },
            "then": {
                "required": ["step"],
                "properties": {
                    "step": {
                        "minimum": 2,
                        "maximum": 7,
                    },
                },
            },
        },
        {
            "if": {
                "properties": {
                    "gate": {"const": "step_done"},
                    "step": {"const": 7},
                },
                "required": ["gate", "step"],
            },
            "then": {"required": ["verify_run_id"]},
        },
    ],
}


TOOLS = [
    ToolDef(
        name="write_asset_integration_report",
        description=(
            "Derive and atomically write the authoritative asset-integration "
            "receipt from asset-manifest.json, on-disk files/metadata, and src/ "
            "runtime references. Caller-reported semantic issues may be added, "
            "but objective counts/checks are MCP-owned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string"},
                            "category": {"type": "string"},
                            "asset_key": {"type": "string"},
                            "description": {"type": "string"},
                            "route_to": {"type": "string"},
                            "root_cause": {"type": "string"},
                        },
                        "required": [
                            "severity",
                            "category",
                            "asset_key",
                            "description",
                            "route_to",
                            "root_cause",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        handler=_write_asset_integration_report,
        requires_workspace=True,
    ),
    ToolDef(
        name="begin_verify_run",
        description=(
            "CAS-start one verification evidence epoch under the workspace "
            "cross-process flock. Fails if a run is active unless "
            "resume_active=true explicitly adopts the same build/mode/URL after "
            "the previous verifier has stopped. "
            "Requires state.json's pipeline_run_id/build_digest to match current "
            "inputs, clears prior current-run outputs while preserving histories, "
            "normalizes a required URL, writes a non-build-input runtime token "
            "receipt, and returns verify_run_id plus runtime receipt hashes. "
            "A new iterate epoch is rejected when pipeline.max_iterations is "
            "exhausted; resuming an existing epoch and final full confirmation "
            "remain available. "
            "Every producer must pass that exact verify_run_id and URL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["iterate", "full", "full-novideo"],
                },
                "url": {"type": "string"},
                "resume_active": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Explicitly resume a matching active epoch after confirming "
                        "its previous verifier has stopped."
                    ),
                },
            },
            "required": ["mode", "url"],
            "additionalProperties": False,
        },
        handler=_begin_verify_run_serialized,
        requires_workspace=True,
    ),
    ToolDef(
        name="run_verify_dag",
        description=(
            "Run a complete checkpointed verification pass in one resilient MCP "
            "call under a cross-process single-flight lock. Starts a new epoch, "
            "resumes the exact active epoch, or idempotently returns an already "
            "finalized matching epoch. Refuses to open a new iterate epoch after "
            "the configured pipeline.max_iterations budget is exhausted. Records "
            "full-mode scenarios alone, then runs formal tests, play, visual "
            "comparison, layout capture, runtime-asset capture, and video analyses "
            "as independent parallel DAG nodes with bounded provider concurrency. "
            "Completed checkpoints are reused on resume; failed or interrupted "
            "nodes are rerun. The fan-in always materializes gameplay/physics/layout axes "
            "and finalizes a partial/blocked/done verify report in a finally block, "
            "then runs the verify_report gate. The orchestrator performs current "
            "screenshot review, runs the composite iteration/release gate, and "
            "proceeds directly to critic even when a producer fails."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["iterate", "full", "full-novideo"],
                },
                "url": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": (
                        "Optional exact active/finalized epoch ID. When provided, "
                        "no new epoch is opened: active matching checkpoints resume, "
                        "while a valid finalized report is returned idempotently."
                    ),
                },
                "resume_active": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Adopt the matching active epoch when no verify_run_id is "
                        "provided, after confirming the prior verifier stopped."
                    ),
                },
                "scenarios": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional unique lowercase-slug full-mode scenario names "
                        "from .playtest/inputs/interactions/*.mjs."
                    ),
                },
                "visual_round": {"type": "integer", "minimum": 1, "default": 1},
                "play_policy": {
                    "type": "string",
                    "enum": ["llm", "heuristic"],
                },
                "play_max_steps": {"type": "integer", "minimum": 1},
                "play_max_wall": {
                    "type": "integer",
                    "minimum": 30,
                    "description": (
                        "Optional play-agent wall override; defaults to "
                        "verify.play_max_wall_seconds (240s)."
                    ),
                },
                "record_duration": {"type": "integer", "minimum": 1},
                "run_tests_timeout_seconds": {"type": "number", "minimum": 1},
                "play_timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "description": (
                        "Outer process timeout; must exceed play_max_wall by "
                        f"at least {PLAY_WATCHDOG_GRACE_SECONDS}s."
                    ),
                },
                "visual_timeout_seconds": {"type": "number", "minimum": 1},
                "changed": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated changed paths used only to "
                        "prioritize the play objectives; formal tests remain unfiltered."
                    ),
                },
                "gate": {
                    "type": "string",
                    "enum": ["iteration_ready", "verify_report"],
                },
            },
            "required": ["mode", "url"],
            "additionalProperties": False,
        },
        handler=_run_verify_dag,
        requires_workspace=True,
    ),
    ToolDef(
        name="gate_check",
        description=(
            "Run a pipeline gate check (bundled gate-check.mjs). Returns "
            "`blocked: true` if any required artifact is missing or stale. "
            "The `game_dir` arg defaults to the open workspace."
        ),
        input_schema=_GATE_CHECK_INPUT_SCHEMA,
        validation_schema=_GATE_CHECK_VALIDATION_SCHEMA,
        handler=_gate_check,
        requires_workspace=True,
    ),
    ToolDef(
        name="check_ui_contract",
        description=(
            "Diagnostic (advisory): run the UI contract gate standalone via "
            "gate-check.mjs — proportion + per-element art parity + text/slot "
            "binding + static-text-as-asset. Returns the same structured "
            "payload as gate_check (blocked + failures[] with axis/agent). Use "
            "mid-build for early UI feedback; it is NOT a release authority — "
            "the release decision stays gate_check({gate:'ready_to_ship', "
            "verify_run_id:VERIFY_RUN_ID})."
        ),
        input_schema={
            "type": "object",
            "properties": {"game_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_make_gate_diagnostic("ui_contract"),
        requires_workspace=True,
    ),
    ToolDef(
        name="check_td_contract",
        description=(
            "Diagnostic (advisory): run the tower-defence structural contract "
            "gate standalone (map-layout SSOT, terrain-only base, path-from-"
            "layout, value-less HUD binding). Auto-skips non-tower_defence "
            "genres. Same structured payload as gate_check; NOT a release "
            "authority."
        ),
        input_schema={
            "type": "object",
            "properties": {"game_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_make_gate_diagnostic("td_contract"),
        requires_workspace=True,
    ),
    ToolDef(
        name="check_interaction_contract",
        description=(
            "Diagnostic (advisory): run the interaction contract gate "
            "standalone (collision layers/matrix/world bounds, hit-target fit, "
            "tunnel risk, terrain-prefab coverage). Same structured payload as "
            "gate_check; NOT a release authority."
        ),
        input_schema={
            "type": "object",
            "properties": {"game_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_make_gate_diagnostic("interaction_contract"),
        requires_workspace=True,
    ),
    ToolDef(
        name="run_tests",
        description=(
            "Run the Playwright test suite (bundled test-runner.mjs) "
            "against the exact active verify-epoch URL. The browser must fetch and "
            "match the current workspace runtime token before tests run. "
            "Supports solo screenshot mode, "
            "filter / id / changed selectors, video recording, and "
            "screenshot-each. Any formal run with selectors or a `tests` path "
            "other than the exact canonical `.playtest/tests/` directory writes "
            "scoped diagnostic results and never replaces the unfiltered "
            "canonical full-suite result required by composite gates."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "game_dir": {"type": "string"},
                "tests": {
                    "type": "string",
                    "default": ".playtest/tests/",
                    "description": (
                        "Test directory. Only the exact canonical "
                        "`.playtest/tests/` path with no selectors is formal "
                        "full-suite gate evidence; every other path is scoped."
                    ),
                },
                "solo": {"type": "boolean", "default": False},
                "script": {"type": "string"},
                "filter": {
                    "type": "string",
                    "description": (
                        "Diagnostic test-name filter; makes the result scoped "
                        "and ineligible as canonical iterate/full gate evidence."
                    ),
                },
                "id": {
                    "type": "string",
                    "description": (
                        "Diagnostic single-test selector; makes the result "
                        "scoped and ineligible as canonical iterate/full gate "
                        "evidence."
                    ),
                },
                "changed": {
                    "type": "string",
                    "description": (
                        "Comma-separated changed paths for a diagnostic scoped "
                        "run; never canonical iterate/full gate evidence."
                    ),
                },
                "timeout": {"type": "integer"},
                "screenshot_each": {"type": "boolean", "default": False},
                "record": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Record a video of each test. Silently dropped when "
                        "verify.skip_video is true in gca.config.yaml."
                    ),
                },
                "headed": {"type": "boolean", "default": False},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "default": 1200,
                    "description": (
                        "Outer process timeout; must exceed max_wall by at "
                        f"least {PLAY_WATCHDOG_GRACE_SECONDS}s."
                    ),
                },
            },
            "required": ["url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler("run_tests", _run_tests),
        requires_workspace=True,
    ),
    ToolDef(
        name="write_verify_report",
        description=(
            "Collect the latest verification artifacts into "
            "`<GAME_DIR>/.playtest/verify/verify-report.json`: test results, screenshots, "
            "recordings, state timelines, performance summaries, video analysis, "
            "play-agent report, all five alignment axes (visual, gameplay, "
            "element, physics, layout), the visual-diff sidecar, critic report, "
            "asset-integration report, runtime asset registry, video receipts, "
            "gate results, "
            "state.json, freshness flags, and issue summaries. "
            "Blocking test/alignment evidence forces report status='blocked'; "
            "a warning-only unavailable VLM forces status='partial'. "
            "run_verify_dag owns this call for canonical passes; direct use is "
            "the last-resort seal when its finalizer could not run. Atomically "
            "changes current-run.status from active to finalized; later "
            "producers are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "output": {
                    "type": "string",
                    "default": ".playtest/verify/verify-report.json",
                },
                "mode": {
                    "type": "string",
                    "enum": ["iterate", "full", "full-novideo"],
                    "description": (
                        "Verification mode: iterate | full | full-novideo. "
                        "`iterate` is the no-video fix-loop pass "
                        "(screenshots + "
                        "visual/element/gameplay/physics/layout, NO recording/analyze_video/"
                        "performance) — those video artifacts are not flagged missing; "
                        "`full` is the one final pass that adds the recording pipeline. "
                        "`full-novideo` is that final pass run without the recording "
                        "pipeline (when recording is unavailable) — video artifacts are "
                        "likewise not flagged missing. "
                        "When verify.skip_video is true in gca.config.yaml, EVERY mode "
                        "(including full) is treated as no-video."
                    ),
                },
                "url": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["done", "partial", "blocked"],
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            "required": ["mode", "url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_write_verify_report_serialized,
        requires_workspace=True,
    ),
    ToolDef(
        name="play_game",
        description=(
            "Intelligent closed-loop play-agent (bundled play-agent.mjs): "
            "perceives the game each step (screenshot + engine state), acts via "
            "the per-game interaction contract (the actions block), and verifies "
            "{operation, expected_result} "
            "objectives (YES/NO/PARTIAL + weighted accuracy). Runs bug oracles "
            "(crash/stuck/NaN/leak/timing), tracks coverage + regressions, and "
            "enforces a Build-Health gate. Requires "
            "`<GAME_DIR>/.playtest/inputs/interaction-contract.json` "
            "(legacy `control-map.json` still resolves; the agent exits 2 "
            "without it) and a strict `objectives.json`. Missing objectives, "
            "unknown keys, unsupported setup/success forms, duplicate IDs, or "
            "a missing `start_game` objective exit 2 before browser/provider work. Returns the "
            "parsed agent-play-report.json. `blocked: true` means Build-Health "
            "failed. Use for the rubric's interaction dimension."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "game_dir": {"type": "string"},
                "policy": {
                    "type": "string",
                    "enum": ["llm", "heuristic"],
                    "default": "llm",
                },
                "model": {"type": "string"},
                "provider": {"type": "string"},
                "vision_only": {"type": "boolean", "default": False},
                "realtime": {"type": "boolean", "default": False},
                "headed": {"type": "boolean", "default": False},
                "record": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Record a video of the play session. Silently dropped "
                        "when verify.skip_video is true in gca.config.yaml."
                    ),
                },
                "changed": {"type": "string"},
                "max_steps": {"type": "integer"},
                "seeds": {"type": "integer"},
                "max_wall": {
                    "type": "integer",
                    "minimum": 30,
                    "description": (
                        "Hard wall-clock cap in seconds; defaults to "
                        "verify.play_max_wall_seconds (240s)."
                    ),
                },
                "action_delay": {"type": "integer"},
                "control_map": {
                    "type": "string",
                    "description": (
                        "Override path to the interaction contract (the actions "
                        "block). Defaults to inputs/interaction-contract.json, "
                        "falling back to the legacy control-map.json."
                    ),
                },
                "objectives": {"type": "string"},
                "out": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 1200},
            },
            "required": ["url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler("play_game", _play_game),
        requires_workspace=True,
    ),
    ToolDef(
        name="materialize_gameplay_alignment",
        description=(
            "Deterministically convert the current play-agent report into the "
            "gameplay alignment axis. Uses build health, objective accuracy, "
            "regressions, and blocking oracle/judge bugs with the configured "
            "play-quality threshold; writes the identity-bound canonical "
            "alignment/gameplay/alignment.json without an extra agent-authored "
            "report."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact active ID returned by begin_verify_run.",
                },
            },
            "required": ["verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler(
            "materialize_gameplay_alignment",
            _materialize_gameplay_alignment,
        ),
        requires_workspace=True,
    ),
    ToolDef(
        name="materialize_physics_alignment",
        description=(
            "Deterministically project the interaction-contract body/art checks "
            "into the identity-bound Axis 4 physics alignment. Writes "
            "alignment/physics/alignment.json; interaction_profile=none becomes "
            "an explicit not-applicable result, while blocking body/world/hit-target/"
            "tunnel/prefab checks become structured alignment issues."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact active ID returned by run_verify_dag/begin_verify_run.",
                },
            },
            "required": ["verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler(
            "materialize_physics_alignment",
            _materialize_physics_alignment,
        ),
        requires_workspace=True,
    ),
    ToolDef(
        name="visual_diff",
        description=(
            "Comprehensive visual verification (Axis 1 whole-screen + Axis 3 "
            "per-element) via the bundled element-visual-diff.mjs. Clips each "
            "rendered UI element by its measured box_px, pairs it 1:1 with its "
            "concept crop (paired VLM), pairs each running screen with its "
            "design/<screen>.png mockup, and writes the source-tagged "
            "alignment/visual + alignment/element files and the "
            "verify_middle/visual/visual-diff-report.json sidecar (+ .screen.png "
            "and .render.png evidence) that gateVisualDiff requires. This is the "
            "ONLY valid producer of the visual/element axes — NEVER hand-author "
            "those files. Run it every MODE=iterate/full pass; re-run with the "
            "next --round after a fix. Returns status plus aligned / global_aligned / "
            "element_aligned from the sidecar. Exhausted VLM retries return "
            "status='unavailable', aligned=null; the visual gate warns and continues "
            "by default. Requires a running game server."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "URL of the running game server (no default — a stale "
                        "localhost server would be silently diffed)."
                    ),
                },
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "round": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Current nested visual fix-loop attempt (1..gates.visual.max_rounds). "
                        "Polish evidence is counted from producer-written history, not "
                        "from mutable pipeline state or this value alone."
                    ),
                },
                "map": {
                    "type": "string",
                    "description": 'Scene→screen overrides, e.g. "GameScene=gameplay,LevelCompleteScene=gameover".',
                },
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
                "settle": {"type": "integer", "default": 700},
                "headed": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 1800},
            },
            "required": ["url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler("visual_diff", _visual_diff),
        requires_workspace=True,
    ),
    ToolDef(
        name="capture_runtime_assets",
        description=(
            "Open the exact verification epoch URL, fetch and verify its current "
            "workspace runtime token, then atomically capture Phaser's loaded "
            "texture keys, animation-frame texture keys, and active/reachable "
            "scene display-list texture keys. Writes the canonical "
            ".playtest/verify/runtime/runtime-assets.json receipt bound to "
            "manifest hash, URL, runtime token, and verify identity."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "url": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
                "settle": {"type": "integer", "default": 400},
                "headed": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 600},
            },
            "required": ["url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler(
            "capture_runtime_assets",
            _capture_runtime_assets,
        ),
        requires_workspace=True,
    ),
    ToolDef(
        name="materialize_layout_alignment",
        description=(
            "Materialize Axis 5 layout alignment without running a blocking "
            "pipeline gate. Reads capture_layout outputs under "
            "verify_middle/layout/ plus inputs/layout-contract.json, writes "
            "verify/alignment/layout/alignment.json, and returns aligned/status/"
            "issues. run_verify_dag owns capture → materialize → report and folds "
            "returned blocking_messages/issues into the aggregate before the "
            "composite gate. "
            "Direct use is a scoped diagnostic and does not replace the DAG."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "timeout_seconds": {"type": "number", "default": 300},
            },
            "required": ["verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler(
            "materialize_layout_alignment",
            _materialize_layout_alignment,
        ),
        requires_workspace=True,
    ),
    ToolDef(
        name="capture_layout",
        description=(
            "Deterministic layout bounds capture (Axis 5) via the bundled "
            "capture-layout.mjs. Reads layout-contract.json, starts each scene "
            "on the running game, walks the display list, and dumps every named "
            "object's center/size to verify_middle/layout/<scene>.rendered.json "
            "— the MEASURED side gateLayoutAlignment consumes. Re-run after any "
            "src/ change (stale captures block). Requires a running game server."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "URL of the running game server (no default — a stale "
                        "localhost server would be silently measured)."
                    ),
                },
                "game_dir": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "contract": {
                    "type": "string",
                    "description": (
                        "Must resolve to canonical "
                        ".playtest/inputs/layout-contract.json."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Must resolve to canonical .playtest/verify_middle/layout."
                    ),
                },
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 720},
                "settle": {"type": "integer", "default": 700},
                "headed": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 600},
            },
            "required": ["url", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_verify_handler("capture_layout", _capture_layout),
        requires_workspace=True,
    ),
]
