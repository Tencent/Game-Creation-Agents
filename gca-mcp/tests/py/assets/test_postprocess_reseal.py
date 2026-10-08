"""Regression tests for carrying a static-asset seal across a post-process.

``make-seamless`` rewrites the bytes of an already-sealed runtime asset. Before
these, that broke ``artifact_sha256`` with no way forward: the only other writer
of a valid seal is ``produce_static_asset``, which generates from a prompt rather
than sealing bytes, and the regenerated image needed the same transform again. A
parallax band could not be both seamless and sealed.
"""

from __future__ import annotations

import hashlib
import json
import os
import weakref
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from gca_mcp.config import Config
from gca_mcp.tools import postprocess_tools
from gca_mcp.tools.postprocess_tools import _reseal_postprocessed_asset
from gca_mcp.workspace import WorkspaceMeta

REL = "assets/maps/map-clouds.png"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed_asset(tmp_path: Path, *, sealed_hash: str | None = None) -> tuple[Path, str]:
    """Write a PNG plus the sidecar a real producer run would have left."""
    png = tmp_path / "map-clouds.png"
    Image.new("RGBA", (64, 32), (1, 2, 3, 255)).save(png)
    original = _sha(png)
    png.with_suffix(".asset.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "producer": "produce_static_asset",
                "artifact": REL,
                "artifact_sha256": sealed_hash or original,
                "width": 64,
                "height": 32,
                "provenance": {"source": "generated", "request_id": "req_abc"},
                "source_contract": {
                    "version": 1,
                    "sourceStrategy": "generated-image",
                    "assetType": "parallax",
                    "generationMode": "text-to-image",
                    "path": REL,
                },
            }
        ),
        encoding="utf-8",
    )
    return png, original


def _transform(png: Path, *, size: tuple[int, int] = (64, 30)) -> None:
    """Stand in for the runner rewriting the artifact."""
    Image.new("RGBA", size, (9, 9, 9, 255)).save(png)


def test_seal_follows_the_bytes_through_the_transform(tmp_path):
    png, original = _sealed_asset(tmp_path)
    _transform(png)

    assert _reseal_postprocessed_asset(
        png, command="make-seamless", pre_sha256=original
    )

    sealed = json.loads(png.with_suffix(".asset.json").read_text(encoding="utf-8"))
    assert sealed["artifact_sha256"] == _sha(png)
    assert (sealed["width"], sealed["height"]) == (64, 30)


def test_provenance_survives_and_the_transform_joins_lineage(tmp_path):
    # The point of refreshing rather than reissuing: the asset still traces to
    # the generation that made it, with what happened afterwards recorded.
    png, original = _sealed_asset(tmp_path)
    _transform(png)

    _reseal_postprocessed_asset(png, command="make-seamless", pre_sha256=original)

    sealed = json.loads(png.with_suffix(".asset.json").read_text(encoding="utf-8"))
    assert sealed["provenance"] == {"source": "generated", "request_id": "req_abc"}
    assert sealed["producer"] == "produce_static_asset"
    assert sealed["lineage"] == [
        {"artifact": REL, "sha256": original, "transform": "make-seamless"}
    ]


def test_sidecar_is_never_older_than_the_artifact(tmp_path):
    # The finalizer reads an older sidecar as stale however correct its contents.
    png, original = _sealed_asset(tmp_path)
    _transform(png)

    _reseal_postprocessed_asset(png, command="make-seamless", pre_sha256=original)

    sidecar = png.with_suffix(".asset.json")
    assert sidecar.stat().st_mtime_ns >= png.stat().st_mtime_ns


def test_a_seal_that_did_not_describe_these_bytes_is_left_alone(tmp_path):
    # The safety property: refreshing only ever extends a seal that was provably
    # intact, so a hand-placed PNG cannot inherit generated provenance.
    png, original = _sealed_asset(tmp_path, sealed_hash="f" * 64)
    _transform(png)

    assert (
        _reseal_postprocessed_asset(png, command="make-seamless", pre_sha256=original)
        is None
    )
    sealed = json.loads(png.with_suffix(".asset.json").read_text(encoding="utf-8"))
    assert sealed["artifact_sha256"] == "f" * 64


def test_an_unsealed_output_is_left_alone(tmp_path):
    png = tmp_path / "loose.png"
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(png)

    assert (
        _reseal_postprocessed_asset(png, command="make-seamless", pre_sha256=_sha(png))
        is None
    )
    assert not png.with_suffix(".asset.json").exists()


def test_an_unchanged_artifact_is_not_rewritten(tmp_path):
    png, original = _sealed_asset(tmp_path)
    before = png.with_suffix(".asset.json").stat().st_mtime_ns

    assert (
        _reseal_postprocessed_asset(png, command="make-seamless", pre_sha256=original)
        is None
    )
    assert png.with_suffix(".asset.json").stat().st_mtime_ns == before


class _TestLease:
    closed = False

    @staticmethod
    def assert_current() -> None:
        return None


def _ctx(tmp_path: Path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(tmp_path.parent, flags)
    root_fd = os.open(tmp_path.name, flags, dir_fd=parent_fd)
    workspace = WorkspaceMeta(
        root=tmp_path,
        name=tmp_path.name,
        mode="test",
        _lease_file=_TestLease(),
        _root_fd=root_fd,
        _parent_fd=parent_fd,
    )

    def _close():
        for descriptor in (root_fd, parent_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass

    weakref.finalize(workspace, _close)
    return SimpleNamespace(
        config=Config.from_env(),
        state=SimpleNamespace(workspace=workspace),
        repo_root=tmp_path,
    )


def test_postprocess_reseals_through_the_tool_entrypoint(tmp_path, monkeypatch):
    # The helper being correct is not enough: the handler has to capture the
    # pre-transform hash *before* the runner rewrites the file, or an intact seal
    # becomes indistinguishable from one that was already stale.
    maps = tmp_path / "assets" / "maps"
    maps.mkdir(parents=True)
    png, original = _sealed_asset(maps)

    def _fake_runner(_script, _cli, check=False):
        _transform(png)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(postprocess_tools, "run_python_script", _fake_runner)

    result = postprocess_tools._postprocess(
        {"command": "make-seamless", "input": REL, "output": REL},
        _ctx(tmp_path),
    )

    assert result["resealed"]["previous_sha256"] == original
    sealed = json.loads(png.with_suffix(".asset.json").read_text(encoding="utf-8"))
    assert sealed["artifact_sha256"] == _sha(png)
    assert sealed["lineage"][-1]["transform"] == "make-seamless"
