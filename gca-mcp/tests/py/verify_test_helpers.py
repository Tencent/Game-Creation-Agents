from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gca_mcp import playtest_layout as layout
from gca_mcp.config import Config
from gca_mcp.evidence import compute_build_digest
from gca_mcp.tools import verify_tools

_RESUME_IDENTITY_KEYS = (
    "pipeline_run_id",
    "build_digest",
    "state_transaction_id",
    "genre",
    "interaction_profile",
    "quality_profile",
    "level_model",
)


def write_pipeline_identity(
    root: Path,
    digest: str,
    *,
    pipeline_run_id: str = "pipeline-test",
    state_transaction_id: str = "transaction-test",
) -> None:
    playtest = root / ".playtest"
    playtest.mkdir(parents=True, exist_ok=True)
    state = {
        "pipeline_run_id": pipeline_run_id,
        "build_digest": digest,
        "state_transaction_id": state_transaction_id,
        "genre": "custom",
        "interaction_profile": "none",
        "quality_profile": "art_directed",
        "level_model": "none",
        "level_design_required": False,
        "verify_runs": 0,
        "iteration_count": 0,
        "audit_offsets_version": 1,
        "audit_offsets": {"verify_runs": 0, "iteration_count": 0},
    }
    (playtest / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (playtest / "resume.json").write_text(
        json.dumps({key: state[key] for key in _RESUME_IDENTITY_KEYS}),
        encoding="utf-8",
    )


def make_tool_ctx(root: Path):
    return SimpleNamespace(
        config=Config.from_env(),
        repo_root=root,
        state=SimpleNamespace(workspace=SimpleNamespace(root=root)),
    )


def prepare_begin_ready(
    root: Path,
    *,
    pipeline_run_id: str = "pipeline-test",
    state_transaction_id: str = "transaction-test",
):
    (root / "src").mkdir(exist_ok=True)
    (root / "src/main.js").write_text(
        "export const build = 1;\n",
        encoding="utf-8",
    )
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/asset-manifest.json").write_text(
        json.dumps({"assets": []}),
        encoding="utf-8",
    )
    digest = compute_build_digest(root, root / "gca.config.yaml")
    write_pipeline_identity(
        root,
        digest,
        pipeline_run_id=pipeline_run_id,
        state_transaction_id=state_transaction_id,
    )
    ctx = make_tool_ctx(root)
    verify_tools._write_asset_integration_report({}, ctx)
    return ctx


def prepare_epoch(
    root: Path,
    *,
    mode: str = "iterate",
    url: str = "http://localhost:8080/",
    pipeline_run_id: str = "pipeline-test",
    state_transaction_id: str = "transaction-test",
):
    ctx = prepare_begin_ready(
        root,
        pipeline_run_id=pipeline_run_id,
        state_transaction_id=state_transaction_id,
    )
    begun = verify_tools._begin_verify_run({"mode": mode, "url": url}, ctx)
    current = json.loads(
        (root / ".playtest" / layout.VERIFY_RUN).read_text(encoding="utf-8")
    )
    return ctx, begun, current


def runtime_evidence(run):
    return {
        "schema_version": 1,
        "source": "runtime-identity.mjs",
        "verified": True,
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
