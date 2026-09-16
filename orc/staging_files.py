"""Descriptor-bound files for child-writable attempt staging directories."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from orc.errors import ChildExecutionError, DlpBoundaryError
from orc.io_utils import canonical_json


class SecureStagingDirectory:
    """Keep Manager reads, writes, and cleanup bound to created directory inodes."""

    def __init__(self, worktree: Path, name: str, allowed_names: set[str]) -> None:
        self.path = worktree / name
        self._worktree = worktree
        self._name = name
        self._allowed_names = frozenset(allowed_names)
        self._worktree_fd: int | None = None
        self._directory_fd: int | None = None
        self._identity: tuple[int, int] | None = None

    @property
    def directory_fd(self) -> int:
        """Return the live staging descriptor without transferring ownership."""
        if self._directory_fd is None:
            raise ChildExecutionError("staging directory is not open")
        return self._directory_fd

    def create(self, schema_name: str, schema: dict[str, Any]) -> None:
        """Create a fresh directory and schema through no-follow descriptors."""
        if schema_name not in self._allowed_names:
            raise ValueError("schema name is outside staging allowlist")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        worktree_fd = os.open(self._worktree, flags)
        try:
            worktree_info = os.fstat(worktree_fd)
            path_info = self._worktree.lstat()
            if (
                not stat.S_ISDIR(worktree_info.st_mode)
                or worktree_info.st_dev != path_info.st_dev
                or worktree_info.st_ino != path_info.st_ino
            ):
                raise ChildExecutionError("worktree directory changed before staging create")
            try:
                os.mkdir(self._name, mode=0o700, dir_fd=worktree_fd)
            except FileExistsError as error:
                raise ChildExecutionError(
                    f"attempt staging path already exists: {self._name}"
                ) from error
            directory_fd = os.open(self._name, flags, dir_fd=worktree_fd)
        except Exception:
            os.close(worktree_fd)
            raise
        self._worktree_fd = worktree_fd
        self._directory_fd = directory_fd
        info = os.fstat(directory_fd)
        self._identity = (info.st_dev, info.st_ino)
        try:
            self._write_private(schema_name, canonical_json(schema) + b"\n")
        except Exception:
            self.cleanup()
            raise

    def exists(self, name: str) -> bool:
        """Check a direct child without following a child-supplied symlink."""
        self._require_name(name)
        try:
            os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def read_bytes(self, name: str, *, max_bytes: int | None = None) -> bytes:
        """Read one exact regular single-link file from the bound directory."""
        self._require_name(name)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=self.directory_fd)
        except (FileNotFoundError, OSError) as error:
            raise DlpBoundaryError("DLP_SOURCE_UNAVAILABLE") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise DlpBoundaryError("DLP_SOURCE_NOT_REGULAR")
            if info.st_nlink != 1:
                raise DlpBoundaryError("DLP_SOURCE_HARDLINK")
            limit = max_bytes if max_bytes is not None else info.st_size
            if limit < 0:
                raise ValueError("staging read limit must be non-negative")
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            payload = b"".join(chunks)
            if len(payload) > limit or os.read(descriptor, 1):
                raise DlpBoundaryError("DLP_SOURCE_SIZE_LIMIT")
            return payload
        finally:
            os.close(descriptor)

    def digest(self, name: str) -> str:
        """Hash an exact bound staging file."""
        return hashlib.sha256(self.read_bytes(name)).hexdigest()

    def size(self, name: str) -> int:
        """Return a regular file size from the bound directory."""
        self._require_name(name)
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self.directory_fd,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DlpBoundaryError("DLP_SOURCE_NOT_REGULAR")
            return info.st_size
        finally:
            os.close(descriptor)

    def cleanup(self) -> None:
        """Unlink only direct children of the original inode, never a replacement path."""
        directory_fd = self._directory_fd
        worktree_fd = self._worktree_fd
        if directory_fd is None or worktree_fd is None:
            return
        error: Exception | None = None
        try:
            entries = set(os.listdir(directory_fd))
            unexpected = entries - self._allowed_names
            if unexpected:
                raise ChildExecutionError(
                    f"unexpected attempt staging entries: {','.join(sorted(unexpected))}"
                )
            for name in entries:
                os.unlink(name, dir_fd=directory_fd)
            try:
                current = os.stat(self._name, dir_fd=worktree_fd, follow_symlinks=False)
            except FileNotFoundError as changed:
                raise ChildExecutionError("attempt staging directory was replaced") from changed
            if self._identity != (current.st_dev, current.st_ino) or not stat.S_ISDIR(
                current.st_mode
            ):
                raise ChildExecutionError("attempt staging directory was replaced")
            os.rmdir(self._name, dir_fd=worktree_fd)
        except Exception as caught:
            error = caught
        finally:
            os.close(directory_fd)
            os.close(worktree_fd)
            self._directory_fd = None
            self._worktree_fd = None
        if error is not None:
            raise error

    def _write_private(self, name: str, payload: bytes) -> None:
        self._require_name(name)
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=self.directory_fd,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _require_name(self, name: str) -> None:
        if name not in self._allowed_names or Path(name).name != name:
            raise ValueError("staging filename is outside allowlist")
