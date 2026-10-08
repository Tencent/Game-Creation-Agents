from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gca_mcp import workspace as workspace_module
from gca_mcp.config import Config
from gca_mcp.evidence import (
    acquire_secure_file_lock,
    prune_stale_locks,
    workspace_lock_path,
    workspace_lock_root,
)
from gca_mcp.errors import ToolError
from gca_mcp.server import ServerState, ToolCtx
from gca_mcp.tools import concept_tools
from gca_mcp.tools.lifecycle import _close_workspace, _init_workspace
from gca_mcp.workspace import open_workspace, resolve_workspace_root


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "playground").mkdir(parents=True)
    return repo


def _try_nonblocking_lock(path, outcomes) -> None:
    try:
        lease = acquire_secure_file_lock(
            path,
            exclusive=True,
            nonblocking=True,
        )
    except BaseException as exc:
        outcomes.put((type(exc).__name__, str(exc)))
        return
    lease.close()
    outcomes.put(("entered", ""))


def test_workspace_paths_are_repo_relative_and_single_component(tmp_path) -> None:
    repo = _repo(tmp_path)
    expected = repo / "playground" / "game"
    assert (
        resolve_workspace_root("playground/game", repo_root=repo)
        == expected
    )
    with pytest.raises(ToolError, match="directly below"):
        resolve_workspace_root(tmp_path / "outside", repo_root=repo)
    with pytest.raises(ToolError, match="exactly playground"):
        resolve_workspace_root("playground/nested/game", repo_root=repo)
    for unsafe in ("BadGame", "game name", "game'quote", "game\ninjected"):
        with pytest.raises(ToolError, match="slug"):
            resolve_workspace_root(repo / "playground" / unsafe, repo_root=repo)

    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "playground" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="directly below"):
        resolve_workspace_root("playground/link", repo_root=repo)


def test_create_and_resume_intent_is_fail_closed(tmp_path) -> None:
    repo = _repo(tmp_path)
    created = open_workspace(
        "playground/new-game",
        repo_root=repo,
        mode="create",
    )
    assert created.root == repo / "playground" / "new-game"
    assert created.mode == "create"
    assert created.lease_held is True
    created.close()
    (created.root / "index.html").write_text("<html></html>")
    with pytest.raises(ToolError, match="Create mode refuses"):
        open_workspace(created.root, repo_root=repo, mode="create")
    with pytest.raises(ToolError, match="requires"):
        open_workspace(created.root, repo_root=repo, mode="resume")
    (created.root / ".playtest").mkdir(exist_ok=True)
    (created.root / ".playtest" / "state.json").write_text("{}")
    resumed = open_workspace(created.root, repo_root=repo, mode="resume")
    assert resumed.root == created.root
    assert resumed.mode == "resume"
    resumed.close()


def test_workspace_lease_rejects_symlinked_coordination_file(tmp_path) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    outside = tmp_path / "outside-lock"
    outside.write_text("", encoding="utf-8")
    lock_path = workspace_lock_path(root, "workspace")
    lock_path.parent.mkdir(parents=True)
    lock_path.symlink_to(outside)

    with pytest.raises(ToolError) as exc_info:
        open_workspace(root, repo_root=repo, mode="create")
    assert exc_info.value.code == "LOCK_ERROR"
    assert outside.read_text(encoding="utf-8") == ""


def test_workspace_locks_live_in_one_directory_and_leave_repo_root_clean(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    lock_root = workspace_lock_root(root)

    assert lock_root.parent.parent == repo / ".gca"
    assert workspace_lock_path(root, "state") == lock_root / "state.lock"

    workspace = open_workspace(root, repo_root=repo, mode="create")
    try:
        assert sorted(entry.name for entry in lock_root.iterdir()) == [
            "workspace.lock"
        ]
        assert not list(repo.glob(".gca-lock.*"))
        assert sorted(entry.name for entry in repo.iterdir()) == [
            ".gca",
            "playground",
        ]
    finally:
        workspace.close()


def test_lock_directory_refuses_a_symlinked_component(tmp_path) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    (repo / ".gca").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolError) as exc_info:
        acquire_secure_file_lock(workspace_lock_path(root, "state"), exclusive=True)

    assert exc_info.value.code == "LOCK_ERROR"
    assert not list(outside.rglob("*"))


def test_prune_stale_locks_reclaims_only_unheld_locks(tmp_path) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    lock_root = workspace_lock_root(root)
    held = acquire_secure_file_lock(
        workspace_lock_path(root, "held"),
        exclusive=True,
    )
    acquire_secure_file_lock(
        workspace_lock_path(root, "stale"),
        exclusive=True,
    ).close()
    legacy = repo / ".gca-lock.0123456789abcdef01234567.workspace"
    legacy.write_bytes(b"")

    assert prune_stale_locks(repo, max_age_seconds=0.0) == 2
    assert sorted(entry.name for entry in lock_root.iterdir()) == ["held.lock"]
    assert not legacy.exists()

    held.close()
    assert prune_stale_locks(repo, max_age_seconds=0.0) == 1
    assert not lock_root.exists()


def test_prune_stale_locks_keeps_locks_younger_than_the_cutoff(tmp_path) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    lock_path = workspace_lock_path(root, "fresh")
    acquire_secure_file_lock(lock_path, exclusive=True).close()

    assert prune_stale_locks(repo, max_age_seconds=3600.0) == 0
    assert lock_path.exists()


def test_prune_stale_locks_leaves_a_symlinked_lock_for_fail_closed_reporting(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"")
    lock_path = workspace_lock_path(root, "planted")
    lock_path.parent.mkdir(parents=True)
    lock_path.symlink_to(outside)

    assert prune_stale_locks(repo, max_age_seconds=0.0) == 0
    assert lock_path.is_symlink()

    with pytest.raises(ToolError) as exc_info:
        acquire_secure_file_lock(lock_path, exclusive=True)
    assert exc_info.value.code == "LOCK_ERROR"


@pytest.mark.parametrize("mode", ["create", "resume"])
def test_workspace_rejects_symlinked_playtest_directory(tmp_path, mode) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    root.mkdir()
    outside = tmp_path / "outside-playtest"
    outside.mkdir()
    if mode == "resume":
        (outside / "state.json").write_text("{}", encoding="utf-8")
    (root / ".playtest").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolError) as exc_info:
        open_workspace(root, repo_root=repo, mode=mode)

    assert exc_info.value.code == "WORKSPACE_INIT_ERROR"
    assert "must be a real directory" in exc_info.value.message


@pytest.mark.parametrize("contender_kind", ["process", "thread"])
def test_held_lock_replacement_blocks_a_second_contender(
    tmp_path,
    contender_kind,
) -> None:
    repo = _repo(tmp_path)
    root = repo / "playground/game"
    root.mkdir()
    lock_path = workspace_lock_path(root, f"{contender_kind}-replacement")
    lease = acquire_secure_file_lock(lock_path, exclusive=True)
    lock_path.rename(lock_path.with_suffix(".original"))
    lock_path.write_text("", encoding="utf-8")

    if contender_kind == "process":
        process_ctx = mp.get_context("spawn")
        outcomes = process_ctx.Queue()
        contender = process_ctx.Process(
            target=_try_nonblocking_lock,
            args=(lock_path, outcomes),
        )
    else:
        outcomes = queue.Queue()
        contender = threading.Thread(
            target=_try_nonblocking_lock,
            args=(lock_path, outcomes),
        )
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive()
    if contender_kind == "process":
        assert contender.exitcode == 0
    assert outcomes.get(timeout=2)[0] == "BlockingIOError"
    if contender_kind == "thread":
        assert outcomes.empty()
    with pytest.raises(ToolError) as exc_info:
        lease.close()
    assert exc_info.value.code == "LOCK_REPLACED"


def test_workspace_identity_is_pinned_after_open(tmp_path) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    original = repo / "playground/game-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    opened.root.rename(original)
    opened.root.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ToolError) as exc_info:
            opened.assert_identity()
        assert exc_info.value.code == "WORKSPACE_REPLACED"
        ctx = SimpleNamespace(state=SimpleNamespace(workspace=opened))
        with pytest.raises(ToolError) as concept_error:
            concept_tools._resolve_under_workspace(ctx, "design/escape.png")
        assert concept_error.value.code == "WORKSPACE_REPLACED"
    finally:
        opened.root.unlink()
        original.rename(opened.root)
        opened.close()


def test_concept_write_uses_pinned_root_during_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    source = opened.root / "design/input.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"input")
    original = repo / "playground/game-original"
    outside = tmp_path / "outside"
    outside.mkdir()

    def replace_then_write(input_path, output_path, **_kwargs):
        assert input_path.is_file()
        opened.root.rename(original)
        opened.root.symlink_to(outside, target_is_directory=True)
        output_path.write_text("pinned", encoding="utf-8")
        return {"path": str(output_path)}

    monkeypatch.setattr(
        concept_tools.concept_ground,
        "fit_to_resolution",
        replace_then_write,
    )
    state = ServerState(
        config=Config.from_env(),
        repo_root=repo,
        workspace=opened,
    )
    tool = next(
        item for item in concept_tools.TOOLS if item.name == "fit_image_to_resolution"
    )
    try:
        with pytest.raises(ToolError) as exc_info:
            tool.handler(
                {
                    "input": "design/input.png",
                    "output": "design/output.png",
                    "width": 10,
                    "height": 10,
                },
                ToolCtx(state=state),
            )
        assert exc_info.value.code == "WORKSPACE_REPLACED"
        assert not (outside / "design/output.png").exists()
        assert not (original / "design/output.png").exists()
    finally:
        if opened.root.is_symlink():
            opened.root.unlink()
        if original.exists():
            original.rename(opened.root)
        opened.close()


def test_concept_output_commits_from_staging_to_pinned_workspace(tmp_path) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    source = opened.root / "design/input.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (20, 10), (10, 20, 30)).save(source)
    state = ServerState(
        config=Config.from_env(),
        repo_root=repo,
        workspace=opened,
    )
    tool = next(
        item for item in concept_tools.TOOLS if item.name == "fit_image_to_resolution"
    )
    try:
        result = tool.handler(
            {
                "input": "design/input.png",
                "output": "design/output.png",
                "width": 40,
                "height": 20,
            },
            ToolCtx(state=state),
        )
        output = opened.root / "design/output.png"
        assert output.is_file()
        assert str(output) in json.dumps(result)
        with Image.open(output) as image:
            assert image.size == (40, 20)
    finally:
        opened.close()


def test_commit_tree_rolls_back_original_when_publication_swap_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    target = opened.root / "design/crops/gameplay"
    (target / "nested").mkdir(parents=True)
    (target / "crops.json").write_bytes(b'{"version":"original"}')
    (target / "player.png").write_bytes(b"original-player")
    (target / "nested/kept.png").write_bytes(b"original-nested")
    staged = tmp_path / "staged-crops"
    staged.mkdir()
    (staged / "crops.json").write_bytes(b'{"version":"replacement"}')
    (staged / "replacement.png").write_bytes(b"replacement")

    def snapshot() -> dict[str, bytes | None]:
        result: dict[str, bytes | None] = {}
        for path in sorted(target.rglob("*")):
            relative = path.relative_to(target).as_posix()
            result[relative] = None if path.is_dir() else path.read_bytes()
        return result

    original = snapshot()
    real_rename = workspace_module.os.rename
    injected = False

    def fail_replacement_swap(source, destination, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and source.startswith(".gameplay.")
            and source.endswith(".tmp")
            and destination == "gameplay"
        ):
            injected = True
            raise OSError("injected replacement swap failure")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "rename", fail_replacement_swap)
    try:
        with pytest.raises(OSError, match="injected replacement swap failure"):
            opened.commit_tree_to_pinned(
                staged,
                Path("design/crops/gameplay"),
            )

        assert injected is True
        assert snapshot() == original
        assert not (target / "replacement.png").exists()
        assert not [
            path
            for path in target.parent.iterdir()
            if path.name.startswith(".gameplay.")
        ]
    finally:
        opened.close()


@pytest.mark.parametrize("failure_point", ["backup", "replacement"])
def test_commit_tree_restores_original_when_parent_fsync_fails_after_rename(
    tmp_path,
    monkeypatch,
    failure_point,
) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    target = opened.root / "design/crops/gameplay"
    target.mkdir(parents=True)
    (target / "crops.json").write_bytes(b'{"version":"original"}')
    (target / "player.png").write_bytes(b"original-player")
    staged = tmp_path / "staged-fsync-crops"
    staged.mkdir()
    (staged / "crops.json").write_bytes(b'{"version":"replacement"}')
    (staged / "replacement.png").write_bytes(b"replacement")
    original = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }

    real_rename = workspace_module.os.rename
    real_fsync = workspace_module.os.fsync
    renamed: str | None = None
    injected = False

    def track_rename(source, destination, *args, **kwargs):
        nonlocal renamed
        result = real_rename(source, destination, *args, **kwargs)
        if source == "gameplay" and str(destination).endswith(".bak"):
            renamed = "backup"
        elif (
            str(source).startswith(".gameplay.")
            and str(source).endswith(".tmp")
            and destination == "gameplay"
        ):
            renamed = "replacement"
        else:
            renamed = None
        return result

    def fail_target_fsync(descriptor):
        nonlocal injected, renamed
        if not injected and renamed == failure_point:
            injected = True
            renamed = None
            raise OSError(f"injected {failure_point} parent fsync failure")
        renamed = None
        return real_fsync(descriptor)

    monkeypatch.setattr(workspace_module.os, "rename", track_rename)
    monkeypatch.setattr(workspace_module.os, "fsync", fail_target_fsync)
    try:
        with pytest.raises(OSError, match=f"injected {failure_point}"):
            opened.commit_tree_to_pinned(
                staged,
                Path("design/crops/gameplay"),
            )

        assert injected is True
        restored = {
            path.relative_to(target).as_posix(): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        }
        assert restored == original
        assert not [
            path
            for path in target.parent.iterdir()
            if path.name.startswith(".gameplay.")
        ]
    finally:
        opened.close()


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo"])
def test_commit_tree_rejects_symlinks_and_special_files(
    tmp_path,
    entry_kind,
) -> None:
    if entry_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is unavailable")
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    target = opened.root / "design/crops/gameplay"
    target.mkdir(parents=True)
    (target / "original.png").write_bytes(b"original")
    staged = tmp_path / f"staged-{entry_kind}"
    staged.mkdir()
    unsafe = staged / "unsafe"
    if entry_kind == "symlink":
        unsafe.symlink_to(tmp_path / "outside")
    else:
        os.mkfifo(unsafe)

    try:
        with pytest.raises(ToolError, match="symlink|special file"):
            opened.commit_tree_to_pinned(
                staged,
                Path("design/crops/gameplay"),
            )
        assert (target / "original.png").read_bytes() == b"original"
        assert [path.name for path in target.iterdir()] == ["original.png"]
    finally:
        opened.close()


def test_commit_tree_never_publishes_through_destination_symlink(tmp_path) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    destination_parent = opened.root / "design/crops"
    destination_parent.mkdir(parents=True)
    outside = tmp_path / "outside-crops"
    outside.mkdir()
    (outside / "sentinel.png").write_bytes(b"outside")
    (destination_parent / "gameplay").symlink_to(
        outside,
        target_is_directory=True,
    )
    staged = tmp_path / "staged-safe"
    staged.mkdir()
    (staged / "player.png").write_bytes(b"replacement")

    try:
        with pytest.raises(ToolError, match="not a real directory"):
            opened.commit_tree_to_pinned(
                staged,
                Path("design/crops/gameplay"),
            )
        assert (outside / "sentinel.png").read_bytes() == b"outside"
        assert not (outside / "player.png").exists()
    finally:
        opened.close()


def test_detect_layout_default_output_commits_from_staging(
    tmp_path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    opened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    source = opened.root / "design/gameplay.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"concept")

    def fake_detect(_config, *, image_path, out_path, **_kwargs):
        assert image_path.is_file()
        assert out_path is not None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text('{"elements":[]}', encoding="utf-8")
        return {"out_path": str(out_path), "elements": []}

    monkeypatch.setattr(
        concept_tools.detect_layout,
        "detect_layout",
        fake_detect,
    )
    state = ServerState(
        config=Config.from_env(),
        repo_root=repo,
        workspace=opened,
    )
    tool = next(
        item for item in concept_tools.TOOLS if item.name == "detect_layout"
    )
    try:
        result = tool.handler(
            {"input": "design/gameplay.png"},
            ToolCtx(state=state),
        )
        output = opened.root / "design/gameplay.detect.json"
        assert output.is_file()
        assert result["out_path"] == str(output)
    finally:
        opened.close()


def test_workspace_lease_excludes_a_second_process_until_exit(tmp_path) -> None:
    repo = _repo(tmp_path)
    holder_code = """
import sys
from pathlib import Path
from gca_mcp.workspace import open_workspace

workspace = open_workspace(
    "playground/game",
    repo_root=Path(sys.argv[1]),
    mode="create",
)
print("ready", flush=True)
sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        ready = holder.stdout.readline().strip()
        if ready != "ready":
            holder.terminate()
            _, stderr = holder.communicate(timeout=5)
            pytest.fail(f"lease holder failed to start: {stderr}")

        with pytest.raises(ToolError) as exc_info:
            open_workspace(
                "playground/game",
                repo_root=repo,
                mode="create",
            )
        assert exc_info.value.code == "WORKSPACE_ALREADY_OPEN"
        assert "another process" in exc_info.value.message
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("\n")
            holder.stdin.flush()
        holder.wait(timeout=5)

    # The child deliberately did not call close(); process exit releases flock.
    reopened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    reopened.close()


def test_init_reopen_requires_same_path_and_mode(tmp_path) -> None:
    repo = _repo(tmp_path)
    state = ServerState(config=Config.from_env(), repo_root=repo)
    ctx = ToolCtx(state=state)
    opened = _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )
    assert opened["reopened"] is False
    assert opened["mode"] == "create"
    assert opened["workspace"]["mode"] == "create"
    assert _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )["reopened"] is True
    with pytest.raises(ToolError) as exc_info:
        _init_workspace(
            {"path": "playground/game", "mode": "resume"},
            ctx,
        )
    assert exc_info.value.code == "WORKSPACE_ALREADY_OPEN"
    assert "mode='create'" in exc_info.value.message

    _close_workspace({}, ctx)


def test_close_waits_for_active_calls_and_releases_lease(tmp_path) -> None:
    repo = _repo(tmp_path)
    state = ServerState(config=Config.from_env(), repo_root=repo)
    ctx = ToolCtx(state=state)
    _init_workspace(
        {"path": "playground/game", "mode": "create"},
        ctx,
    )

    assert state.begin_workspace_call() is not None
    closed = threading.Event()

    def close() -> None:
        _close_workspace({}, ctx)
        closed.set()

    thread = threading.Thread(target=close)
    thread.start()
    time.sleep(0.05)
    assert not closed.is_set()
    state.end_workspace_call()
    thread.join(timeout=2)
    assert closed.is_set()
    assert state.workspace is None

    reopened = open_workspace(
        "playground/game",
        repo_root=repo,
        mode="create",
    )
    reopened.close()
