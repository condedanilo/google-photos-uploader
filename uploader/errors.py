"""Domain error hierarchy and human-readable message mapping.

All errors raised inside the uploader package are subclasses of UploaderError.
The cli layer catches these and displays human_message(exc) to the user.
"""
from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class UploaderError(Exception):
    """Root exception for all uploader errors."""


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class AuthError(UploaderError):
    """Could not obtain valid Google credentials."""


class TokenRefreshError(AuthError):
    """Existing token could not be refreshed; re-authentication required."""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ApiError(UploaderError):
    """An error response from the Google Photos API."""

    def __init__(self, status_code: int, message: str, reason: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason  # e.g. "dailyLimitExceeded" from API error body


class RateLimitError(ApiError):
    """HTTP 429 — too many requests; retry after a delay."""

    def __init__(self, retry_after: Optional[float] = None):
        super().__init__(429, "Rate limit reached")
        self.retry_after = retry_after  # seconds, parsed from Retry-After header


class QuotaExhaustedError(ApiError):
    """HTTP 429 with dailyLimitExceeded — daily quota is exhausted."""

    def __init__(self):
        super().__init__(429, "Daily upload quota exhausted", reason="dailyLimitExceeded")


class ServerError(ApiError):
    """HTTP 5xx — transient server-side error."""


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class NetworkError(UploaderError):
    """Connection timeout, DNS failure, or other network-level error."""


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

class FileReadError(UploaderError):
    """Could not read a local file (permissions, missing drive, etc.)."""


class FileTooLargeError(UploaderError):
    """File exceeds Google Photos' 200 MB per-item limit."""

    def __init__(self, path: str, size_bytes: int):
        super().__init__(f"{path}: {size_bytes / 1_048_576:.1f} MB exceeds the 200 MB limit")
        self.path = path
        self.size_bytes = size_bytes


class EmptyFileError(UploaderError):
    """File is 0 bytes and cannot be uploaded."""

    def __init__(self, path: str):
        super().__init__(f"{path}: file is empty (0 bytes)")
        self.path = path


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

class CompressionError(UploaderError):
    """Could not compress the image (corrupt data, unsupported format, etc.)."""


class FfmpegNotFoundError(UploaderError):
    """ffmpeg binary not found on PATH; required for video compression."""

    def __init__(self):
        super().__init__(
            "ffmpeg not found on PATH. Install it to enable video compression:\n"
            "  macOS:  brew install ffmpeg\n"
            "  Ubuntu: sudo apt install ffmpeg\n"
            "Or disable video compression with --no-compress-video."
        )


# ---------------------------------------------------------------------------
# State / database
# ---------------------------------------------------------------------------

class StateError(UploaderError):
    """Could not read or write the SQLite state database."""


class StateCorruptedError(StateError):
    """The state database failed integrity check and could not be recovered."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigError(UploaderError):
    """Invalid or missing configuration value."""


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

class DiskFullError(UploaderError):
    """Local disk is full; cannot write state or compressed temp files."""


# ---------------------------------------------------------------------------
# Human-readable message mapping
# ---------------------------------------------------------------------------

_HUMAN_MESSAGES: dict[type[UploaderError], str] = {
    RateLimitError:       "Request limit reached — waiting before retrying.",
    QuotaExhaustedError:  (
        "Daily upload quota exhausted. "
        "Your progress has been saved — re-run tomorrow and it will pick up where it left off."
    ),
    TokenRefreshError:    "Could not refresh your Google token. Run: gphotos auth login",
    AuthError:            "Authentication failed. Run: gphotos auth login",
    NetworkError:         "Network connection lost. Check your internet connection and try again.",
    FileTooLargeError:    "File exceeds Google Photos' 200 MB limit — skipped.",
    EmptyFileError:       "File is empty (0 bytes) — skipped.",
    FileReadError:        "File could not be read. It may have been moved, deleted, or corrupted.",
    CompressionError:     "Image could not be compressed — uploading original instead.",
    FfmpegNotFoundError:  (
        "ffmpeg is required for video compression but was not found on PATH. "
        "Install it (brew install ffmpeg / sudo apt install ffmpeg) "
        "or disable video compression with --no-compress-video."
    ),
    StateCorruptedError:  "Upload state database is corrupted. Run with --reset to start over.",
    DiskFullError:        "Local disk is full. Free up space and re-run — progress is preserved.",
    ConfigError:          "Configuration error.",
    ServerError:          "Google Photos returned a server error — will retry automatically.",
    ApiError:             "Google Photos API error.",
}


def human_message(exc: UploaderError) -> str:
    """Return a plain-language description of the error, suitable for terminal display."""
    # Walk the MRO to find the most specific match
    for cls in type(exc).__mro__:
        if cls in _HUMAN_MESSAGES:
            base = _HUMAN_MESSAGES[cls]
            detail = str(exc)
            # Avoid repeating the same text twice
            if detail and detail not in base:
                return f"{base}\n  Detail: {detail}"
            return base
    return f"Unexpected error: {exc}"
