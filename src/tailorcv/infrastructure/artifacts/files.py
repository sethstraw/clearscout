"""Write files that hold personal data so that only their owner can read them.

Files are created 0600 and private directories 0700. Permission bits are
advisory on Windows, so a failure to set them is ignored.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from tailorcv.shared.errors import ConfigError

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


def private_directory(path: Path) -> Path:
    """Create a directory only its owner can enter, and return it."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIRECTORY_MODE)
    return path


def write_private(path: Path, text: str) -> Path:
    """Write a file only its owner can read.

    Permissions are set before the content, so the data is never briefly
    world-readable. The parent directory is created but not tightened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as error:
        raise ConfigError(f"Cannot write {path}: {error}") from error
    _chmod(path, FILE_MODE)
    return path


def write_private_bytes(path: Path, content: bytes) -> Path:
    """Write binary personal data with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    except OSError as error:
        raise ConfigError(f"Cannot write {path}: {error}") from error
    _chmod(path, FILE_MODE)
    return path


def replace_private(path: Path, text: str) -> Path:
    """Replace a file's entire contents in one operation, owner-only.

    Written to a temporary sibling and renamed over the target, so a reader
    sees the whole old file or the whole new one and never half of either.
    The temporary file is 0600 and is removed on any failure.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".tailorcv-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        _chmod(temporary, FILE_MODE)
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"Cannot replace {path}: {error}") from error
    return path


def _chmod(path: Path, mode: int) -> None:
    """Apply a mode, tolerating filesystems that do not implement them."""
    try:
        path.chmod(mode)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        return


def sync_directory(path: Path) -> None:
    """Persist a rename when the host supports directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
