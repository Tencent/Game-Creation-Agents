"""Cross-process verification epochs and browser runtime identity binding."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from . import playtest_layout as layout
from .errors import ToolError
from .evidence import atomic_write_json, secure_file_lock

IDENTITY_KEYS = ("pipeline_run_id", "build_digest", "verify_run_id")


def canonical_playtest_path(game_dir: Path, relative: str = "") -> Path:
    """Resolve an internal ``.playtest`` path without following symlink escapes."""
    root = Path(os.path.abspath(game_dir))
    if root.resolve() != root:
        raise ToolError(
            "NON_CANONICAL_PATH",
            f"Active workspace root may not be replaced or symlinked: {root}.",
        )
    candidate = root / ".playtest" / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ToolError(
            "PATH_ESCAPE",
            f"Canonical .playtest path escapes the active workspace: {relative or '.'}.",
        )
    if resolved != candidate:
        raise ToolError(
            "NON_CANONICAL_PATH",
            f"Canonical .playtest path may not traverse symlinks: {relative or '.'}.",
        )
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_epoch_url(value: Any) -> str:
    """Return one canonical HTTP(S) workspace URL or fail closed."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            "VERIFY_URL_REQUIRED",
            "A non-empty `url` is required for every verification epoch.",
        )
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as exc:
        raise ToolError(
            "INVALID_VERIFY_URL", f"Invalid verification URL: {exc}"
        ) from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ToolError(
            "INVALID_VERIFY_URL",
            "Verification `url` must be an absolute HTTP(S) URL without "
            "credentials or a fragment.",
        )
    path = parts.path or "/"
    if path != "/" and not path.endswith("/"):
        raise ToolError(
            "INVALID_VERIFY_URL",
            "Verification `url` paths must end with `/` so the workspace "
            "runtime-token receipt has an unambiguous served location.",
        )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            parts.query,
            "",
        )
    )


def runtime_token_url(epoch_url: str, verify_run_id: str) -> str:
    parts = urlsplit(normalize_epoch_url(epoch_url))
    token_path = (
        f"{parts.path}.playtest/{layout.RUNTIME_TOKEN_RECEIPT}"
        if parts.path.endswith("/")
        else f"{parts.path}/.playtest/{layout.RUNTIME_TOKEN_RECEIPT}"
    )
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            token_path,
            f"verify_run_id={quote(verify_run_id, safe='')}",
            "",
        )
    )


@contextmanager
def verify_flock(game_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the workspace verify flock across a producer or epoch transition.

    Producers take a shared lock, so independent browsers can still run in
    parallel. ``begin_verify_run`` and ``write_verify_report`` take the
    exclusive lock, making active-run CAS, cleanup, and finalization mutually
    exclusive with every producer in this process or another MCP process.
    """
    playtest_dir = canonical_playtest_path(game_dir)
    playtest_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = canonical_playtest_path(game_dir, "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "verify.lock"
    with secure_file_lock(lock_path, exclusive=exclusive):
        # Revalidate the authority path after taking the stable external lock.
        canonical_playtest_path(game_dir)
        yield


@contextmanager
def verify_dag_flock(game_dir: Path) -> Iterator[None]:
    """Allow only one canonical DAG invocation per workspace across processes."""
    playtest_dir = canonical_playtest_path(game_dir)
    playtest_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = canonical_playtest_path(game_dir, "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "verify-dag.lock"
    with secure_file_lock(lock_path, exclusive=True):
        canonical_playtest_path(game_dir)
        yield


@contextmanager
def build_mutation_flock(
    game_dir: Path,
    *,
    exclusive: bool = True,
) -> Iterator[None]:
    """Fence build writes against verify; optionally share across safe writers."""

    with verify_flock(game_dir, exclusive=exclusive):
        current = read_current_run(game_dir)
        if isinstance(current, dict) and current.get("status") == "active":
            raise ToolError(
                "VERIFY_RUN_ACTIVE",
                "Build inputs cannot change during an active verification epoch. "
                "Finalize the run before mutating the build.",
            )
        yield


def read_current_run(game_dir: Path) -> dict[str, Any] | None:
    path = canonical_playtest_path(game_dir, layout.VERIFY_RUN)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "VERIFY_RUN_STATE_INVALID",
            "current-run.json exists but is unreadable or invalid JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise ToolError(
            "VERIFY_RUN_STATE_INVALID",
            "current-run.json must contain a JSON object.",
        )
    return value


def require_current_run(
    game_dir: Path,
    verify_run_id: Any,
    *,
    allowed_statuses: set[str],
    pipeline_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate explicit caller identity against the current epoch snapshot."""
    if not isinstance(verify_run_id, str) or not verify_run_id:
        raise ToolError(
            "VERIFY_RUN_ID_REQUIRED",
            "Pass the exact `verify_run_id` returned by begin_verify_run.",
        )
    run = read_current_run(game_dir)
    if run is None:
        raise ToolError(
            "VERIFY_RUN_NOT_STARTED",
            "Call begin_verify_run before producing verification evidence.",
        )
    if run.get("verify_run_id") != verify_run_id:
        raise ToolError(
            "VERIFY_RUN_ID_MISMATCH",
            "The supplied verify_run_id is stale or belongs to another workspace run.",
        )
    if any(not isinstance(run.get(key), str) or not run[key] for key in IDENTITY_KEYS):
        raise ToolError(
            "VERIFY_RUN_IDENTITY_MISMATCH",
            "current-run.json is missing its complete verification identity.",
        )
    if pipeline_identity is not None and any(
        run.get(key) != pipeline_identity.get(key)
        for key in ("pipeline_run_id", "build_digest")
    ):
        raise ToolError(
            "VERIFY_RUN_IDENTITY_MISMATCH",
            "current-run.json does not match the active pipeline build.",
        )
    status = run.get("status")
    if status not in allowed_statuses:
        if status == "finalized":
            raise ToolError(
                "VERIFY_RUN_FINALIZED",
                "This verification run is finalized; begin a new run before "
                "producing more evidence.",
            )
        raise ToolError(
            "VERIFY_RUN_NOT_ACTIVE",
            f"Verification run status {status!r} is not allowed for this operation.",
        )
    normalized_url = normalize_epoch_url(run.get("url"))
    if run.get("url") != normalized_url:
        raise ToolError(
            "VERIFY_RUN_IDENTITY_MISMATCH",
            "current-run.json contains a non-canonical verification URL.",
        )
    return run


def create_runtime_token_receipt(
    game_dir: Path,
    *,
    identity: dict[str, str],
    url: str,
    generated_at: str,
) -> tuple[dict[str, Any], Path, str]:
    """Create a non-build-input nonce file that the served browser must fetch."""
    token = secrets.token_urlsafe(32)
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_url = runtime_token_url(url, identity["verify_run_id"])
    payload: dict[str, Any] = {
        "schema_version": 1,
        "producer": "gca-mcp.begin_verify_run",
        **identity,
        "url": url,
        "runtime_token": token,
        "runtime_token_sha256": token_sha256,
        "runtime_token_url": token_url,
        "generated_at": generated_at,
    }
    path = canonical_playtest_path(game_dir, layout.RUNTIME_TOKEN_RECEIPT)
    atomic_write_json(path, payload)
    return payload, path, sha256_file(path)


def load_runtime_token_receipt(
    game_dir: Path,
    run: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    path = canonical_playtest_path(game_dir, layout.RUNTIME_TOKEN_RECEIPT)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "RUNTIME_TOKEN_RECEIPT_INVALID",
            "The current runtime-token receipt is missing or invalid.",
        ) from exc
    if not isinstance(payload, dict):
        raise ToolError(
            "RUNTIME_TOKEN_RECEIPT_INVALID",
            "The current runtime-token receipt must be a JSON object.",
        )
    token = payload.get("runtime_token")
    actual_token_sha = (
        hashlib.sha256(token.encode("utf-8")).hexdigest()
        if isinstance(token, str) and token
        else None
    )
    actual_receipt_sha = sha256_file(path)
    if (
        payload.get("schema_version") != 1
        or payload.get("producer") != "gca-mcp.begin_verify_run"
        or any(payload.get(key) != run.get(key) for key in IDENTITY_KEYS)
        or payload.get("url") != run.get("url")
        or payload.get("runtime_token_url") != run.get("runtime_token_url")
        or actual_token_sha != payload.get("runtime_token_sha256")
        or actual_token_sha != run.get("runtime_token_sha256")
        or actual_receipt_sha != run.get("runtime_receipt_sha256")
    ):
        raise ToolError(
            "RUNTIME_TOKEN_RECEIPT_INVALID",
            "The runtime-token receipt is stale, altered, or cross-run.",
        )
    return payload, path


def runtime_cli_args(game_dir: Path, run: dict[str, Any]) -> list[str]:
    receipt, _ = load_runtime_token_receipt(game_dir, run)
    return [
        "--epoch-url",
        str(run["url"]),
        "--runtime-token-url",
        str(run["runtime_token_url"]),
        "--runtime-token",
        str(receipt["runtime_token"]),
        "--runtime-receipt-sha256",
        str(run["runtime_receipt_sha256"]),
        "--pipeline-run-id",
        str(run["pipeline_run_id"]),
        "--build-digest",
        str(run["build_digest"]),
        "--verify-run-id",
        str(run["verify_run_id"]),
    ]


def validate_runtime_evidence(
    evidence: Any,
    run: dict[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != 1
        or evidence.get("source") != "runtime-identity.mjs"
        or evidence.get("verified") is not True
    ):
        raise ToolError(
            "RUNTIME_IDENTITY_MISSING",
            "The browser producer did not return verified runtime identity evidence.",
        )
    expected = {
        "epoch_url": run["url"],
        "runtime_token_url": run["runtime_token_url"],
        "runtime_token_sha256": run["runtime_token_sha256"],
        "runtime_receipt_sha256": run["runtime_receipt_sha256"],
        "pipeline_run_id": run["pipeline_run_id"],
        "build_digest": run["build_digest"],
        "verify_run_id": run["verify_run_id"],
        "page_url": run["url"],
        "fetched_url": run["runtime_token_url"],
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ToolError(
            "RUNTIME_IDENTITY_MISMATCH",
            "The browser fetched a runtime token for another URL, workspace, or run.",
        )
    return evidence


def identity_from_run(run: dict[str, Any]) -> dict[str, str]:
    return {key: str(run[key]) for key in IDENTITY_KEYS}


__all__ = [
    "IDENTITY_KEYS",
    "build_mutation_flock",
    "canonical_playtest_path",
    "create_runtime_token_receipt",
    "identity_from_run",
    "load_runtime_token_receipt",
    "normalize_epoch_url",
    "read_current_run",
    "require_current_run",
    "runtime_cli_args",
    "runtime_token_url",
    "sha256_file",
    "validate_runtime_evidence",
    "verify_dag_flock",
    "verify_flock",
]
