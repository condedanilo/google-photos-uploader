"""Recursive directory scanner with live progress callback.

The scanner yields Path objects as it finds them, allowing the caller to display
a running file count without buffering the entire result set.

Design notes:
  - Uses os.scandir() for efficiency (avoids extra stat calls on directories)
  - Extension filtering against SUPPORTED_EXTENSIONS (or a custom subset)
  - Skips symlinks by default (configurable)
  - Does NOT compute hashes — that is done separately to keep responsibilities clean
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generator, Iterator, Optional

from uploader.models import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Summary returned after scanning completes."""
    paths: list[Path] = field(default_factory=list)
    skipped_count: int = 0   # files ignored (wrong extension, symlink, etc.)
    photo_count: int = 0     # accepted files with image extensions
    video_count: int = 0     # accepted files with video extensions


def scan_directory(
    root: Path,
    *,
    include_extensions: Optional[frozenset[str]] = None,
    follow_symlinks: bool = False,
    on_file_found: Optional[Callable[[int, Path], None]] = None,
) -> ScanResult:
    """Recursively scan *root* and collect all supported media files.

    Args:
        root: The directory to scan.
        include_extensions: If provided, only include files with these extensions
            (e.g. frozenset({".jpg", ".mp4"})). Defaults to SUPPORTED_EXTENSIONS.
        follow_symlinks: If True, follow symbolic links to directories/files.
        on_file_found: Optional callback called after each accepted file:
            ``on_file_found(total_found_so_far, path)``.
            Use this to drive a live progress display.

    Returns:
        ScanResult with the collected paths and a count of skipped entries.

    Raises:
        NotADirectoryError: if *root* is not a directory.
        PermissionError: if *root* cannot be read.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    allowed = include_extensions if include_extensions is not None else SUPPORTED_EXTENSIONS
    result = ScanResult()

    for path in _walk(root, follow_symlinks=follow_symlinks):
        ext = path.suffix.lower()
        if ext not in allowed:
            result.skipped_count += 1
            continue
        result.paths.append(path)
        if ext in VIDEO_EXTENSIONS:
            result.video_count += 1
        else:
            result.photo_count += 1
        if on_file_found is not None:
            on_file_found(len(result.paths), path)

    return result


# ---------------------------------------------------------------------------
# Internal walker
# ---------------------------------------------------------------------------

def _walk(root: Path, follow_symlinks: bool) -> Iterator[Path]:
    """Depth-first directory walk using os.scandir for efficiency."""
    try:
        entries = list(os.scandir(root))
    except PermissionError:
        return   # skip unreadable directories silently

    for entry in entries:
        if entry.is_symlink() and not follow_symlinks:
            continue
        if entry.is_dir(follow_symlinks=follow_symlinks):
            yield from _walk(Path(entry.path), follow_symlinks=follow_symlinks)
        elif entry.is_file(follow_symlinks=follow_symlinks):
            yield Path(entry.path)
