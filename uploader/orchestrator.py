"""Upload orchestrator: manages the thread pool and the batchCreate loop.

Architecture:
  - ThreadPoolExecutor runs N worker threads (parallel byte uploads)
  - A queue.Queue passes upload tokens from workers to the main thread
  - The main thread drains the queue in batches and calls batchCreate (serialized)
  - A threading.Event signals graceful shutdown (from Ctrl+C or quota exhaustion)

The batchCreate loop runs on the calling thread. It exits when:
  1. All submitted futures have completed, AND
  2. The token queue is empty.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from uploader.api_client import GooglePhotosClient
from uploader.auth import get_credentials
from uploader.compressor import check_ffmpeg
from uploader.config import AppConfig
from uploader.errors import (
    ConflictError,
    FfmpegNotFoundError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
    human_message,
)
from uploader.models import BatchCreateResult, FileRecord, FileStatus, RunStats, UploadToken
from uploader.retry import with_retry
from uploader.state import StateStore
from uploader.worker import upload_file


# Errors retried on batchCreate
_BATCH_RETRYABLE = (NetworkError, RateLimitError, ServerError, ConflictError)
_BATCH_NON_RETRYABLE = (QuotaExhaustedError,)

# How long (seconds) to wait for a new token before checking if workers are done
_QUEUE_DRAIN_TIMEOUT = 1.0


def run_upload(
    config: AppConfig,
    state: StateStore,
    on_progress: Callable[[RunStats], None],
    shutdown_event: threading.Event,
    source_dir: Optional[Path] = None,
) -> RunStats:
    """Execute the full upload run.

    Args:
        config: Fully-resolved application configuration.
        state: StateStore instance (already initialized; in-progress rows reverted).
        on_progress: Callback invoked after each batchCreate batch completes.
            Receives the latest RunStats.
        shutdown_event: Set externally (e.g. by SIGINT handler) to trigger
            graceful shutdown. Also set internally on quota exhaustion.

    Returns:
        Final RunStats after all workers have finished.
    """
    # Check for ffmpeg if video compression is enabled
    if config.compress_video:
        check_ffmpeg()

    # Authenticate
    creds = get_credentials(config.credentials_path, config.token_path)
    client = GooglePhotosClient(creds, timeout=120.0)

    # Resolve album(s)
    album_id: Optional[str] = None          # used for --album (global)
    dir_album_map: dict[str, str] = {}      # used for --albums-from-dirs

    # Load pending files (filtered by media-type when set)
    pending = state.get_pending_files(include_extensions=config.include_extensions)
    if not pending:
        return state.get_stats(include_extensions=config.include_extensions)

    if config.album:
        # Single global album (existing behaviour)
        album_id = state.get_meta("album_id")
        if not album_id:
            album_id = with_retry(
                lambda: client.get_or_create_album(config.album),
                max_attempts=config.max_retries,
                base_delay=config.retry_base_delay,
                retryable=_BATCH_RETRYABLE,
                non_retryable=_BATCH_NON_RETRYABLE,
            )
            state.set_meta("album_id", album_id)

    elif config.album_per_dir and source_dir is not None:
        # Process one directory at a time: create album → upload its files → next.
        # This naturally spaces album-creation API calls apart, avoiding 429s.
        logger = logging.getLogger(__name__)

        # Group pending files by parent directory name
        dir_groups: dict[str, list[FileRecord]] = defaultdict(list)
        root_files: list[FileRecord] = []
        for record in pending:
            parent = Path(record.path).parent
            try:
                rel = parent.relative_to(source_dir)
            except ValueError:
                root_files.append(record)
                continue
            if str(rel) == ".":
                root_files.append(record)
            else:
                dir_groups[parent.name].append(record)

        sorted_dirs = sorted(dir_groups.keys())
        token_queue: queue.Queue[UploadToken] = queue.Queue()
        progress_event = threading.Event()

        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            for i, dir_name in enumerate(sorted_dirs, 1):
                if shutdown_event.is_set():
                    break

                # Create/get album for this directory
                album_name = f"{config.album_prefix} {dir_name}" if config.album_prefix else dir_name
                meta_key = f"album_id_dir:{dir_name}"
                dir_album_id = state.get_meta(meta_key)
                if not dir_album_id:
                    logger.debug("Creating album %d/%d '%s'", i, len(sorted_dirs), album_name)
                    dir_album_id = with_retry(
                        lambda name=album_name: client.get_or_create_album(name),
                        max_attempts=config.max_retries,
                        base_delay=config.retry_base_delay,
                        retryable=_BATCH_RETRYABLE,
                        non_retryable=_BATCH_NON_RETRYABLE,
                    )
                    state.set_meta(meta_key, dir_album_id)
                else:
                    logger.debug("Album %d/%d '%s' already cached", i, len(sorted_dirs), album_name)

                # Submit this directory's files and drain
                futures: list[Future] = []
                for record in dir_groups[dir_name]:
                    if shutdown_event.is_set():
                        break
                    futures.append(executor.submit(
                        upload_file,
                        record, config, client, state,
                        token_queue, shutdown_event, progress_event,
                        album_id=dir_album_id,
                    ))

                _batch_create_loop(
                    futures=futures,
                    token_queue=token_queue,
                    client=client,
                    state=state,
                    config=config,
                    on_progress=on_progress,
                    shutdown_event=shutdown_event,
                    progress_event=progress_event,
                )

            # Upload root-level files (no album)
            if root_files and not shutdown_event.is_set():
                futures = []
                for record in root_files:
                    if shutdown_event.is_set():
                        break
                    futures.append(executor.submit(
                        upload_file,
                        record, config, client, state,
                        token_queue, shutdown_event, progress_event,
                        album_id=None,
                    ))

                _batch_create_loop(
                    futures=futures,
                    token_queue=token_queue,
                    client=client,
                    state=state,
                    config=config,
                    on_progress=on_progress,
                    shutdown_event=shutdown_event,
                    progress_event=progress_event,
                )

        return state.get_stats(include_extensions=config.include_extensions)

    token_queue: queue.Queue[UploadToken] = queue.Queue()
    progress_event = threading.Event()
    futures: list[Future] = []

    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        for record in pending:
            if shutdown_event.is_set():
                break
            future = executor.submit(
                upload_file,
                record, config, client, state,
                token_queue, shutdown_event, progress_event,
                album_id=album_id,
            )
            futures.append(future)

        _batch_create_loop(
            futures=futures,
            token_queue=token_queue,
            client=client,
            state=state,
            config=config,
            on_progress=on_progress,
            shutdown_event=shutdown_event,
            progress_event=progress_event,
        )

    return state.get_stats(include_extensions=config.include_extensions)


# ---------------------------------------------------------------------------
# batchCreate loop
# ---------------------------------------------------------------------------

def _batch_create_loop(
    *,
    futures: list[Future],
    token_queue: "queue.Queue[UploadToken]",
    client: GooglePhotosClient,
    state: StateStore,
    config: AppConfig,
    on_progress: Callable[[RunStats], None],
    shutdown_event: threading.Event,
    progress_event: threading.Event,
) -> None:
    """Drain token_queue in batches and call batchCreate until all workers finish."""
    active_futures = set(futures)

    while active_futures or not token_queue.empty():
        # Remove completed futures
        done = {f for f in active_futures if f.done()}
        active_futures -= done

        # Collect a batch of tokens (up to batch_size, with timeout drain)
        batch: list[UploadToken] = []
        deadline = time.monotonic() + _QUEUE_DRAIN_TIMEOUT

        while len(batch) < config.batch_size and time.monotonic() < deadline:
            try:
                token = token_queue.get(timeout=0.1)
                batch.append(token)
            except queue.Empty:
                if not active_futures:
                    break   # no more workers → flush whatever we have

        if not batch:
            # Progress event may have fired without new tokens (e.g. SKIPPED/ERROR)
            if progress_event.is_set():
                progress_event.clear()
                on_progress(state.get_stats(include_extensions=config.include_extensions))
            continue

        if shutdown_event.is_set():
            # Still process the batch — we have valid upload tokens, don't waste them
            pass

        # Group batch by album_id so each album gets its own batchCreate call
        by_album: dict[Optional[str], list[UploadToken]] = defaultdict(list)
        for token in batch:
            by_album[token.album_id].append(token)

        all_results: list[BatchCreateResult] = []
        quota_error = False

        for aid, tokens_for_album in by_album.items():
            try:
                results = with_retry(
                    lambda b=tokens_for_album, a=aid: client.batch_create(b, album_id=a),
                    max_attempts=config.max_retries,
                    base_delay=config.retry_base_delay,
                    retryable=_BATCH_RETRYABLE,
                    non_retryable=_BATCH_NON_RETRYABLE,
                )
                all_results.extend(results)
            except QuotaExhaustedError as exc:
                shutdown_event.set()
                for token in tokens_for_album:
                    state.set_status(
                        token.file_id, FileStatus.ERROR,
                        error_msg=human_message(exc),
                    )
                quota_error = True
                break
            except Exception as exc:
                for token in tokens_for_album:
                    state.set_status(
                        token.file_id, FileStatus.ERROR,
                        error_msg=f"batchCreate failed: {exc}",
                    )

        if quota_error:
            on_progress(state.get_stats(include_extensions=config.include_extensions))
            return

        # Update state for each result
        for result in all_results:
            if result.success:
                state.set_status(
                    result.file_id, FileStatus.UPLOADED,
                    google_media_id=result.google_media_id,
                )
            else:
                state.set_status(
                    result.file_id, FileStatus.ERROR,
                    error_msg=result.error_msg or "Unknown batchCreate error",
                )

        progress_event.clear()
        on_progress(state.get_stats(include_extensions=config.include_extensions))


# ---------------------------------------------------------------------------
# Scan phase
# ---------------------------------------------------------------------------

def scan_and_register(
    source_dir: "Path",
    config: AppConfig,
    state: StateStore,
    on_file_scanned: Callable[[int, str], None],
) -> tuple[int, int, int, int, int]:
    """Scan source directory and register files in state without hashing.

    Hashing is deferred to upload time so that uploads can begin immediately.
    Files already uploaded in a previous run are detected by path (fast DB
    lookup) and counted as estimated skipped.

    Handles:
      - 0-byte files → SKIPPED
      - >200 MB files → SKIPPED
      - Already uploaded by path → counted as estimated skipped (not re-queued)
      - Everything else → PENDING (hash computed later by the worker)

    Args:
        source_dir: Root directory to scan.
        config: Application configuration.
        state: State store to write records into.
        on_file_scanned: Callback after each file is processed:
            ``on_file_scanned(total_found_so_far, display_path)``.

    Returns:
        Tuple of (total_files, photo_count, video_count,
                  estimated_skipped, total_size_bytes).
        ``estimated_skipped`` is based on path matching and may differ from
        the exact count shown in the final report.
    """
    from pathlib import Path as _Path

    from uploader.models import MAX_FILE_SIZE_BYTES
    from uploader.scanner import scan_directory

    found_count = 0

    def _on_found(count: int, path: "Path") -> None:
        nonlocal found_count
        found_count = count
        on_file_scanned(count, str(path))

    scan_result = scan_directory(
        source_dir,
        include_extensions=config.include_extensions,
        follow_symlinks=config.follow_symlinks,
        on_file_found=_on_found,
    )

    estimated_skipped = 0
    total_size_bytes = 0

    for path in scan_result.paths:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        if size == 0:
            state.upsert_file(FileRecord(
                path=str(path),
                status=FileStatus.SKIPPED,
                error_msg="Empty file (0 bytes)",
                original_size_bytes=0,
                compressed_size_bytes=0,
            ))
            continue

        if size > MAX_FILE_SIZE_BYTES:
            state.upsert_file(FileRecord(
                path=str(path),
                status=FileStatus.SKIPPED,
                error_msg=f"File too large ({size / 1_048_576:.1f} MB, limit 200 MB)",
                original_size_bytes=size,
                compressed_size_bytes=size,
            ))
            continue

        # Fast path-based check: preserve already-uploaded records and skip re-queuing
        existing = state.get_file_by_path(str(path))
        if existing and existing.status == FileStatus.UPLOADED:
            estimated_skipped += 1
            total_size_bytes += size
            continue  # Don't overwrite the uploaded record

        if existing and existing.status == FileStatus.SKIPPED:
            # Already known-skipped (e.g. dup from previous run) — leave it
            continue

        total_size_bytes += size
        # Register as PENDING without hash; worker computes hash at upload time
        state.upsert_file(FileRecord(
            path=str(path),
            status=FileStatus.PENDING,
            content_hash=None,
            original_size_bytes=size,
        ))

    return (
        len(scan_result.paths),
        scan_result.photo_count,
        scan_result.video_count,
        estimated_skipped,
        total_size_bytes,
    )
