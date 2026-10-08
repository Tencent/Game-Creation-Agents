"""Video understanding + recording tools — wrap the bundled Node.js runners."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import playtest_layout as layout
from ..adapters.script_runner import run_node_script, runner_path
from ..errors import ToolError
from ..evidence import (
    atomic_write_json,
)
from ..server import ToolCtx, ToolDef
from ..verify_checkpoint import run_checkpointed
from ..verify_epoch import (
    identity_from_run,
    load_runtime_token_receipt,
    normalize_epoch_url,
    require_current_run,
    runtime_cli_args,
    sha256_file,
    validate_runtime_evidence,
    verify_dag_flock,
    verify_flock,
)
from ..workspace import validated_workspace_root
from ._shared import (
    parse_json_output as _parse_json_output,
)
from ._shared import (
    resolve_workspace_path as _resolve_workspace_path,
)
from ._shared import (
    workspace_relative as _rel,
)
from .verify_tools import _require_pipeline_identity


def _base_dir(ctx: ToolCtx) -> Path:
    workspace = getattr(getattr(ctx, "state", None), "workspace", None)
    if workspace is None:
        raise ToolError(
            "NO_WORKSPACE_OPEN",
            "Video verification requires an open game workspace.",
        )
    return validated_workspace_root(workspace)


def _recording_dir(game_dir: Path, value: Any) -> Path:
    base_rel = f".playtest/{layout.RECORDING_DIR}"
    path = _resolve_workspace_path(
        game_dir,
        value,
        field="output",
        allowed_roots=(base_rel,),
    )
    base = (game_dir / base_rel).resolve()
    if (
        path.parent != base
        or path.name in {"", ".", ".."}
        or not all(ch.isalnum() or ch in {"-", "_"} for ch in path.name)
    ):
        raise ToolError(
            "NON_CANONICAL_OUTPUT",
            "`output` must be `.playtest/verify/recording/<scenario>` with "
            "one safe scenario segment.",
        )
    return path


def _push(out: list[str], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            out.append(f"--{key}")
        return
    out.extend([f"--{key}", str(value)])


def _gca_env(ctx: ToolCtx) -> dict[str, str]:
    """Inherit os.environ and overlay resolved ARK VLM configuration."""
    env = os.environ.copy()
    env.setdefault("DOUBAO_API_ENDPOINT", ctx.config.doubao.endpoint)
    env.setdefault("DOUBAO_MODEL", ctx.config.doubao.model)
    policy = ctx.config.vlm_policy
    env.setdefault("VLM_ATTEMPTS_PER_MODEL", str(policy.attempts_per_model))
    env.setdefault("VLM_BACKOFF_SECONDS", str(policy.backoff_seconds))
    env.setdefault(
        "VLM_RATE_LIMIT_BACKOFF_SECONDS",
        str(policy.rate_limit_backoff_seconds),
    )
    return env


def _skip_video_result(tool: str) -> dict[str, Any]:
    """Uniform payload when `verify.skip_video` short-circuits a video tool.

    Returned instead of raising so a verify-agent that still calls the tool
    keeps flowing — the gates and hooks treat the video axes as N/A.
    """
    return {
        "skipped": True,
        "reason": (
            f"{tool} skipped: verify.skip_video is true in gca.config.yaml. "
            "Video verification (recording + analysis + performance) is "
            "disabled; rely on the no-video axes (screenshots, play-agent, "
            "alignment). Set verify.skip_video: false to re-enable."
        ),
    }


def _require_verify_run(
    ctx: ToolCtx,
    verify_run_id: Any,
) -> tuple[Path, dict[str, Any]]:
    game_dir = _base_dir(ctx)
    pipeline = _require_pipeline_identity(game_dir, ctx)
    run = require_current_run(
        game_dir,
        verify_run_id,
        allowed_statuses={"active"},
        pipeline_identity=pipeline,
    )
    load_runtime_token_receipt(game_dir, run)
    return game_dir, run


def _require_epoch_url(args: dict[str, Any], run: dict[str, Any]) -> str:
    url = normalize_epoch_url(args.get("url"))
    if url != run.get("url"):
        raise ToolError(
            "VERIFY_URL_MISMATCH",
            "record_with_state `url` must equal begin_verify_run's epoch URL.",
        )
    return url


def _checkpointed_video_handler(
    producer_name: str,
    handler: Callable[[dict[str, Any], ToolCtx], dict[str, Any]],
) -> Callable[[dict[str, Any], ToolCtx], dict[str, Any]]:
    def wrapped(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        game_dir = _base_dir(ctx)
        with verify_dag_flock(game_dir):
            game_dir, run = _require_verify_run(ctx, args.get("verify_run_id"))
            prepare_retry: Callable[[], None] | None = None
            if producer_name == "record_with_state":
                candidate = _recording_dir(game_dir, args.get("output"))
                instance = candidate.name

                def reset_recording() -> None:
                    if candidate.is_dir():
                        shutil.rmtree(candidate)
                    else:
                        candidate.unlink(missing_ok=True)

                prepare_retry = reset_recording
            else:
                raw_video = Path(str(args.get("video") or "default"))
                instance = raw_video.parent.name
                if not ctx.config.verify.skip_video:
                    video_path = _resolve_workspace_path(
                        game_dir,
                        args.get("video"),
                        field="video",
                        allowed_roots=(f".playtest/{layout.RECORDING_DIR}",),
                        must_exist=True,
                    )
                    _load_video_receipt(game_dir, run, video_path)
                    instance = video_path.parent.name

                    def reset_analysis() -> None:
                        (video_path.parent / "video-analysis.json").unlink(
                            missing_ok=True
                        )
                        evidence_dir = video_path.parent / "evidence"
                        if evidence_dir.is_dir():
                            shutil.rmtree(evidence_dir)

                    prepare_retry = reset_analysis
            return run_checkpointed(
                game_dir=game_dir,
                run=run,
                producer_name=producer_name,
                instance=instance,
                arguments={
                    **args,
                    "_checkpoint_policy": {
                        "skip_video": bool(ctx.config.verify.skip_video),
                    },
                },
                call=lambda: handler(args, ctx),
                prepare_retry=prepare_retry,
                reuse_completed=True,
            )

    return wrapped


def _load_video_receipt(
    game_dir: Path,
    run: dict[str, Any],
    video_path: Path,
) -> tuple[Path, dict[str, Any], Path, Path]:
    base = (game_dir / ".playtest" / layout.RECORDING_DIR).resolve()
    scenario = video_path.parent.name
    if (
        video_path.name != "recording.webm"
        or video_path.parent.parent != base
        or not video_path.is_file()
        or not scenario
        or not all(ch.isalnum() or ch in {"-", "_"} for ch in scenario)
    ):
        raise ToolError(
            "FOREIGN_VIDEO",
            "`video` must be a current canonical scenario recording.",
        )
    receipt_path = video_path.parent / layout.VIDEO_RECEIPT_NAME
    if receipt_path.is_symlink():
        raise ToolError(
            "VIDEO_RECEIPT_INVALID",
            "The selected video receipt may not be a symlink.",
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "VIDEO_RECEIPT_INVALID",
            "The selected video has no valid current video receipt.",
        ) from exc
    identity = identity_from_run(run)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("producer") != "gca-mcp.record_with_state"
        or any(receipt.get(key) != value for key, value in identity.items())
        or receipt.get("url") != run.get("url")
        or receipt.get("runtime_token_sha256") != run.get("runtime_token_sha256")
        or receipt.get("runtime_receipt_sha256") != run.get("runtime_receipt_sha256")
        or receipt.get("video_path") != _rel(game_dir, video_path)
        or receipt.get("video_sha256") != sha256_file(video_path)
    ):
        raise ToolError(
            "VIDEO_RECEIPT_INVALID",
            "The selected video receipt is stale, altered, or cross-run.",
        )
    validate_runtime_evidence(receipt.get("runtime_identity"), run)
    timeline_path = (game_dir / str(receipt.get("timeline_path", ""))).resolve()
    performance_path = (game_dir / str(receipt.get("performance_path", ""))).resolve()
    if (
        timeline_path.parent != video_path.parent
        or performance_path.parent != video_path.parent
        or timeline_path.name != "state-timeline.json"
        or performance_path.name != "performance-summary.json"
        or not timeline_path.is_file()
        or not performance_path.is_file()
        or receipt.get("timeline_sha256") != sha256_file(timeline_path)
        or receipt.get("performance_sha256") != sha256_file(performance_path)
    ):
        raise ToolError(
            "VIDEO_RECEIPT_INVALID",
            "Video receipt timeline/performance hashes do not match current files.",
        )
    return receipt_path, receipt, timeline_path, performance_path


# -------------------- analyze_video --------------------


def _analyze_video(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = _base_dir(ctx)
    with verify_flock(game_dir, exclusive=False):
        game_dir, run = _require_verify_run(ctx, args.get("verify_run_id"))
        identity = identity_from_run(run)
        video_path = _resolve_workspace_path(
            game_dir,
            args.get("video"),
            field="video",
            allowed_roots=(f".playtest/{layout.RECORDING_DIR}",),
            must_exist=True,
        )
        receipt_path, receipt, timeline_path, _performance_path = _load_video_receipt(
            game_dir, run, video_path
        )
        if ctx.config.verify.skip_video:
            return {
                **_skip_video_result("analyze_video"),
                **identity,
                "video_receipt": _rel(game_dir, receipt_path),
            }

        canonical_output = video_path.parent / "video-analysis.json"
        output_path = _resolve_workspace_path(
            game_dir,
            args.get("output"),
            field="output",
            allowed_roots=(f".playtest/{layout.RECORDING_DIR}",),
            default=_rel(game_dir, canonical_output),
        )
        if output_path != canonical_output:
            raise ToolError(
                "NON_CANONICAL_OUTPUT",
                "`output` must be video-analysis.json beside the current recording.",
            )
        if args.get("state_timeline") is not None:
            supplied_timeline = _resolve_workspace_path(
                game_dir,
                args["state_timeline"],
                field="state_timeline",
                allowed_roots=(f".playtest/{layout.RECORDING_DIR}",),
                must_exist=True,
            )
            if supplied_timeline != timeline_path:
                raise ToolError(
                    "VIDEO_RECEIPT_INVALID",
                    "state_timeline must be the timeline bound by the video receipt.",
                )

        temporary_output = output_path.with_name(
            f".{output_path.name}.{uuid.uuid4().hex}.tmp"
        )
        script = runner_path("verify", "gemini-video-analyze.mjs")
        cli: list[str] = [
            "--video",
            str(video_path),
            "--output",
            str(temporary_output),
            "--state-timeline",
            str(timeline_path),
        ]
        for key in ("rubric",):
            if args.get(key) is None:
                continue
            path = _resolve_workspace_path(
                game_dir,
                args[key],
                field=key,
                allowed_roots=(f".playtest/{layout.INPUTS_DIR}",),
                must_exist=True,
            )
            cli.extend([f"--{key.replace('_', '-')}", str(path)])
        if args.get("prompt") is not None:
            cli.extend(["--prompt", str(args["prompt"])])
        for key in (
            "endpoint",
            "timeout",
            "max_tokens",
            "segment_duration",
            "max_parallel",
        ):
            value = args.get(key)
            if value is not None:
                cli.extend([f"--{key.replace('_', '-')}", str(value)])
        if args.get("no_recheck"):
            cli.append("--no-recheck")
        if args.get("split"):
            cli.append("--split")

        try:
            proc = run_node_script(
                script,
                cli,
                env=_gca_env(ctx),
                cwd=game_dir,
                timeout=float(
                    args.get(
                        "timeout_seconds",
                        ctx.config.verify.video_analysis_timeout_seconds,
                    )
                ),
                check=False,
            )
            if proc["returncode"] != 0:
                raise ToolError(
                    "VLM_CALL_FAILED",
                    "video analysis runner failed.",
                    hint=proc["stderr"][-500:],
                )
            try:
                analysis = json.loads(temporary_output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Video analyzer did not produce a valid JSON object.",
                ) from exc
            if not isinstance(analysis, dict):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Video analyzer output must be a JSON object.",
                )
            analysis_text = analysis.get("analysis")
            analysis_errors = analysis.get("analysis_errors")
            segment_analyses = analysis.get("segment_analyses")
            if (
                analysis.get("analysis_status") != "completed"
                or not isinstance(analysis_text, str)
                or not analysis_text.strip()
                or analysis_text.lstrip().lower().startswith("analysis failed")
                or not isinstance(analysis_errors, list)
                or analysis_errors
                or not isinstance(segment_analyses, list)
                or not segment_analyses
                or any(
                    not isinstance(item, dict)
                    or item.get("error")
                    or not isinstance(item.get("analysis"), str)
                    or not item["analysis"].strip()
                    for item in segment_analyses
                )
            ):
                raise ToolError(
                    "VIDEO_ANALYSIS_INCOMPLETE",
                    "Video analyzer did not produce a complete successful analysis.",
                    hint=str(analysis_errors or analysis_text or "")[-500:],
                )
            analysis["video"] = _rel(game_dir, video_path)
            evidence_dir = analysis.get("evidence_dir")
            if isinstance(evidence_dir, str) and evidence_dir:
                evidence_candidate = Path(evidence_dir).expanduser()
                if not evidence_candidate.is_absolute():
                    evidence_candidate = game_dir / evidence_candidate
                resolved_evidence = evidence_candidate.resolve()
                if (
                    not resolved_evidence.is_relative_to(video_path.parent)
                    or resolved_evidence.name != "evidence"
                ):
                    raise ToolError(
                        "PATH_ESCAPE",
                        "Video analyzer evidence_dir escaped its canonical scenario.",
                    )
                analysis["evidence_dir"] = _rel(game_dir, resolved_evidence)
            analysis.update(
                {
                    "schema_version": max(int(analysis.get("schema_version", 0)), 1),
                    "producer": "gca-mcp.analyze_video",
                    **identity,
                    "url": run["url"],
                    "runtime_token_sha256": run["runtime_token_sha256"],
                    "runtime_receipt_sha256": run["runtime_receipt_sha256"],
                    "runtime_identity": receipt["runtime_identity"],
                    "video_path": _rel(game_dir, video_path),
                    "video_sha256": receipt["video_sha256"],
                    "timeline_path": _rel(game_dir, timeline_path),
                    "timeline_sha256": receipt["timeline_sha256"],
                    "video_receipt": _rel(game_dir, receipt_path),
                    "video_receipt_sha256": sha256_file(receipt_path),
                }
            )
            atomic_write_json(output_path, analysis)
        finally:
            temporary_output.unlink(missing_ok=True)
        return {
            "output": _rel(game_dir, output_path),
            "video_receipt": _rel(game_dir, receipt_path),
            "video_sha256": receipt["video_sha256"],
            "stdout": proc["stdout"][-2000:],
            "stderr_tail": proc["stderr"][-1000:],
            **identity,
        }


# -------------------- record_with_state --------------------


def _record_with_state(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    game_dir = _base_dir(ctx)
    # Recording owns one scenario directory, so use the exclusive epoch lock to
    # prevent two processes from clobbering the same browser/video outputs.
    with verify_flock(game_dir, exclusive=True):
        game_dir, run = _require_verify_run(ctx, args.get("verify_run_id"))
        identity = identity_from_run(run)
        url = _require_epoch_url(args, run)
        output = _recording_dir(game_dir, args.get("output"))
        if ctx.config.verify.skip_video:
            return {
                **_skip_video_result("record_with_state"),
                **identity,
                "url": url,
                "output": _rel(game_dir, output),
            }
        if output.exists() and any(output.iterdir()):
            raise ToolError(
                "OUTPUT_EXISTS",
                "The scenario recording directory already contains output; "
                "begin a new verify run or choose a new scenario.",
            )

        script = runner_path("verify", "record-with-state.mjs")
        cli: list[str] = [
            "--url",
            url,
            "--output",
            str(output),
            *runtime_cli_args(game_dir, run),
        ]
        if args.get("script") is not None:
            interaction = _resolve_workspace_path(
                game_dir,
                args["script"],
                field="script",
                allowed_roots=(f".playtest/{layout.INTERACTIONS_DIR}",),
                must_exist=True,
            )
            cli.extend(["--script", str(interaction)])
        seed = args.get("seed")
        if seed is not None:
            try:
                parsed_seed = json.loads(str(seed))
            except json.JSONDecodeError:
                parsed_seed = None
            if not isinstance(parsed_seed, dict):
                seed_path = _resolve_workspace_path(
                    game_dir,
                    seed,
                    field="seed",
                    allowed_roots=(f".playtest/{layout.SCENARIOS_DIR}",),
                    must_exist=True,
                )
                try:
                    parsed_seed = json.loads(seed_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ToolError(
                        "INVALID_ARTIFACT",
                        "Scenario seed must be a JSON object.",
                    ) from exc
            if not isinstance(parsed_seed, dict):
                raise ToolError(
                    "INVALID_ARTIFACT",
                    "Scenario seed must be a JSON object.",
                )
            if isinstance(parsed_seed.get("js"), str):
                parsed_seed["js"] = str(
                    _resolve_workspace_path(
                        game_dir,
                        parsed_seed["js"],
                        field="seed.js",
                        allowed_roots=(f".playtest/{layout.INTERACTIONS_DIR}",),
                        must_exist=True,
                    )
                )
            if isinstance(parsed_seed.get("snapshot"), str):
                parsed_seed["snapshot"] = str(
                    _resolve_workspace_path(
                        game_dir,
                        parsed_seed["snapshot"],
                        field="seed.snapshot",
                        allowed_roots=(f".playtest/{layout.SCENARIOS_DIR}",),
                        must_exist=True,
                    )
                )
            cli.extend(["--seed", json.dumps(parsed_seed)])
        _push(
            cli,
            "duration",
            args.get("duration", ctx.config.verify.record_duration_seconds),
        )
        _push(cli, "interval", args.get("interval"))
        _push(cli, "width", args.get("width"))
        _push(cli, "height", args.get("height"))

        proc = run_node_script(
            script,
            cli,
            env=_gca_env(ctx),
            cwd=game_dir,
            timeout=float(
                args.get("timeout_seconds", ctx.config.verify.record_timeout_seconds)
            ),
            check=False,
        )
        if proc["returncode"] != 0:
            shutil.rmtree(output, ignore_errors=True)
            raise ToolError(
                "POSTPROCESS_FAILED",
                "record-with-state failed.",
                hint=proc["stderr"][-500:],
            )
        runner_result = _parse_json_output(proc.get("stdout", ""))
        if not isinstance(runner_result, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                "record-with-state did not return structured runtime evidence.",
            )
        runtime_identity = validate_runtime_evidence(
            runner_result.get("runtime_identity"),
            run,
        )
        video_path = output / "recording.webm"
        timeline_path = output / "state-timeline.json"
        performance_path = output / "performance-summary.json"
        if (
            not video_path.is_file()
            or video_path.stat().st_size <= 0
            or not timeline_path.is_file()
            or not performance_path.is_file()
        ):
            raise ToolError(
                "MISSING_ARTIFACT",
                "record_with_state did not produce video, timeline, and performance files.",
            )
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            performance = json.loads(performance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(
                "INVALID_ARTIFACT",
                "record_with_state produced invalid JSON evidence.",
            ) from exc
        if not isinstance(timeline, list) or not isinstance(performance, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                "Timeline must be an array and performance summary an object.",
            )
        validate_runtime_evidence(performance.get("runtime_identity"), run)
        performance.update(
            {
                **identity,
                "url": url,
                "runtime_token_sha256": run["runtime_token_sha256"],
                "runtime_receipt_sha256": run["runtime_receipt_sha256"],
            }
        )
        atomic_write_json(performance_path, performance)

        receipt_path = output / layout.VIDEO_RECEIPT_NAME
        receipt = {
            "schema_version": 1,
            "producer": "gca-mcp.record_with_state",
            "generated_at": performance.get("generated_at") or run.get("started_at"),
            **identity,
            "url": url,
            "runtime_token_sha256": run["runtime_token_sha256"],
            "runtime_receipt_sha256": run["runtime_receipt_sha256"],
            "runtime_identity": runtime_identity,
            "video_path": _rel(game_dir, video_path),
            "video_sha256": sha256_file(video_path),
            "timeline_path": _rel(game_dir, timeline_path),
            "timeline_sha256": sha256_file(timeline_path),
            "performance_path": _rel(game_dir, performance_path),
            "performance_sha256": sha256_file(performance_path),
        }
        atomic_write_json(receipt_path, receipt)
        return {
            "stdout": proc["stdout"][-2000:],
            "output": _rel(game_dir, output),
            "video_receipt": _rel(game_dir, receipt_path),
            "video_sha256": receipt["video_sha256"],
            "timeline_sha256": receipt["timeline_sha256"],
            "performance_sha256": receipt["performance_sha256"],
            "runtime_identity": runtime_identity,
            **identity,
        }


# -------------------- get_state_extractor --------------------


def _get_state_extractor(_args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    """Return the absolute path + source of the bundled ``state-extractor.mjs``.

    Agent-written test scripts can ``import { EXTRACTOR, … } from
    '<path>'`` to inspect Phaser game state inside ``page.evaluate``.
    Returning the source as a string is also handy when the test file
    is generated dynamically.
    """
    path = runner_path("verify", "state-extractor.mjs")
    return {
        "path": str(path),
        "source": path.read_text(encoding="utf-8"),
    }


TOOLS = [
    ToolDef(
        name="analyze_video",
        description=(
            "Analyze only a recording sealed by record_with_state's current-run "
            "video receipt. Rejects old/foreign videos and hash mismatches. "
            "The canonical video-analysis.json is atomically written beside the "
            "recording. "
            "Uses the pinned Doubao 2.0 native video input through ARK. "
            "Default mode is DIRECT whole-video understanding: the entire "
            "recording is sent in a SINGLE pass (no ffmpeg splitting, no "
            "per-segment calls, no separate merge) and the one call returns the "
            "final consolidated report — much faster than the legacy pipeline. "
            "Pass split=true to use the legacy segmented pipeline (split → "
            "per-segment analysis → rubric evidence re-check → merge); "
            "segment_duration/max_parallel/rubric/no_recheck only apply then. "
            "Reads ARK_API_KEY and DOUBAO_API_ENDPOINT from env (the server "
            "forwards its resolved config). Returns {skipped:true} without "
            "running when verify.skip_video is true in gca.config.yaml."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "video": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "rubric": {"type": "string"},
                "output": {"type": "string"},
                "state_timeline": {"type": "string"},
                "prompt": {"type": "string"},
                "endpoint": {"type": "string"},
                "timeout": {"type": "string"},
                "max_tokens": {"type": "string"},
                "segment_duration": {"type": "string"},
                "max_parallel": {"type": "string"},
                "no_recheck": {"type": "boolean", "default": False},
                "split": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Use the legacy segmented pipeline instead of direct "
                        "whole-video analysis. Default false (direct)."
                    ),
                },
                "timeout_seconds": {"type": "number", "default": 1800},
            },
            "required": ["video", "verify_run_id"],
            "additionalProperties": False,
        },
        handler=_checkpointed_video_handler("analyze_video", _analyze_video),
        requires_workspace=True,
    ),
    ToolDef(
        name="record_with_state",
        description=(
            "Record a Phaser game running at `url` while sampling "
            "window.__phaserGame state every `interval` ms. Produces a "
            ".webm video, state-timeline.json, performance-summary.json, and an "
            "atomic video-receipt.json bound to hashes, epoch URL, runtime token, "
            "pipeline identity, and verify_run_id. Output must be one canonical "
            "`.playtest/verify/recording/<scenario>` directory. "
            "Returns {skipped:true} without running when "
            "verify.skip_video is true in gca.config.yaml."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "verify_run_id": {
                    "type": "string",
                    "description": "Exact ID returned by begin_verify_run.",
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Canonical scenario directory: "
                        ".playtest/verify/recording/<scenario>."
                    ),
                },
                "script": {"type": "string"},
                "seed": {
                    "type": "string",
                    "description": (
                        "Optional scenario seed: path to a JSON file (or inline "
                        "JSON) with the same schema as play-agent objective.setup "
                        "({scene, stop, data, state, eval, js, snapshot}). Warps "
                        "the game to an intermediate state (boss fight, low HP, "
                        "late level) "
                        "right after boot, before the recording window opens — so "
                        "the recording captures the scenario, not a fresh start. "
                        "Different verify passes can seed different scenarios."
                    ),
                },
                "duration": {"type": "integer", "default": 60},
                "interval": {"type": "integer", "default": 1000},
                "width": {"type": "integer", "default": 800},
                "height": {"type": "integer", "default": 600},
                "timeout_seconds": {"type": "number", "default": 600},
            },
            "required": ["url", "verify_run_id", "output"],
            "additionalProperties": False,
        },
        handler=_checkpointed_video_handler("record_with_state", _record_with_state),
        requires_workspace=True,
    ),
    ToolDef(
        name="get_state_extractor",
        description=(
            "Return the path + source of the bundled state-extractor.mjs. "
            "Agent-written Playwright tests `import { EXTRACTOR, "
            "extractStateCompact } from '<path>'` to inspect Phaser state "
            "inside page.evaluate()."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_get_state_extractor,
    ),
]
