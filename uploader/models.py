"""Domain types shared across the uploader package.

This module contains pure data classes and enums — no business logic,
no I/O, no external dependencies beyond the standard library.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class FileStatus(str, Enum):
    """Lifecycle states of a single file in the upload pipeline."""
    PENDING     = "pending"       # Not yet processed
    IN_PROGRESS = "in_progress"   # Currently being uploaded (cleared on restart)
    UPLOADED    = "uploaded"      # Successfully registered in Google Photos
    ERROR       = "error"         # Failed after exhausting retries
    SKIPPED     = "skipped"       # 0-byte, >200 MB, or duplicate of an UPLOADED file


class CompressionLevel(str, Enum):
    """Named compression presets that map to JPEG quality values."""
    LOW  = "low"   # quality=92 — light compression, ~30-40% smaller
    MID  = "mid"   # quality=85 — Google "Storage Saver" equivalent (default)
    HIGH = "high"  # quality=60 — aggressive compression, ~70-80% smaller

    def jpeg_quality(self) -> int:
        return {
            CompressionLevel.LOW:  92,
            CompressionLevel.MID:  85,
            CompressionLevel.HIGH: 60,
        }[self]


# ---------------------------------------------------------------------------
# Supported file extensions
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".heic", ".heif", ".raw", ".arw", ".cr2", ".nef", ".dng",
    # Videos
    ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".3gp", ".mpg", ".mpeg",
})

COMPRESSIBLE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".3gp", ".mpg", ".mpeg",
})

MAX_FILE_SIZE_BYTES: int = 200 * 1024 * 1024  # 200 MB — Google Photos hard limit


def is_video(path: Path) -> bool:
    """Return True if *path* has a video extension."""
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    """Mirrors a row in the SQLite `files` table."""
    path: str
    status: FileStatus = FileStatus.PENDING
    id: Optional[int] = None
    content_hash: Optional[str] = None
    upload_token: Optional[str] = None        # bytes-upload token (transient)
    google_media_id: Optional[str] = None     # assigned by batchCreate
    attempts: int = 0
    error_msg: Optional[str] = None
    original_size_bytes: Optional[int] = None
    compressed_size_bytes: Optional[int] = None
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


@dataclass
class UploadToken:
    """Handoff object placed on the token queue by a worker thread."""
    file_id: int
    token: str
    original_path: str
    album_id: Optional[str] = None


@dataclass
class BatchCreateResult:
    """Result for a single item returned by the batchCreate API call."""
    file_id: int
    success: bool
    google_media_id: Optional[str] = None
    error_msg: Optional[str] = None


@dataclass
class CompressionResult:
    """Outcome of compressing a single file."""
    source_path: str
    output_path: str          # Path to the (possibly compressed) file to upload
    original_bytes: int
    output_bytes: int         # Same as original_bytes if compression was skipped
    was_compressed: bool      # False if original was used (e.g. skip_if_larger triggered)

    @property
    def bytes_saved(self) -> int:
        return self.original_bytes - self.output_bytes

    @property
    def ratio(self) -> float:
        """Compression ratio as a fraction (0.0–1.0). 0.0 means no savings."""
        if self.original_bytes == 0:
            return 0.0
        return self.bytes_saved / self.original_bytes


@dataclass
class RunStats:
    """Aggregated progress counters for the current upload run."""
    total: int = 0
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    total_original_bytes: int = 0     # Sum of original_size_bytes for processed files
    total_compressed_bytes: int = 0   # Sum of compressed_size_bytes (or original when not compressed)
    start_time: Optional[datetime.datetime] = None

    # Per-type counts (photos vs videos)
    total_photos: int = 0
    total_videos: int = 0
    uploaded_photos: int = 0
    uploaded_videos: int = 0
    skipped_photos: int = 0
    skipped_videos: int = 0
    errors_photos: int = 0
    errors_videos: int = 0

    @property
    def remaining(self) -> int:
        return self.total - self.uploaded - self.skipped - self.errors

    @property
    def bytes_saved(self) -> int:
        return self.total_original_bytes - self.total_compressed_bytes

    @property
    def compression_ratio(self) -> float:
        if self.total_original_bytes == 0:
            return 0.0
        return self.bytes_saved / self.total_original_bytes

    @property
    def elapsed_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        return (datetime.datetime.now(tz=datetime.timezone.utc) - self.start_time).total_seconds()

    @property
    def files_per_minute(self) -> float:
        elapsed = self.elapsed_seconds
        if elapsed < 1:
            return 0.0
        done = self.uploaded + self.skipped + self.errors
        return (done / elapsed) * 60

    @property
    def eta_seconds(self) -> Optional[float]:
        fpm = self.files_per_minute
        if fpm <= 0 or self.remaining <= 0:
            return None
        return (self.remaining / fpm) * 60
