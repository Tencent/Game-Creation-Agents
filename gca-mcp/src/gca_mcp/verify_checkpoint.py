"""Identity-bound checkpoints for independently resumable verify producers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import playtest_layout as layout
from .errors import ToolError
from .evidence import atomic_write_json
from .verify_epoch import IDENTITY_KEYS, canonical_playtest_path

_SAFE_NAME = re.compile(r"[^a-z0-9_-]+")
_ARTIFACT_KEYS = {
    "alignment_path",
    "output",
    "result_path",
    "report_path",
    "receipt",
    "video_receipt",
}
_ARTIFACT_LIST_KEYS = {
    "captured_scenes",
    "outputs",
}
_OMITTED_RESULT_KEYS = {
    "command",
    "source",
    "stdout",
    "stderr",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def checkpoint_name(producer_name: str, instance: str | None = None) -> str:
    raw = producer_name if not instance else f"{producer_name}-{instance}"
    normalized = _SAFE_NAME.sub("-", raw.lower()).strip("-")
    if not normalized:
        raise ToolError("INVALID_CONFIG", "Verify checkpoint name is empty.")
    return normalized[:120]


def checkpoint_path(
    game_dir: Path,
    producer_name: str,
    *,
    instance: str | None = None,
) -> Path:
    name = checkpoint_name(producer_name, instance)
    return canonical_playtest_path(
        game_dir,
        f"{layout.VERIFY_CHECKPOINTS_DIR}/{name}.json",
    )


def _input_sha256(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<truncated>"
    if isinstance(value, dict):
        return {
            str(key): _compact(child, depth=depth + 1)
            for key, child in value.items()
            if key not in _OMITTED_RESULT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_compact(child, depth=depth + 1) for child in value[:100]]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= 4000 else f"{value[:4000]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _artifact_candidates(result: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in _ARTIFACT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    for key in _ARTIFACT_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, str) and item)
        elif isinstance(value, dict):
            candidates.extend(
                item for item in value.values() if isinstance(item, str) and item
            )
    return sorted(set(candidates))


def _artifact_exists(game_dir: Path, value: str) -> bool:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = game_dir / candidate
    resolved = candidate.resolve()
    return resolved.is_relative_to(game_dir.resolve()) and resolved.exists()


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _matches_run(checkpoint: dict[str, Any], run: dict[str, Any]) -> bool:
    return all(checkpoint.get(key) == run.get(key) for key in IDENTITY_KEYS)


def _reusable(
    checkpoint: dict[str, Any] | None,
    *,
    game_dir: Path,
    run: dict[str, Any],
    input_sha256: str,
) -> bool:
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version") != 1
        or checkpoint.get("producer") != "gca-mcp.verify_checkpoint"
        or checkpoint.get("status") != "completed"
        or checkpoint.get("outcome") not in {"passed", "skipped"}
        or checkpoint.get("input_sha256") != input_sha256
        or not _matches_run(checkpoint, run)
        or not isinstance(checkpoint.get("result"), dict)
    ):
        return False
    artifacts = checkpoint.get("artifacts")
    if checkpoint["result"].get("skipped") is True:
        return True
    return (
        isinstance(artifacts, list)
        and bool(artifacts)
        and all(
            isinstance(artifact, str) and _artifact_exists(game_dir, artifact)
            for artifact in artifacts
        )
    )


def run_checkpointed(
    *,
    game_dir: Path,
    run: dict[str, Any],
    producer_name: str,
    arguments: dict[str, Any],
    call: Callable[[], dict[str, Any]],
    prepare_retry: Callable[[], None] | None = None,
    instance: str | None = None,
    reuse_completed: bool = False,
) -> dict[str, Any]:
    """Run one producer, atomically checkpointing start, success, and failure."""
    path = checkpoint_path(game_dir, producer_name, instance=instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = _input_sha256(arguments)
    previous = read_checkpoint(path)
    if reuse_completed and _reusable(
        previous,
        game_dir=game_dir,
        run=run,
        input_sha256=fingerprint,
    ):
        return {
            **previous["result"],
            "checkpoint": str(path.relative_to(game_dir)),
            "checkpoint_reused": True,
        }

    attempt = (
        int(previous.get("attempt", 0)) + 1
        if isinstance(previous, dict) and _matches_run(previous, run)
        else 1
    )
    started_at = _utc_now()
    started = time.monotonic()
    base = {
        "schema_version": 1,
        "producer": "gca-mcp.verify_checkpoint",
        "producer_name": producer_name,
        "checkpoint_name": path.stem,
        **{key: run[key] for key in IDENTITY_KEYS},
        "mode": run.get("mode"),
        "url": run.get("url"),
        "input_sha256": fingerprint,
        "attempt": attempt,
        "started_at": started_at,
    }
    atomic_write_json(path, {**base, "status": "running"})

    try:
        if prepare_retry is not None:
            prepare_retry()
        result = call()
        if not isinstance(result, dict):
            raise ToolError(
                "INVALID_ARTIFACT",
                f"{producer_name} returned a non-object result.",
            )
    except ToolError as exc:
        atomic_write_json(
            path,
            {
                **base,
                "status": "failed",
                "completed_at": _utc_now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "error": {
                    "code": str(exc.code),
                    "message": exc.message,
                    "hint": exc.hint,
                },
            },
        )
        raise
    except Exception as exc:
        atomic_write_json(
            path,
            {
                **base,
                "status": "failed",
                "completed_at": _utc_now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "hint": None,
                },
            },
        )
        raise

    compact_result = _compact(result)
    artifacts = _artifact_candidates(compact_result)
    atomic_write_json(
        path,
        {
            **base,
            "status": "completed",
            "outcome": (
                "blocked"
                if result.get("blocked") is True
                or result.get("returncode") not in (None, 0)
                else "skipped"
                if result.get("skipped") is True
                else "passed"
            ),
            "completed_at": _utc_now(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "artifacts": artifacts,
            "result": compact_result,
        },
    )
    return {
        **result,
        "checkpoint": str(path.relative_to(game_dir)),
        "checkpoint_reused": False,
    }


def load_checkpoints(game_dir: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    root = canonical_playtest_path(game_dir, layout.VERIFY_CHECKPOINTS_DIR)
    if not root.is_dir():
        return []
    checkpoints = []
    for path in sorted(root.glob("*.json")):
        checkpoint = read_checkpoint(path)
        if isinstance(checkpoint, dict) and _matches_run(checkpoint, run):
            checkpoints.append(
                {
                    **checkpoint,
                    "path": str(path.relative_to(game_dir)),
                }
            )
    return checkpoints


def checkpoint_issues(checkpoints: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for checkpoint in checkpoints:
        status = checkpoint.get("status")
        if status == "completed" and checkpoint.get("outcome") == "blocked":
            result = (
                checkpoint.get("result")
                if isinstance(checkpoint.get("result"), dict)
                else {}
            )
            messages: list[str] = []
            for key in ("blocking_messages", "issues"):
                entries = result.get(key)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    message = (
                        entry.get("description") or entry.get("message")
                        if isinstance(entry, dict)
                        else entry
                    )
                    if isinstance(message, str) and message.strip():
                        messages.append(message.strip())
            producer_name = str(
                checkpoint.get("producer_name") or checkpoint.get("checkpoint_name")
            )
            issues.append(
                {
                    "severity": "high",
                    "description": f"{producer_name} reported a blocking result",
                    "root_cause": (
                        "; ".join(dict.fromkeys(messages))
                        if messages
                        else str(result.get("stderr_tail") or "").strip()
                        or "See the producer checkpoint and artifact"
                    ),
                }
            )
            continue
        if status not in {"failed", "running"}:
            continue
        error = (
            checkpoint.get("error") if isinstance(checkpoint.get("error"), dict) else {}
        )
        producer_name = str(
            checkpoint.get("producer_name") or checkpoint.get("checkpoint_name")
        )
        description = (
            f"{producer_name} failed: {error.get('code', 'INTERNAL_ERROR')}"
            if status == "failed"
            else f"{producer_name} was interrupted before sealing its checkpoint"
        )
        root_cause = str(
            error.get("message")
            or "The producer checkpoint remained running when the verify pass was finalized."
        )
        issues.append(
            {
                "severity": "high",
                "description": description,
                "root_cause": root_cause,
            }
        )
    return issues


__all__ = [
    "checkpoint_issues",
    "checkpoint_name",
    "checkpoint_path",
    "load_checkpoints",
    "read_checkpoint",
    "run_checkpointed",
]
