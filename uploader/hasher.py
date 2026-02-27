"""SHA-256 content hashing with streaming I/O.

Files are read in 64 KB chunks to avoid loading large files into memory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from uploader.errors import FileReadError


_CHUNK_SIZE = 64 * 1024  # 64 KB


def hash_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file.

    Raises:
        FileReadError: if the file cannot be read (missing, permissions, I/O error).
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
                h.update(chunk)
    except OSError as e:
        raise FileReadError(f"Cannot read {path}: {e}") from e
    return h.hexdigest()


def hash_file_safe(path: Path) -> tuple[Optional[str], Optional[Exception]]:
    """Like hash_file() but returns (digest, None) on success or (None, exc) on failure.

    Useful in worker threads where exceptions must be captured rather than propagated.
    """
    try:
        return hash_file(path), None
    except Exception as exc:
        return None, exc
