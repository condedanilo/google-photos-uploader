"""Per-file upload pipeline executed inside ThreadPoolExecutor worker threads.

Pipeline for each file:
  1. Check shutdown_event — bail early if set
  2. Set status to IN_PROGRESS
  3. Validate file (readable, size within limits)
  4. Compute content hash (skip if already stored)
  5. Check hash against state — mark SKIPPED if already UPLOADED
  6. Compress (if enabled and compressible type)
  7. Upload bytes via API client (with retry)
  8. Put UploadToken onto token_queue for the batchCreate loop
  9. Record size information in state
 10. Clean up compressed temp file

On any unrecoverable error: set status=ERROR with a human-readable message.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Optional

from uploader.api_client import GooglePhotosClient
from uploader.compressor import cleanup_temp, compress, compress_video
from uploader.config import AppConfig
from uploader.errors import (
    CompressionError,
    EmptyFileError,
    FileReadError,
    FileTooLargeError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
    UploaderError,
    human_message,
)
from uploader.hasher import hash_file
from uploader.models import (
    COMPRESSIBLE_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    FileRecord,
    FileStatus,
    UploadToken,
    is_video,
)
from uploader.retry import with_retry
from uploader.state import StateStore


# Errors that should trigger a retry
_RETRYABLE = (NetworkError, RateLimitError, ServerError)

# Errors that stop the whole run (handled by orchestrator)
_FATAL = (QuotaExhaustedError,)


def upload_file(
    record: FileRecord,
    config: AppConfig,
    client: GooglePhotosClient,
    state: StateStore,
    token_queue: "queue.Queue[UploadToken]",
    shutdown_event: threading.Event,
    progress_event: threading.Event,
    album_id: Optional[str] = None,
) -> None:
    """Execute the full upload pipeline for a single file.

    This function is designed to run inside a ThreadPoolExecutor worker thread.
    It never raises — all errors are caught and recorded in *state*.

    Args:
        record: The FileRecord to upload (must have id set).
        config: Application configuration.
        client: Google Photos API client.
        state: State store for status updates.
        token_queue: Queue where upload tokens are placed for the batchCreate loop.
        shutdown_event: Set by the orchestrator on graceful shutdown signal.
        progress_event: Set after each state update to wake the display refresh.
    """
    assert record.id is not None, "FileRecord must have a database id"
    path = Path(record.path)

    # --- Step 1: Check for shutdown ---
    if shutdown_event.is_set():
        return

    # --- Step 2: Mark IN_PROGRESS ---
    state.set_status(record.id, FileStatus.IN_PROGRESS, attempts_increment=1)

    compression_result = None  # initialized here so finally block can always reference it

    try:
        # --- Step 3: Validate file ---
        _validate_file(path)

        # --- Step 4: Compute hash ---
        content_hash = record.content_hash
        if not content_hash:
            content_hash = hash_file(path)

        # --- Step 5: Deduplication check ---
        existing = state.get_file_by_hash(content_hash)
        if existing and existing.id != record.id:
            state.set_status(
                record.id, FileStatus.SKIPPED,
                error_msg=f"Duplicate of already-uploaded file: {existing.path}",
                original_size_bytes=path.stat().st_size,
                compressed_size_bytes=path.stat().st_size,
            )
            progress_event.set()
            return

        # Update the hash in state so it's available on restart
        state.upsert_file(FileRecord(
            id=record.id,
            path=record.path,
            status=FileStatus.IN_PROGRESS,
            content_hash=content_hash,
            attempts=record.attempts + 1,
        ))

        # --- Step 6: Compress ---
        compression_result = None
        if config.compress and path.suffix.lower() in COMPRESSIBLE_EXTENSIONS:
            # Image compression
            try:
                compression_result = compress(
                    path,
                    config.compression_level,
                    skip_if_larger=config.skip_if_larger,
                )
            except CompressionError:
                # Compression failure is non-fatal — upload the original
                compression_result = None
        elif config.compress_video and is_video(path):
            # Video compression (transcode to H.264/AAC via ffmpeg)
            try:
                compression_result = compress_video(
                    path,
                    max_height=config.video_max_height,
                    crf=config.video_crf,
                    preset=config.video_preset,
                    audio_bitrate=config.video_audio_bitrate,
                    skip_if_larger=config.skip_if_larger,
                )
            except Exception:
                # Transcoding failure is non-fatal — upload the original
                compression_result = None

        upload_path = Path(compression_result.output_path) if compression_result else path
        original_size = path.stat().st_size
        compressed_size = (
            compression_result.output_bytes if compression_result else original_size
        )

        # --- Step 7: Upload bytes (with retry) ---
        if shutdown_event.is_set():
            return

        def do_upload() -> str:
            return client.upload_bytes(upload_path)

        upload_token = with_retry(
            do_upload,
            max_attempts=config.max_retries,
            base_delay=config.retry_base_delay,
            retryable=_RETRYABLE,
            non_retryable=_FATAL,
        )

        # --- Step 8: Enqueue token ---
        token_queue.put(UploadToken(
            file_id=record.id,
            token=upload_token,
            original_path=record.path,
            album_id=album_id,
        ))

        # --- Step 9: Record sizes (status stays IN_PROGRESS until batchCreate confirms) ---
        state.set_status(
            record.id, FileStatus.IN_PROGRESS,
            upload_token=upload_token,
            original_size_bytes=original_size,
            compressed_size_bytes=compressed_size,
        )

    except tuple(_FATAL) as exc:
        # Quota exhaustion — signal the orchestrator to stop the run
        shutdown_event.set()
        state.set_status(
            record.id, FileStatus.ERROR,
            error_msg=human_message(exc),
        )

    except UploaderError as exc:
        state.set_status(
            record.id, FileStatus.ERROR,
            error_msg=human_message(exc),
        )

    except Exception as exc:
        state.set_status(
            record.id, FileStatus.ERROR,
            error_msg=f"Unexpected error: {exc}",
        )

    finally:
        # --- Step 10: Cleanup temp file ---
        if compression_result is not None:
            cleanup_temp(compression_result)
        progress_event.set()


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

def _validate_file(path: Path) -> None:
    """Raise a domain error if the file cannot be uploaded."""
    if not path.exists():
        raise FileReadError(f"File not found: {path}")

    try:
        size = path.stat().st_size
    except OSError as e:
        raise FileReadError(f"Cannot stat {path}: {e}") from e

    if size == 0:
        raise EmptyFileError(str(path))

    if size > MAX_FILE_SIZE_BYTES:
        raise FileTooLargeError(str(path), size)
