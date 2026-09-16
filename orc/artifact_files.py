"""Filesystem boundaries for M6 staging reads and normal publication."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from orc.errors import DlpBoundaryError
from orc.paths import ensure_private_dir


@dataclass
class ValidatedSource:
    """One exact regular staging file read through a no-follow descriptor."""

    path: Path
    payload: bytes
    _parent_fd: int
    _name: str
    _device: int
    _inode: int

    def unlink(self) -> None:
        """Remove only the exact inode that supplied the scanned bytes."""
        info = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_dev != self._device
            or info.st_ino != self._inode
        ):
            raise DlpBoundaryError("DLP_SOURCE_CHANGED")
        os.unlink(self._name, dir_fd=self._parent_fd)

    def close(self) -> None:
        """Release the descriptor pinning the source parent directory."""
        if self._parent_fd >= 0:
            os.close(self._parent_fd)
            self._parent_fd = -1


class ArtifactSourceReader:
    """Reject every path/link/type/size ambiguity before returning bytes."""

    def __init__(self, allowed_roots: tuple[Path, ...], max_bytes: int) -> None:
        self.allowed_roots = tuple(path.resolve(strict=False) for path in allowed_roots)
        self.max_bytes = max_bytes

    def read(self, root: Path, relative: Path) -> ValidatedSource:
        """Read one child staging file without following links or path escapes."""
        invalid_part = any(part in {"", ".", ".."} for part in relative.parts)
        if relative.is_absolute() or not relative.parts or invalid_part:
            raise DlpBoundaryError("DLP_SOURCE_PATH_INVALID")
        if root.is_symlink():
            raise DlpBoundaryError("DLP_SOURCE_ROOT_INVALID")
        try:
            safe_root = root.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise DlpBoundaryError("DLP_SOURCE_UNAVAILABLE") from error
        if not any(safe_root.is_relative_to(allowed) for allowed in self.allowed_roots):
            raise DlpBoundaryError("DLP_SOURCE_OUTSIDE_STAGING")
        candidate = safe_root / relative
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            parent_fd = os.open(safe_root, directory_flags)
            root_info = os.fstat(parent_fd)
            visible_root = safe_root.lstat()
            if (
                root_info.st_dev != visible_root.st_dev
                or root_info.st_ino != visible_root.st_ino
            ):
                raise DlpBoundaryError("DLP_SOURCE_ROOT_CHANGED")
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            name = relative.parts[-1]
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise DlpBoundaryError("DLP_SOURCE_NOT_REGULAR")
            if info.st_nlink != 1:
                raise DlpBoundaryError("DLP_SOURCE_HARDLINK")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except (OSError, DlpBoundaryError) as error:
            if "parent_fd" in locals():
                os.close(parent_fd)
            if isinstance(error, DlpBoundaryError):
                raise
            raise DlpBoundaryError("DLP_SOURCE_UNAVAILABLE") from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
            ):
                raise DlpBoundaryError("DLP_SOURCE_CHANGED")
            payload = os.read(descriptor, self.max_bytes + 1)
            if len(payload) > self.max_bytes or os.read(descriptor, 1):
                raise DlpBoundaryError("DLP_SOURCE_SIZE_LIMIT")
        except Exception:
            os.close(parent_fd)
            raise
        finally:
            os.close(descriptor)
        return ValidatedSource(
            candidate,
            payload,
            parent_fd,
            name,
            opened.st_dev,
            opened.st_ino,
        )


def publish_artifact(run_dir: Path, kind: str, artifact_id: str, payload: bytes) -> Path:
    """Atomically publish one already-clean artifact at 0600."""
    target_dir = run_dir / "artifacts" / artifact_id
    created = not target_dir.exists()
    ensure_private_dir(target_dir)
    target = target_dir / f"{kind}.artifact"
    if target.exists():
        raise FileExistsError("normal artifact id already exists")
    try:
        write_private(target, payload)
    except OSError:
        if created and target_dir.exists() and not any(target_dir.iterdir()):
            target_dir.rmdir()
        raise
    return target


def write_private(path: Path, payload: bytes) -> None:
    """Write bytes through a same-directory 0600 temporary and atomic replace."""
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
