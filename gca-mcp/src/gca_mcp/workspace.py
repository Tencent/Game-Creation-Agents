"""Workspace lifecycle — the GCA analog of ``video-creation-mcp``'s canvas.

A "workspace" is a single game directory under ``playground/<game-slug>/``.
Tools that need a default game directory (gate-check, record_with_state,
extract_state) read it from the open workspace; explicit ``game_dir``
arguments still override.

Lazy mode: server boots without a workspace, agent calls
``init_workspace(path=..., mode="create"|"resume")``.

Eager mode: pass ``--workspace-root`` to the CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import (
    SecureFileLease,
    acquire_secure_file_lock,
    lock_repo_root,
    pipeline_state_lock,
    prune_stale_locks,
    recover_state_transaction,
    workspace_lock_path,
)
from .errors import ToolError


_WORKSPACE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class WorkspaceMeta:
    """Resolved workspace info exposed to handlers."""

    root: Path
    name: str
    mode: str
    _lease_file: SecureFileLease | None = field(default=None, repr=False, compare=False)
    _root_fd: int | None = field(default=None, repr=False, compare=False)
    _parent_fd: int | None = field(default=None, repr=False, compare=False)

    @property
    def playtest_dir(self) -> Path:
        return self.root / ".playtest"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def state_json(self) -> Path:
        return self.playtest_dir / "state.json"

    @property
    def lease_held(self) -> bool:
        return (
            self._lease_file is not None
            and not self._lease_file.closed
            and self._root_fd is not None
            and self._parent_fd is not None
        )

    def assert_identity(self) -> None:
        """Reject a workspace or parent replaced after init_workspace."""
        lease_file = self._lease_file
        root_fd = self._root_fd
        parent_fd = self._parent_fd
        if lease_file is None or root_fd is None or parent_fd is None:
            raise ToolError("WORKSPACE_CLOSED", "Workspace identity is no longer held.")
        lease_file.assert_current()
        try:
            opened_root = os.fstat(root_fd)
            current_root = os.stat(
                self.root.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            path_root = os.stat(self.root, follow_symlinks=False)
            opened_parent = os.fstat(parent_fd)
            current_parent = os.stat(self.root.parent, follow_symlinks=False)
        except OSError as exc:
            raise ToolError(
                "WORKSPACE_REPLACED",
                f"Workspace identity disappeared after init_workspace: {self.root}.",
            ) from exc
        if (
            not stat_module.S_ISDIR(opened_root.st_mode)
            or not stat_module.S_ISDIR(current_root.st_mode)
            or opened_root.st_dev != current_root.st_dev
            or opened_root.st_ino != current_root.st_ino
            or opened_root.st_dev != path_root.st_dev
            or opened_root.st_ino != path_root.st_ino
            or opened_parent.st_dev != current_parent.st_dev
            or opened_parent.st_ino != current_parent.st_ino
        ):
            raise ToolError(
                "WORKSPACE_REPLACED",
                f"Workspace or its parent changed after init_workspace: {self.root}.",
            )

    def _open_relative_parent(self, relative: Path, *, create: bool) -> tuple[int, str]:
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ToolError("INVALID_PATH", f"Invalid workspace-relative path: {relative}.")
        self.assert_identity()
        root_fd = self._root_fd
        assert root_fd is not None
        current_fd = os.dup(root_fd)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in relative.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, relative.parts[-1]
        except OSError as exc:
            os.close(current_fd)
            raise ToolError(
                "INVALID_PATH",
                f"Workspace path traverses a replaced or symlinked directory: {relative}.",
            ) from exc
        except BaseException:
            os.close(current_fd)
            raise

    def read_bytes_from_pinned(self, relative: Path) -> bytes:
        """Read one regular file through pinned, no-follow descriptors."""
        parent_fd, name = self._open_relative_parent(relative, create=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                source_fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise ToolError(
                    "INVALID_PATH",
                    f"Workspace file is missing, replaced, or symlinked: {relative}.",
                ) from exc
            metadata = os.fstat(source_fd)
            if not stat_module.S_ISREG(metadata.st_mode):
                os.close(source_fd)
                raise ToolError("INVALID_PATH", f"Input is not a file: {relative}.")
            with os.fdopen(source_fd, "rb") as source:
                return source.read()
        finally:
            os.close(parent_fd)

    def copy_from_pinned(self, relative: Path, destination: Path) -> None:
        """Copy one input from the pinned workspace without pathname traversal."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.read_bytes_from_pinned(relative))

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        os.fsync(fd)

    def create_bytes_in_pinned(self, relative: Path, data: bytes) -> None:
        """Create one authority file exactly once through a pinned parent."""
        parent_fd, name = self._open_relative_parent(relative, create=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                output_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise ToolError(
                    "REPLAYED_RECEIPT",
                    f"Pinned authority file already exists: {relative}.",
                ) from exc
            try:
                try:
                    self._write_all(output_fd, data)
                except BaseException:
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    raise
            finally:
                os.close(output_fd)
        finally:
            os.close(parent_fd)

    def replace_bytes_in_pinned(self, relative: Path, data: bytes) -> None:
        """Atomically replace one authority file through a pinned parent."""
        parent_fd, name = self._open_relative_parent(relative, create=True)
        temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            output_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            try:
                self._write_all(output_fd, data)
            finally:
                os.close(output_fd)
            os.rename(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def commit_to_pinned(self, source: Path, relative: Path) -> None:
        """Atomically copy one staged output through pinned directory descriptors."""
        parent_fd, name = self._open_relative_parent(relative, create=True)
        temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            output_fd = os.open(temp_name, flags, 0o644, dir_fd=parent_fd)
            try:
                with source.open("rb") as input_stream, os.fdopen(
                    output_fd,
                    "wb",
                ) as output_stream:
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except BaseException:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                raise
            os.rename(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)

    @staticmethod
    def _directory_open_flags() -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        return flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino

    @classmethod
    def _copy_tree_between_fds(
        cls,
        source_fd: int,
        destination_fd: int,
        *,
        source_path: Path,
    ) -> None:
        """Copy one staged tree without following entries within it."""
        with os.scandir(source_fd) as entries:
            for entry in entries:
                entry_path = source_path / entry.name
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ToolError(
                        "INVALID_PATH",
                        f"Could not inspect staged tree entry: {entry_path}.",
                    ) from exc

                if stat_module.S_ISLNK(metadata.st_mode):
                    raise ToolError(
                        "INVALID_PATH",
                        f"Staged publication tree contains a symlink: {entry_path}.",
                    )

                if stat_module.S_ISDIR(metadata.st_mode):
                    source_child_fd: int | None = None
                    destination_child_fd: int | None = None
                    try:
                        source_child_fd = os.open(
                            entry.name,
                            cls._directory_open_flags(),
                            dir_fd=source_fd,
                        )
                        opened = os.fstat(source_child_fd)
                        if not stat_module.S_ISDIR(
                            opened.st_mode
                        ) or not cls._same_identity(metadata, opened):
                            raise ToolError(
                                "INVALID_PATH",
                                f"Staged directory changed while publishing: {entry_path}.",
                            )
                        os.mkdir(entry.name, 0o755, dir_fd=destination_fd)
                        destination_child_fd = os.open(
                            entry.name,
                            cls._directory_open_flags(),
                            dir_fd=destination_fd,
                        )
                        cls._copy_tree_between_fds(
                            source_child_fd,
                            destination_child_fd,
                            source_path=entry_path,
                        )
                        os.fsync(destination_child_fd)
                    except OSError as exc:
                        raise ToolError(
                            "INVALID_PATH",
                            f"Could not securely copy staged directory: {entry_path}.",
                        ) from exc
                    finally:
                        if destination_child_fd is not None:
                            os.close(destination_child_fd)
                        if source_child_fd is not None:
                            os.close(source_child_fd)
                    continue

                if not stat_module.S_ISREG(metadata.st_mode):
                    raise ToolError(
                        "INVALID_PATH",
                        f"Staged publication tree contains a special file: {entry_path}.",
                    )

                source_file_fd: int | None = None
                destination_file_fd: int | None = None
                try:
                    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    read_flags |= getattr(os, "O_NOFOLLOW", 0)
                    source_file_fd = os.open(
                        entry.name,
                        read_flags,
                        dir_fd=source_fd,
                    )
                    opened = os.fstat(source_file_fd)
                    if not stat_module.S_ISREG(
                        opened.st_mode
                    ) or not cls._same_identity(metadata, opened):
                        raise ToolError(
                            "INVALID_PATH",
                            f"Staged file changed while publishing: {entry_path}.",
                        )
                    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    write_flags |= getattr(os, "O_CLOEXEC", 0)
                    write_flags |= getattr(os, "O_NOFOLLOW", 0)
                    destination_file_fd = os.open(
                        entry.name,
                        write_flags,
                        0o644,
                        dir_fd=destination_fd,
                    )
                    while chunk := os.read(source_file_fd, 1024 * 1024):
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(destination_file_fd, remaining)
                            remaining = remaining[written:]
                    os.fsync(destination_file_fd)
                except OSError as exc:
                    raise ToolError(
                        "INVALID_PATH",
                        f"Could not securely copy staged file: {entry_path}.",
                    ) from exc
                finally:
                    if destination_file_fd is not None:
                        os.close(destination_file_fd)
                    if source_file_fd is not None:
                        os.close(source_file_fd)

    @classmethod
    def _remove_tree_at(cls, parent_fd: int, name: str) -> None:
        """Remove one directory entry recursively without following symlinks."""
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat_module.S_ISDIR(metadata.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return

        child_fd = os.open(name, cls._directory_open_flags(), dir_fd=parent_fd)
        try:
            with os.scandir(child_fd) as entries:
                child_names = [entry.name for entry in entries]
            for child_name in child_names:
                cls._remove_tree_at(child_fd, child_name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)

    @classmethod
    def _remove_tree_best_effort(cls, parent_fd: int, name: str) -> None:
        try:
            cls._remove_tree_at(parent_fd, name)
        except (FileNotFoundError, OSError):
            pass

    def commit_files_to_pinned(
        self,
        replacements: list[tuple[Path, Path]],
        *,
        remove: list[Path] | None = None,
    ) -> None:
        """Transactionally replace/remove sibling files through one pinned parent."""
        removals = list(remove or [])
        destinations = [relative for _source, relative in replacements] + removals
        if not destinations:
            return
        for relative in destinations:
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ToolError(
                    "INVALID_PATH",
                    f"Invalid workspace-relative publication path: {relative}.",
                )
        parent_relative = destinations[0].parent
        if any(relative.parent != parent_relative for relative in destinations):
            raise ToolError(
                "INVALID_PATH",
                "Transactional file publication requires sibling destinations.",
            )
        destination_names = [relative.name for relative in destinations]
        if len(destination_names) != len(set(destination_names)):
            raise ToolError(
                "INVALID_PATH",
                "Transactional file publication contains duplicate destinations.",
            )

        parent_fd, _name = self._open_relative_parent(destinations[0], create=True)
        token = uuid.uuid4().hex
        replacement_names: dict[str, str] = {}
        backup_names: dict[str, str] = {}
        existing_metadata: dict[str, os.stat_result] = {}
        existing_fds: list[int] = []
        backed_up: set[str] = set()
        installed: set[str] = set()
        committed = False

        def fsync_parent(errors: list[BaseException] | None = None) -> None:
            try:
                os.fsync(parent_fd)
            except BaseException as exc:
                if errors is None:
                    raise
                errors.append(exc)

        try:
            self.assert_identity()
            read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            read_flags |= getattr(os, "O_NOFOLLOW", 0)
            for name in destination_names:
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat_module.S_ISREG(metadata.st_mode):
                    raise ToolError(
                        "INVALID_PATH",
                        "Pinned file transaction destinations must be regular files: "
                        f"{parent_relative / name}.",
                    )
                try:
                    existing_fd = os.open(name, read_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise ToolError(
                        "INVALID_PATH",
                        "Pinned file transaction destination was replaced or symlinked: "
                        f"{parent_relative / name}.",
                    ) from exc
                existing_fds.append(existing_fd)
                if not self._same_identity(metadata, os.fstat(existing_fd)):
                    raise ToolError(
                        "INVALID_PATH",
                        "Pinned file transaction destination changed while opening: "
                        f"{parent_relative / name}.",
                    )
                existing_metadata[name] = metadata
                backup_names[name] = f".{name}.{token}.bak"

            for source, relative in replacements:
                name = relative.name
                replacement_name = f".{name}.{token}.tmp"
                replacement_names[name] = replacement_name
                try:
                    source_metadata = os.stat(source, follow_symlinks=False)
                    source_fd = os.open(source, read_flags)
                except OSError as exc:
                    raise ToolError(
                        "INVALID_PATH",
                        f"Staged publication source is missing or unsafe: {source}.",
                    ) from exc
                try:
                    opened_source = os.fstat(source_fd)
                    if (
                        not stat_module.S_ISREG(source_metadata.st_mode)
                        or not stat_module.S_ISREG(opened_source.st_mode)
                        or not self._same_identity(source_metadata, opened_source)
                    ):
                        raise ToolError(
                            "INVALID_PATH",
                            f"Staged publication source is not a stable regular file: {source}.",
                        )
                    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    write_flags |= getattr(os, "O_CLOEXEC", 0)
                    write_flags |= getattr(os, "O_NOFOLLOW", 0)
                    output_fd = os.open(
                        replacement_name,
                        write_flags,
                        0o644,
                        dir_fd=parent_fd,
                    )
                    try:
                        while chunk := os.read(source_fd, 1024 * 1024):
                            remaining = memoryview(chunk)
                            while remaining:
                                written = os.write(output_fd, remaining)
                                remaining = remaining[written:]
                        os.fsync(output_fd)
                    finally:
                        os.close(output_fd)
                finally:
                    os.close(source_fd)

            self.assert_identity()
            try:
                for name, metadata in existing_metadata.items():
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not self._same_identity(metadata, current):
                        raise ToolError(
                            "INVALID_PATH",
                            "Pinned file transaction destination changed before swap: "
                            f"{parent_relative / name}.",
                        )
                    os.rename(
                        name,
                        backup_names[name],
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    backed_up.add(name)
                    fsync_parent()
                    moved = os.stat(
                        backup_names[name],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if not self._same_identity(metadata, moved):
                        raise ToolError(
                            "INVALID_PATH",
                            "Pinned file transaction destination changed during swap: "
                            f"{parent_relative / name}.",
                        )

                for name in destination_names:
                    try:
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise ToolError(
                        "INVALID_PATH",
                        "Pinned file transaction destination appeared during swap: "
                        f"{parent_relative / name}.",
                    )

                for _source, relative in replacements:
                    name = relative.name
                    os.rename(
                        replacement_names[name],
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    installed.add(name)
                    fsync_parent()
            except BaseException:
                rollback_errors: list[BaseException] = []
                for _source, relative in reversed(replacements):
                    name = relative.name
                    if name not in installed:
                        continue
                    try:
                        os.rename(
                            name,
                            replacement_names[name],
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        installed.remove(name)
                    except BaseException as exc:
                        rollback_errors.append(exc)
                    else:
                        fsync_parent(rollback_errors)
                for name in reversed(destination_names):
                    if name not in backed_up or name in installed:
                        continue
                    try:
                        os.rename(
                            backup_names[name],
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        backed_up.remove(name)
                    except BaseException as exc:
                        rollback_errors.append(exc)
                    else:
                        fsync_parent(rollback_errors)
                if rollback_errors:
                    raise ToolError(
                        "INTERNAL_ERROR",
                        "Could not fully restore files after a failed pinned transaction.",
                    ) from rollback_errors[0]
                raise

            committed = True
        finally:
            for existing_fd in existing_fds:
                os.close(existing_fd)
            if committed:
                for backup_name in backup_names.values():
                    self._remove_tree_best_effort(parent_fd, backup_name)
            for replacement_name in replacement_names.values():
                self._remove_tree_best_effort(parent_fd, replacement_name)
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            os.close(parent_fd)

    def commit_tree_to_pinned(self, source: Path, relative: Path) -> None:
        """Publish a complete staged directory through one rollback-safe swap."""
        parent_fd, name = self._open_relative_parent(relative, create=True)
        token = uuid.uuid4().hex
        replacement_name = f".{name}.{token}.tmp"
        backup_name = f".{name}.{token}.bak"
        source_fd: int | None = None
        replacement_fd: int | None = None
        existing_fd: int | None = None
        existing_metadata: os.stat_result | None = None
        old_moved = False
        replacement_installed = False
        published = False
        try:
            try:
                existing_metadata = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing_metadata = None
            if existing_metadata is not None:
                if not stat_module.S_ISDIR(existing_metadata.st_mode):
                    raise ToolError(
                        "INVALID_PATH",
                        f"Pinned tree destination is not a real directory: {relative}.",
                    )
                try:
                    existing_fd = os.open(
                        name,
                        self._directory_open_flags(),
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise ToolError(
                        "INVALID_PATH",
                        f"Pinned tree destination is replaced or symlinked: {relative}.",
                    ) from exc
                opened_existing = os.fstat(existing_fd)
                if not self._same_identity(existing_metadata, opened_existing):
                    raise ToolError(
                        "INVALID_PATH",
                        f"Pinned tree destination changed while opening: {relative}.",
                    )

            try:
                source_fd = os.open(source, self._directory_open_flags())
            except OSError as exc:
                raise ToolError(
                    "INVALID_PATH",
                    f"Staged publication source is not a real directory: {source}.",
                ) from exc
            if not stat_module.S_ISDIR(os.fstat(source_fd).st_mode):
                raise ToolError(
                    "INVALID_PATH",
                    f"Staged publication source is not a directory: {source}.",
                )

            os.mkdir(replacement_name, 0o755, dir_fd=parent_fd)
            replacement_fd = os.open(
                replacement_name,
                self._directory_open_flags(),
                dir_fd=parent_fd,
            )
            self._copy_tree_between_fds(
                source_fd,
                replacement_fd,
                source_path=source,
            )
            os.fsync(replacement_fd)
            os.close(replacement_fd)
            replacement_fd = None
            os.close(source_fd)
            source_fd = None

            self.assert_identity()
            try:
                current_metadata = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current_metadata = None
            if existing_metadata is None:
                if current_metadata is not None:
                    raise ToolError(
                        "INVALID_PATH",
                        f"Pinned tree destination appeared while publishing: {relative}.",
                    )
            elif current_metadata is None or not self._same_identity(
                existing_metadata,
                current_metadata,
            ):
                raise ToolError(
                    "INVALID_PATH",
                    f"Pinned tree destination changed while publishing: {relative}.",
                )

            try:
                if existing_metadata is not None:
                    os.rename(
                        name,
                        backup_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    old_moved = True
                    os.fsync(parent_fd)
                    moved_metadata = os.stat(
                        backup_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if not self._same_identity(existing_metadata, moved_metadata):
                        raise ToolError(
                            "INVALID_PATH",
                            f"Pinned tree destination changed during swap: {relative}.",
                        )
                os.rename(
                    replacement_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                replacement_installed = True
                os.fsync(parent_fd)
            except BaseException:
                rollback_errors: list[BaseException] = []
                if replacement_installed:
                    try:
                        os.rename(
                            name,
                            replacement_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        replacement_installed = False
                    except BaseException as rollback_error:
                        rollback_errors.append(rollback_error)
                    else:
                        try:
                            os.fsync(parent_fd)
                        except BaseException as rollback_error:
                            rollback_errors.append(rollback_error)
                if old_moved and not replacement_installed:
                    try:
                        os.rename(
                            backup_name,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        old_moved = False
                    except BaseException as rollback_error:
                        rollback_errors.append(rollback_error)
                    else:
                        try:
                            os.fsync(parent_fd)
                        except BaseException as rollback_error:
                            rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise ToolError(
                        "INTERNAL_ERROR",
                        f"Could not restore pinned tree after publication failed: {relative}.",
                    ) from rollback_errors[0]
                raise
            published = True
        finally:
            if replacement_fd is not None:
                os.close(replacement_fd)
            if source_fd is not None:
                os.close(source_fd)
            if existing_fd is not None:
                os.close(existing_fd)
            if published:
                self._remove_tree_best_effort(parent_fd, backup_name)
                self._remove_tree_best_effort(parent_fd, replacement_name)
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
            elif not old_moved:
                self._remove_tree_best_effort(parent_fd, replacement_name)
            os.close(parent_fd)

    def unlink_from_pinned(
        self,
        relative: Path,
        expected: dict[str, object],
    ) -> bool:
        """Revalidate and unlink one file through its pinned parent directory."""
        parent_fd, name = self._open_relative_parent(relative, create=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                file_fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if expected.get("exists") is False:
                    return False
                raise ToolError(
                    "STALE_RECEIPT",
                    f"Sealed stale file disappeared: {relative}.",
                )
            try:
                metadata = os.fstat(file_fd)
                digest = hashlib.sha256()
                with os.fdopen(os.dup(file_fd), "rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                current = {
                    "exists": True,
                    "sha256": digest.hexdigest(),
                    "size": metadata.st_size,
                    "st_dev": metadata.st_dev,
                    "st_ino": metadata.st_ino,
                }
                if (
                    not stat_module.S_ISREG(metadata.st_mode)
                    or current != expected
                ):
                    raise ToolError(
                        "STALE_RECEIPT",
                        f"Stale-owned file changed after sealing: {relative}.",
                    )
                os.unlink(name, dir_fd=parent_fd)
                return True
            finally:
                os.close(file_fd)
        finally:
            os.close(parent_fd)

    def close(self) -> None:
        """Release this process's exclusive workspace lease."""
        lease_file = self._lease_file
        if lease_file is None:
            return
        self._lease_file = None
        root_fd, parent_fd = self._root_fd, self._parent_fd
        self._root_fd = None
        self._parent_fd = None
        try:
            if root_fd is not None:
                os.close(root_fd)
            if parent_fd is not None:
                os.close(parent_fd)
        finally:
            lease_file.close()

    def __enter__(self) -> WorkspaceMeta:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "name": self.name,
            "mode": self.mode,
            "playtest_dir": str(self.playtest_dir),
            "assets_dir": str(self.assets_dir),
            "lease_held": self.lease_held,
        }


def resolve_workspace_root(path: str | Path, *, repo_root: Path) -> Path:
    """Resolve one exact ``<repo>/playground/<game-slug>`` root."""
    if not path:
        raise ToolError("INVALID_PATH", "init_workspace requires a non-empty path.")
    repo_root = repo_root.expanduser().resolve()
    raw = Path(str(path)).expanduser()
    if not raw.is_absolute():
        raw = repo_root / raw
    root = raw.resolve(strict=False)
    playground = (repo_root / "playground").resolve(strict=False)
    try:
        relative = root.relative_to(playground)
    except ValueError as exc:
        raise ToolError(
            "INVALID_PATH",
            f"Workspace must be directly below {playground}; got {root}.",
        ) from exc
    if len(relative.parts) != 1 or relative.parts[0] in {"", ".", ".."}:
        raise ToolError(
            "INVALID_PATH",
            f"Workspace must be exactly playground/<game-slug>; got {root}.",
        )
    if not _WORKSPACE_SLUG_RE.fullmatch(relative.parts[0]):
        raise ToolError(
            "INVALID_PATH",
            "Workspace game slug must use 1-64 lowercase letters, digits, "
            "hyphens, or underscores and start with a letter or digit.",
        )
    return root


def validated_workspace_root(workspace: object) -> Path:
    """Return the pinned root; lightweight test doubles fall back to resolution."""
    validator = getattr(workspace, "assert_identity", None)
    if callable(validator):
        validator()
        return Path(getattr(workspace, "root"))
    return Path(getattr(workspace, "root")).resolve()


def _pin_workspace_identity(root: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root.parent, flags)
        try:
            root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        except BaseException:
            os.close(parent_fd)
            raise
    except OSError as exc:
        raise ToolError(
            "WORKSPACE_INIT_ERROR",
            f"Could not pin workspace directory identity: {exc}",
        ) from exc
    return root_fd, parent_fd


def _create_mode_blockers(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    blockers: list[str] = []
    for child in root.iterdir():
        if child.name != ".playtest":
            blockers.append(child.name)
            continue
        if not child.is_dir():
            blockers.append(".playtest")
            continue
        for entry in child.iterdir():
            if entry.name == "audit.jsonl":
                try:
                    rows = [
                        json.loads(line)
                        for line in entry.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                except (OSError, json.JSONDecodeError):
                    blockers.append(".playtest/audit.jsonl")
                    continue
                if any(
                    not isinstance(row, dict) or row.get("event") != "preflight_check"
                    for row in rows
                ):
                    blockers.append(".playtest/audit.jsonl")
            elif entry.name not in {
                "preflight",
                ".state.lock",
                ".workspace.lock",
            }:
                blockers.append(f".playtest/{entry.name}")
    return blockers


def _prepare_playtest_dir(root: Path) -> None:
    """Create ``.playtest`` or reject any non-directory entry without following it."""
    playtest_dir = root / ".playtest"
    try:
        metadata = playtest_dir.lstat()
    except FileNotFoundError:
        # Do not use exist_ok: a racing replacement must fail closed.
        playtest_dir.mkdir()
        metadata = playtest_dir.lstat()
    if not stat_module.S_ISDIR(metadata.st_mode):
        raise ToolError(
            "WORKSPACE_INIT_ERROR",
            f"Workspace .playtest must be a real directory, not a symlink: {playtest_dir}.",
        )


def _acquire_workspace_lease(root: Path) -> SecureFileLease:
    """Acquire one non-blocking process lease before inspecting workspace data."""
    lock_path = workspace_lock_path(root, "workspace")
    try:
        lease_file = acquire_secure_file_lock(
            lock_path,
            exclusive=True,
            nonblocking=True,
        )
    except BlockingIOError as exc:
        raise ToolError(
            "WORKSPACE_ALREADY_OPEN",
            f"Workspace is already leased by another process: {root}.",
            hint="Use the process that owns this workspace or close it before retrying.",
        ) from exc
    except ToolError:
        raise
    try:
        root.mkdir(parents=True, exist_ok=True)
        _prepare_playtest_dir(root)
    except ToolError:
        lease_file.close()
        raise
    except OSError as exc:
        lease_file.close()
        raise ToolError(
            "WORKSPACE_INIT_ERROR",
            f"Could not prepare workspace lease: {exc}",
        ) from exc
    return lease_file


def open_workspace(
    path: str | Path,
    *,
    repo_root: Path,
    mode: str,
    name: str | None = None,
) -> WorkspaceMeta:
    """Open or create a workspace at ``path``.

    In create mode the folder may be absent and will be created. Lifecycle owns
    only ``.playtest`` lock bookkeeping; downstream agents own game/build layout.
    """
    if mode not in {"create", "resume"}:
        raise ToolError("INVALID_CONFIG", "Workspace mode must be `create` or `resume`.")
    if name is not None and (
        not isinstance(name, str) or not _WORKSPACE_SLUG_RE.fullmatch(name)
    ):
        raise ToolError("INVALID_PATH", "Workspace name must be a filesystem-safe slug.")
    root = resolve_workspace_root(path, repo_root=repo_root)
    prune_stale_locks(lock_repo_root(root))
    lease_file = _acquire_workspace_lease(root)
    try:
        root_fd, parent_fd = _pin_workspace_identity(root)
    except BaseException:
        lease_file.close()
        raise
    meta = WorkspaceMeta(
        root=root,
        name=name or root.name,
        mode=mode,
        _lease_file=lease_file,
        _root_fd=root_fd,
        _parent_fd=parent_fd,
    )
    try:
        meta.assert_identity()
        if mode == "create":
            blockers = _create_mode_blockers(root)
            if blockers:
                raise ToolError(
                    "WORKSPACE_NOT_CLEAN",
                    "Create mode refuses an existing game workspace: "
                    + ", ".join(blockers[:8]),
                    hint="Choose a new game slug or explicitly use mode='resume'.",
                )
        else:
            with pipeline_state_lock(meta.playtest_dir):
                recover_state_transaction(meta.playtest_dir)
                state_path = meta.state_json
                if not state_path.is_file():
                    raise ToolError(
                        "WORKSPACE_NOT_RESUMABLE",
                        f"Resume mode requires {state_path} or a valid pending "
                        "state transaction.",
                    )
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ToolError(
                        "WORKSPACE_NOT_RESUMABLE",
                        f"Resume state is unreadable or malformed: {exc}",
                    ) from exc
                if not isinstance(state, dict):
                    raise ToolError(
                        "WORKSPACE_NOT_RESUMABLE",
                        "Resume state.json must contain a JSON object.",
                    )
    except BaseException:
        meta.close()
        raise
    return meta


__all__ = [
    "WorkspaceMeta",
    "open_workspace",
    "resolve_workspace_root",
    "validated_workspace_root",
]
