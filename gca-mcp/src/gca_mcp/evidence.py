"""Run identity, deterministic build digests, and atomic JSON helpers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat as stat_module
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .errors import ToolError


# Every coordination lock lives under one generated repo directory so the repo
# root stays readable and a whole run can be reclaimed by removing one tree.
LOCK_ROOT_NAME = ".gca"
LOCK_DIR_NAME = "locks"
LOCK_SUFFIX = ".lock"
# Pre-`.gca/locks` layout: flat `.gca-lock.<key>.<purpose>` files at the repo
# root. Swept by `prune_stale_locks` so existing checkouts self-clean.
LEGACY_LOCK_PREFIX = ".gca-lock."
STALE_LOCK_MAX_AGE_SECONDS = 3600.0

_STATE_LOCK_GUARD = threading.Lock()
_STATE_LOCKS: dict[Path, threading.RLock] = {}
_REPO_GUARD_CONDITION = threading.Condition()
_REPO_GUARDS: dict[Path, dict[str, Any]] = {}
_REPO_GUARD_ACQUIRING: set[Path] = set()
_REPO_GUARD_PID = os.getpid()
_NAMED_LOCKS_GUARD = threading.Lock()
_NAMED_LOCKS: dict[Path, "_NamedThreadLock"] = {}
_NAMED_LOCK_PID = os.getpid()


class _NamedThreadToken:
    def __init__(
        self,
        lock: "_NamedThreadLock",
        *,
        exclusive: bool,
        owner: int,
    ):
        self._lock = lock
        self._exclusive = exclusive
        self._owner = owner
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._lock.release(exclusive=self._exclusive, owner=self._owner)


class _NamedThreadLock:
    """Process-local reader/writer lock keyed by immutable lexical lock name."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.writer: int | None = None
        self.readers: dict[int, int] = {}

    def acquire(
        self,
        *,
        exclusive: bool,
        nonblocking: bool,
    ) -> _NamedThreadToken:
        owner = threading.get_ident()
        with self.condition:
            if exclusive and self.writer == owner:
                raise ToolError(
                    "LOCK_REENTRY",
                    "Exclusive coordination locks may not re-enter on one thread.",
                )
            while True:
                writer_free = self.writer is None
                readers_free = not self.readers
                if exclusive and writer_free and readers_free:
                    self.writer = owner
                    return _NamedThreadToken(
                        self,
                        exclusive=True,
                        owner=owner,
                    )
                if not exclusive and (writer_free or self.writer == owner):
                    self.readers[owner] = self.readers.get(owner, 0) + 1
                    return _NamedThreadToken(
                        self,
                        exclusive=False,
                        owner=owner,
                    )
                if nonblocking:
                    raise BlockingIOError("Named coordination lock is busy.")
                self.condition.wait()

    def release(self, *, exclusive: bool, owner: int) -> None:
        with self.condition:
            if exclusive:
                if self.writer != owner:
                    raise RuntimeError("Named writer lock released by non-owner.")
                self.writer = None
            else:
                depth = self.readers.get(owner, 0)
                if depth < 1:
                    raise RuntimeError("Named reader lock released by non-owner.")
                if depth == 1:
                    del self.readers[owner]
                else:
                    self.readers[owner] = depth - 1
            self.condition.notify_all()


def _acquire_named_thread_lock(
    path: Path,
    *,
    exclusive: bool,
    nonblocking: bool,
) -> _NamedThreadToken:
    global _NAMED_LOCK_PID
    key = Path(os.path.abspath(path))
    with _NAMED_LOCKS_GUARD:
        if os.getpid() != _NAMED_LOCK_PID:
            _NAMED_LOCKS.clear()
            _NAMED_LOCK_PID = os.getpid()
        lock = _NAMED_LOCKS.setdefault(key, _NamedThreadLock())
    return lock.acquire(exclusive=exclusive, nonblocking=nonblocking)


class _RepoGuardToken:
    """One process-local reference to a repo-directory flock."""

    def __init__(self, path: Path):
        self.path = path
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with _REPO_GUARD_CONDITION:
            state = _REPO_GUARDS.get(self.path)
            if state is None or state["refs"] < 1:
                raise RuntimeError(f"Repo guard reference is missing: {self.path}")
            state["refs"] -= 1
            if state["refs"] == 0:
                fd = state["fd"]
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                    del _REPO_GUARDS[self.path]
                    _REPO_GUARD_CONDITION.notify_all()


def _reset_repo_guards_after_fork() -> None:
    global _REPO_GUARD_PID
    pid = os.getpid()
    if pid == _REPO_GUARD_PID:
        return
    for state in _REPO_GUARDS.values():
        try:
            os.close(state["fd"])
        except OSError:
            pass
    _REPO_GUARDS.clear()
    _REPO_GUARD_ACQUIRING.clear()
    _REPO_GUARD_PID = pid


def _acquire_repo_guard(
    path: Path,
    *,
    exclusive: bool,
    nonblocking: bool,
) -> _RepoGuardToken:
    """Flock the stable repo directory around every named coordination lock."""
    key = Path(os.path.abspath(path))
    while True:
        with _REPO_GUARD_CONDITION:
            _reset_repo_guards_after_fork()
            state = _REPO_GUARDS.get(key)
            if state is not None:
                if state["exclusive"] or not exclusive:
                    state["refs"] += 1
                    return _RepoGuardToken(key)
                if nonblocking:
                    raise BlockingIOError(f"Repo guard is busy: {key}")
                _REPO_GUARD_CONDITION.wait()
                continue
            if key in _REPO_GUARD_ACQUIRING:
                if nonblocking:
                    raise BlockingIOError(f"Repo guard is being acquired: {key}")
                _REPO_GUARD_CONDITION.wait()
                continue
            _REPO_GUARD_ACQUIRING.add(key)
            break

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(key, flags)
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            mode |= fcntl.LOCK_NB
        fcntl.flock(fd, mode)
        opened = os.fstat(fd)
        current = os.stat(key, follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(opened.st_mode)
            or not stat_module.S_ISDIR(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise ToolError(
                "LOCK_REPLACED",
                f"Repo coordination directory was replaced: {key}.",
            )
    except BaseException:
        if fd is not None:
            os.close(fd)
        with _REPO_GUARD_CONDITION:
            _REPO_GUARD_ACQUIRING.discard(key)
            _REPO_GUARD_CONDITION.notify_all()
        raise

    with _REPO_GUARD_CONDITION:
        _REPO_GUARD_ACQUIRING.discard(key)
        _REPO_GUARDS[key] = {"fd": fd, "exclusive": exclusive, "refs": 1}
        _REPO_GUARD_CONDITION.notify_all()
    return _RepoGuardToken(key)


class SecureFileLease:
    """One flock held on a no-follow, inode-verified coordination file."""

    def __init__(
        self,
        fd: int,
        parent_fd: int,
        path: Path,
        repo_guard: _RepoGuardToken,
        thread_lock: _NamedThreadToken,
    ):
        self._fd = fd
        self._parent_fd = parent_fd
        self._repo_guard = repo_guard
        self._thread_lock = thread_lock
        self.path = path
        self.closed = False

    def fileno(self) -> int:
        if self.closed:
            raise ValueError("lease is closed")
        return self._fd

    def assert_current(self) -> None:
        if self.closed:
            raise ToolError("LOCK_ERROR", f"Coordination lock is closed: {self.path}.")
        try:
            opened = os.fstat(self._fd)
            current = os.stat(
                self.path.name,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            parent_opened = os.fstat(self._parent_fd)
            parent_current = os.stat(self.path.parent, follow_symlinks=False)
            path_current = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ToolError(
                "LOCK_REPLACED",
                f"Coordination lock disappeared while held: {self.path}.",
            ) from exc
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_dev != path_current.st_dev
            or opened.st_ino != path_current.st_ino
            or parent_opened.st_dev != parent_current.st_dev
            or parent_opened.st_ino != parent_current.st_ino
        ):
            raise ToolError(
                "LOCK_REPLACED",
                f"Coordination lock was replaced while held: {self.path}.",
            )

    def close(self) -> None:
        if self.closed:
            return
        error: BaseException | None = None
        try:
            self.assert_current()
        except BaseException as exc:
            error = exc
        self.closed = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            os.close(self._parent_fd)
            try:
                self._repo_guard.close()
            finally:
                self._thread_lock.close()
        if error is not None:
            raise error


def lock_repo_root(workspace_root: Path) -> Path:
    """Return the stable directory that anchors one workspace's coordination locks."""
    root = Path(os.path.abspath(workspace_root))
    return (
        root.parent.parent
        if root.parent.name == "playground"
        else root
    )


def workspace_lock_root(workspace_root: Path) -> Path:
    """Return the single directory holding every lock for one workspace."""
    root = Path(os.path.abspath(workspace_root))
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    return lock_repo_root(root) / LOCK_ROOT_NAME / LOCK_DIR_NAME / key


def workspace_lock_path(workspace_root: Path, purpose: str) -> Path:
    """Return a stable repo-level lock path keyed by lexical workspace identity."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", purpose):
        raise ToolError("INVALID_CONFIG", f"Invalid lock purpose: {purpose!r}.")
    return workspace_lock_root(workspace_root) / f"{purpose}{LOCK_SUFFIX}"


def _ensure_lock_directory(directory: Path) -> None:
    """Create ``directory`` without traversing or creating a symlinked component.

    ``mkdir(parents=True)`` would happily follow a pre-planted symlink such as
    ``.gca`` and place locks outside the repo, so every component below the
    deepest existing ancestor is created and reopened through ``O_NOFOLLOW``.
    """
    path = Path(os.path.abspath(directory))
    chain = [path, *path.parents]
    anchor: int | None = None
    for index, candidate in enumerate(chain):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ToolError(
                "LOCK_ERROR",
                f"Could not inspect coordination directory {candidate}: {exc}",
            ) from exc
        if not stat_module.S_ISDIR(metadata.st_mode):
            raise ToolError(
                "LOCK_ERROR",
                f"Coordination path is not a real directory: {candidate}.",
            )
        anchor = index
        break
    if anchor is None:
        raise ToolError(
            "LOCK_ERROR",
            f"No existing ancestor for coordination directory {path}.",
        )
    if anchor == 0:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(chain[anchor], flags)
    except OSError as exc:
        raise ToolError(
            "LOCK_ERROR",
            f"Could not open coordination directory {chain[anchor]}: {exc}",
        ) from exc
    try:
        for index in range(anchor - 1, -1, -1):
            name = chain[index].name
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ToolError(
                    "LOCK_ERROR",
                    f"Could not create coordination directory {chain[index]}: {exc}",
                ) from exc
            try:
                child = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                raise ToolError(
                    "LOCK_ERROR",
                    "Coordination directory component is not a real directory: "
                    f"{chain[index]}: {exc}",
                ) from exc
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def acquire_secure_file_lock(
    lock_path: Path,
    *,
    exclusive: bool,
    nonblocking: bool = False,
) -> SecureFileLease:
    """Open and flock one regular file without following a replaced symlink."""
    path = Path(os.path.abspath(lock_path))
    _ensure_lock_directory(path.parent)
    thread_lock = _acquire_named_thread_lock(
        path,
        exclusive=exclusive,
        nonblocking=nonblocking,
    )
    try:
        repo_guard = _acquire_repo_guard(
            path.parent,
            exclusive=exclusive,
            nonblocking=nonblocking,
        )
    except BaseException:
        thread_lock.close()
        raise
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(path.parent, parent_flags)
    except OSError as exc:
        repo_guard.close()
        thread_lock.close()
        raise ToolError(
            "LOCK_ERROR",
            f"Could not securely open coordination directory {path.parent}: {exc}",
        ) from exc
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                fd = os.open(
                    path.name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        repo_guard.close()
        thread_lock.close()
        raise ToolError(
            "LOCK_ERROR",
            f"Could not securely open coordination lock {path}: {exc}",
        ) from exc
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            mode |= fcntl.LOCK_NB
        fcntl.flock(fd, mode)
        opened = os.fstat(fd)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_opened = os.fstat(parent_fd)
        parent_current = os.stat(path.parent, follow_symlinks=False)
        path_current = os.stat(path, follow_symlinks=False)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_dev != path_current.st_dev
            or opened.st_ino != path_current.st_ino
            or parent_opened.st_dev != parent_current.st_dev
            or parent_opened.st_ino != parent_current.st_ino
        ):
            raise ToolError(
                "LOCK_REPLACED",
                f"Coordination lock was replaced while opening: {path}.",
            )
        return SecureFileLease(fd, parent_fd, path, repo_guard, thread_lock)
    except BlockingIOError:
        os.close(parent_fd)
        os.close(fd)
        repo_guard.close()
        thread_lock.close()
        raise
    except ToolError:
        os.close(parent_fd)
        os.close(fd)
        repo_guard.close()
        thread_lock.close()
        raise
    except OSError as exc:
        os.close(parent_fd)
        os.close(fd)
        repo_guard.close()
        thread_lock.close()
        raise ToolError(
            "LOCK_ERROR",
            f"Could not acquire coordination lock {path}: {exc}",
        ) from exc
    except BaseException:
        os.close(parent_fd)
        os.close(fd)
        repo_guard.close()
        thread_lock.close()
        raise


@contextmanager
def secure_file_lock(
    lock_path: Path,
    *,
    exclusive: bool,
) -> Iterator[SecureFileLease]:
    lease = acquire_secure_file_lock(lock_path, exclusive=exclusive)
    try:
        yield lease
    finally:
        lease.close()


def _reclaim_one_lock(
    name: str,
    *,
    parent_fd: int,
    directory: Path,
    cutoff: float,
) -> bool:
    """Unlink one lock only while proven free of thread and process holders."""
    try:
        token = _acquire_named_thread_lock(
            directory / name,
            exclusive=True,
            nonblocking=True,
        )
    except (BlockingIOError, ToolError):
        return False
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        # A symlinked or otherwise non-regular entry is an attack signal that
        # acquisition reports as LOCK_ERROR; reclaiming it would delete the
        # evidence and let the next caller proceed as if nothing happened.
        metadata = os.lstat(name, dir_fd=parent_fd)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_mtime > cutoff:
            return False
        fd = os.open(name, flags, dir_fd=parent_fd)
        if os.fstat(fd).st_ino != metadata.st_ino:
            return False
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.unlink(name, dir_fd=parent_fd)
        return True
    except (BlockingIOError, OSError):
        return False
    finally:
        if fd is not None:
            os.close(fd)
        token.close()


def _prune_lock_directory(
    directory: Path,
    *,
    max_age_seconds: float,
    match: Callable[[str], bool],
) -> int:
    """Reclaim matching unheld locks in one directory; return how many went away."""
    try:
        guard = _acquire_repo_guard(directory, exclusive=True, nonblocking=True)
    except (BlockingIOError, ToolError, OSError):
        return 0
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    removed = 0
    parent_fd: int | None = None
    try:
        parent_fd = os.open(directory, flags)
        cutoff = time.time() - max_age_seconds
        for name in sorted(os.listdir(parent_fd)):
            if not match(name):
                continue
            if _reclaim_one_lock(
                name,
                parent_fd=parent_fd,
                directory=directory,
                cutoff=cutoff,
            ):
                removed += 1
    except OSError:
        pass
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        guard.close()
    return removed


def prune_stale_locks(
    repo_root: Path,
    *,
    max_age_seconds: float = STALE_LOCK_MAX_AGE_SECONDS,
) -> int:
    """Reclaim coordination locks no live holder owns; return the count removed.

    Every acquisition takes the directory guard before touching a lock file, so
    holding that guard exclusively freezes cross-process acquisition, and the
    per-file probe skips anything a same-process thread still owns. Whatever
    survives both checks cannot be observed by a holder and is safe to unlink.
    """
    root = Path(os.path.abspath(repo_root))
    locks_root = root / LOCK_ROOT_NAME / LOCK_DIR_NAME
    removed = 0
    try:
        with os.scandir(locks_root) as entries:
            key_dirs = sorted(
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        key_dirs = []
    for key_dir in key_dirs:
        removed += _prune_lock_directory(
            key_dir,
            max_age_seconds=max_age_seconds,
            match=lambda name: name.endswith(LOCK_SUFFIX),
        )
        try:
            key_dir.rmdir()
        except OSError:
            pass
    removed += _prune_lock_directory(
        root,
        max_age_seconds=max_age_seconds,
        match=lambda name: name.startswith(LEGACY_LOCK_PREFIX),
    )
    return removed


def atomic_write_json(path: Path, data: Any) -> None:
    """Replace one JSON file atomically; readers never observe partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def pipeline_state_lock(playtest_dir: Path) -> Iterator[None]:
    """Serialize state reads, recovery, and writes across threads/processes."""
    playtest_dir.mkdir(parents=True, exist_ok=True)
    key = playtest_dir.resolve()
    with _STATE_LOCK_GUARD:
        thread_lock = _STATE_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        lock_path = workspace_lock_path(playtest_dir.parent, "state")
        with secure_file_lock(lock_path, exclusive=True):
            yield


def _validate_state_transaction(
    pending: Any,
    *,
    playtest_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return two coherent projections or reject the pending journal."""
    if not isinstance(pending, dict):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction must contain a JSON object.",
        )
    expected_fields = {
        "schema_version",
        "state_transaction_id",
        "state",
        "resume",
    }
    if set(pending) != expected_fields:
        missing = sorted(expected_fields - set(pending))
        unknown = sorted(set(pending) - expected_fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction has an invalid envelope"
            + (f" ({'; '.join(details)})" if details else "")
            + ".",
        )
    if type(pending["schema_version"]) is not int or pending["schema_version"] != 1:
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction schema_version must equal 1.",
        )

    transaction_id = pending["state_transaction_id"]
    state = pending["state"]
    resume = pending["resume"]
    if not isinstance(transaction_id, str) or not transaction_id:
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction_id must be a non-empty string.",
        )
    if not isinstance(state, dict) or not isinstance(resume, dict):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction lacks state/resume object projections.",
        )
    if any(
        projection.get("state_transaction_id") != transaction_id
        for projection in (state, resume)
    ):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state/resume transaction IDs do not match the journal.",
        )

    pipeline_run_id = state.get("pipeline_run_id")
    build_digest = state.get("build_digest")
    if (
        not isinstance(pipeline_run_id, str)
        or not pipeline_run_id
        or resume.get("pipeline_run_id") != pipeline_run_id
    ):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state/resume pipeline identities are missing or inconsistent.",
        )
    if (
        not isinstance(build_digest, str)
        or len(build_digest) != len("sha256:") + 64
        or not build_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in build_digest[7:])
        or resume.get("build_digest") != build_digest
    ):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state/resume build digests are missing or inconsistent.",
        )

    expected_game_dir = playtest_dir.parent.resolve()
    game_dirs = (state.get("game_dir"), resume.get("game_dir"))
    if any(
        not isinstance(value, str)
        or Path(value).expanduser().resolve(strict=False) != expected_game_dir
        for value in game_dirs
    ):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state transaction belongs to a different workspace.",
        )
    if (
        not isinstance(state.get("phase"), str)
        or resume.get("phase") != state.get("phase")
        or not isinstance(state.get("phase_status"), str)
        or resume.get("status") != state.get("phase_status")
        or not isinstance(state.get("completed_phases"), list)
        or resume.get("completed_phases") != state.get("completed_phases")
    ):
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            "Pending state/resume phase projections are inconsistent.",
        )
    for field in (
        "genre",
        "interaction_profile",
        "quality_profile",
        "level_model",
    ):
        if resume.get(field) != state.get(field):
            raise ToolError(
                "INVALID_STATE_TRANSACTION",
                f"Pending state/resume {field} projections are inconsistent.",
            )
    return state, resume


def recover_state_transaction(playtest_dir: Path) -> bool:
    """Roll forward one valid pending journal while its state lock is held."""
    journal = playtest_dir / "state-transaction.pending.json"
    if not journal.exists():
        return False
    if not journal.is_file():
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            f"Pending state transaction is not a regular file: {journal}.",
        )
    try:
        pending = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            "INVALID_STATE_TRANSACTION",
            f"Pending state transaction is unreadable: {exc}",
        ) from exc
    state, resume = _validate_state_transaction(
        pending,
        playtest_dir=playtest_dir,
    )
    atomic_write_json(playtest_dir / "state.json", state)
    atomic_write_json(playtest_dir / "resume.json", resume)
    journal.unlink()
    return True


def manifest_entries(manifest: Any) -> list[Any]:
    """Normalize canonical and legacy manifest shapes to one flat entry list."""
    if isinstance(manifest, list):
        return manifest
    if not isinstance(manifest, dict):
        return []
    for field in ("assets", "entries", "items"):
        entries = manifest.get(field)
        if isinstance(entries, list):
            return entries

    reserved = {
        "schema_version",
        "version",
        "generated_at",
        "notes",
        "qualityProfile",
        "fileAssetsRequired",
        "runtimeSources",
    }
    singular_categories = {
        "sprites": "sprite",
        "characters": "character",
        "enemies": "enemy",
        "bosses": "boss",
        "npcs": "npc",
        "projectiles": "projectile",
        "props": "prop",
        "tiles": "tile",
        "tilesets": "tileset",
        "platforms": "platform",
        "obstacles": "obstacle",
        "backgrounds": "background",
    }
    normalized: list[Any] = []

    def singular(value: Any) -> Any:
        return singular_categories.get(str(value or "").lower(), value)

    def visit(node: Any, category: Any, key_hint: Any) -> None:
        category = singular(category)
        if node is None:
            return
        if isinstance(node, str):
            if key_hint:
                normalized.append(
                    {"key": key_hint, "category": category, "path": node}
                )
            return
        if isinstance(node, list):
            for index, value in enumerate(node):
                explicit_key = value.get("key") if isinstance(value, dict) else None
                visit(
                    value,
                    category,
                    explicit_key or f"{key_hint or category}-{index}",
                )
            return
        if not isinstance(node, dict):
            return
        key = node.get("key") or key_hint
        if any(node.get(field) for field in ("path", "actions", "textureKey")):
            normalized.append(
                {
                    **node,
                    "key": key,
                    "category": singular(node.get("category") or category),
                }
            )
            return
        items = node.get("items")
        if isinstance(items, dict):
            for item_key, value in items.items():
                visit(value, category, item_key)
            return
        for child_key, value in node.items():
            if child_key not in reserved:
                visit(value, category, child_key)

    for category, node in manifest.items():
        if category not in reserved:
            visit(node, category, None)
    return normalized


def _manifest_paths(game_dir: Path) -> set[Path]:
    manifest_path = game_dir / "assets" / "asset-manifest.json"
    paths = {manifest_path}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return paths
    for entry in manifest_entries(manifest):
        if not isinstance(entry, dict):
            continue
        specs = [entry]
        actions = entry.get("actions")
        if isinstance(actions, dict):
            specs.extend(spec for spec in actions.values() if isinstance(spec, dict))
        for spec in specs:
            for field in ("path", "metadata"):
                value = spec.get(field)
                if not isinstance(value, str) or not value:
                    continue
                path = (game_dir / value).resolve()
                if path.is_relative_to(game_dir):
                    paths.add(path)
                    if field == "path":
                        paths.add(path.with_suffix(".asset.json"))
                        paths.add(path.with_suffix(".bbox.json"))
    return paths


def _tree_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return (
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".archive" not in path.parts
        and not path.name.endswith((".tmp", ".log"))
    )


def build_input_files(game_dir: Path, config_path: Path | None = None) -> list[Path]:
    """Return the canonical files whose content defines a verifiable build."""
    game_dir = game_dir.resolve()
    paths: set[Path] = {
        game_dir / "index.html",
        game_dir / "GAME_DESIGN.md",
        *_manifest_paths(game_dir),
    }
    for root in (
        game_dir / "src",
        game_dir / "levels",
        game_dir / "design",
        game_dir / ".playtest" / "tests",
        game_dir / ".playtest" / "inputs",
    ):
        paths.update(_tree_files(root))
    if config_path is not None:
        paths.add(config_path.resolve())
    return sorted((path for path in paths if path.is_file()), key=lambda p: str(p))


def compute_build_digest(game_dir: Path, config_path: Path | None = None) -> str:
    """Hash canonical build inputs using a stable path+content framing."""
    game_dir = game_dir.resolve()
    digest = hashlib.sha256()
    for path in build_input_files(game_dir, config_path):
        try:
            label = path.relative_to(game_dir).as_posix()
        except ValueError:
            label = f"@config/{path.name}"
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def new_pipeline_run_id() -> str:
    return f"pipeline-{uuid.uuid4()}"


def new_verify_run_id() -> str:
    return f"verify-{uuid.uuid4()}"


def read_pipeline_identity(game_dir: Path) -> dict[str, str] | None:
    state_path = game_dir / ".playtest" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pipeline_run_id = state.get("pipeline_run_id")
    build_digest = state.get("build_digest")
    if not isinstance(pipeline_run_id, str) or not isinstance(build_digest, str):
        return None
    return {
        "pipeline_run_id": pipeline_run_id,
        "build_digest": build_digest,
    }


def stamp_json_file(path: Path, identity: dict[str, str]) -> dict[str, Any]:
    """Attach run identity to a producer JSON artifact atomically."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot stamp invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cannot stamp non-object JSON artifact: {path}")
    payload.update(identity)
    atomic_write_json(path, payload)
    return payload


def stamp_jsonl_last(
    path: Path,
    identity: dict[str, str],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Identity-stamp the newest matching JSONL row and replace the file."""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Cannot read JSONL artifact: {path}") from exc
    parsed: list[dict[str, Any] | None] = []
    for line in raw_lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = None
        parsed.append(value if isinstance(value, dict) else None)
    target = None
    for index in range(len(parsed) - 1, -1, -1):
        value = parsed[index]
        if value is not None and (run_id is None or value.get("run_id") == run_id):
            target = index
            break
    if target is None:
        raise ValueError(f"No matching JSONL row to stamp: {path}")
    payload = parsed[target]
    assert payload is not None
    payload.update(identity)
    raw_lines[target] = json.dumps(payload, ensure_ascii=False)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return payload
