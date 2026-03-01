"""`gphotos upload <source_dir>` command."""
from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from uploader.config import load as load_config
from uploader.errors import (
    AuthError,
    ConfigError,
    DiskFullError,
    QuotaExhaustedError,
    StateCorruptedError,
    human_message,
)
from uploader.models import CompressionLevel, FileStatus, RunStats
from uploader.notifier import notify_complete
from uploader.orchestrator import run_upload, scan_and_register
from uploader.state import StateStore

def upload(
    source: Annotated[Path, typer.Argument(help="Directory containing photos to upload")],
    config_file: Annotated[Optional[Path], typer.Option("--config", "-c", help="Path to uploader.toml")] = None,
    workers: Annotated[Optional[int], typer.Option("--workers", "-w", help="Parallel upload threads")] = None,
    no_compress: Annotated[bool, typer.Option("--no-compress", help="Skip image compression")] = False,
    compression_level: Annotated[Optional[str], typer.Option("--compression-level", help="Compression level: low | mid | high")] = None,
    max_retries: Annotated[Optional[int], typer.Option("--max-retries", help="Retries per file")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
    reset: Annotated[bool, typer.Option("--reset", help="Ignore saved state and start fresh")] = False,
    credentials: Annotated[Optional[Path], typer.Option("--credentials", help="Path to client_secret.json")] = None,
    token: Annotated[Optional[Path], typer.Option("--token", help="Path to token.json")] = None,
    state_db: Annotated[Optional[Path], typer.Option("--state-db", help="Path to SQLite state file")] = None,
    album: Annotated[Optional[str], typer.Option("--album", help="Add uploaded photos to this Google Photos album (created if it doesn't exist)")] = None,
    albums_from_dirs: Annotated[bool, typer.Option("--albums-from-dirs", help="Create one Google Photos album per subdirectory (mutually exclusive with --album)")] = False,
    album_prefix: Annotated[Optional[str], typer.Option("--album-prefix", help="Prefix to prepend to every album name created by --albums-from-dirs")] = None,
) -> None:
    """Bulk upload photos from SOURCE to Google Photos."""
    from cli.display import (
        ScanDisplay,
        UploadProgress,
        ask_confirm_upload,
        ask_continue_previous_run,
        console,
        show_final_report,
        show_pre_upload_summary,
    )

    # --- 1. Validate source directory ---
    if album and albums_from_dirs:
        console.print("[red]Error:[/red] --album and --albums-from-dirs are mutually exclusive")
        raise typer.Exit(1)

    if not source.exists():
        console.print(f"[red]Error:[/red] Directory not found: {source}")
        raise typer.Exit(1)
    if not source.is_dir():
        console.print(f"[red]Error:[/red] Not a directory: {source}")
        raise typer.Exit(1)

    # --- 2. Load configuration ---
    try:
        config = load_config(
            config_file,
            workers=workers,
            compress=False if no_compress else None,
            compression_level=compression_level,
            max_retries=max_retries,
            credentials_path=credentials,
            token_path=token,
            state_db_path=state_db,
            album=album,
            album_per_dir=albums_from_dirs,
            album_prefix=album_prefix,
        )
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)

    # --- 3. Open state store ---
    try:
        state = StateStore(config.state_db_path)
    except StateCorruptedError as e:
        console.print(f"[red]State database error:[/red] {e}")
        if typer.confirm("Reset state and start over?"):
            config.state_db_path.unlink(missing_ok=True)
            state = StateStore(config.state_db_path)
        else:
            raise typer.Exit(1)

    # --- 4. Handle previous state ---
    if state.has_existing_state() and not reset:
        if yes or ask_continue_previous_run():
            reverted = state.revert_in_progress_to_pending()
            if reverted:
                console.print(f"[yellow]Reverted {reverted} in-progress file(s) to pending.[/yellow]")
        else:
            reset = True

    if reset:
        state.clear_all()
        console.print("[dim]Previous state cleared. Starting fresh.[/dim]")

    # --- 5. Scan phase ---
    console.print()
    scan_display = ScanDisplay()
    scan_display.start()

    try:
        total_found = scan_and_register(
            source, config, state,
            on_file_scanned=scan_display.update,
        )
    except PermissionError as e:
        scan_display.stop(0)
        console.print(f"[red]Permission error during scan:[/red] {e}")
        raise typer.Exit(1)

    scan_display.stop(total_found)

    if total_found == 0:
        console.print("[yellow]No supported media files found in the specified directory.[/yellow]")
        state.close()
        raise typer.Exit(0)

    # --- 6. Pre-upload summary ---
    initial_stats = state.get_stats()
    show_pre_upload_summary(initial_stats, config.workers)

    pending_count = len(state.get_pending_files())
    if pending_count == 0:
        console.print("[green]All files are already uploaded.[/green]")
        state.close()
        raise typer.Exit(0)

    if not yes and not ask_confirm_upload():
        console.print("[dim]Upload cancelled.[/dim]")
        state.close()
        raise typer.Exit(0)

    # --- 7. Set up graceful shutdown ---
    shutdown_event = threading.Event()
    first_sigint_time: list[float] = [0.0]

    def _sigint_handler(sig, frame):
        now = time.monotonic()
        if shutdown_event.is_set() and (now - first_sigint_time[0]) < 3.0:
            console.print("\n[red]Forced exit.[/red]")
            state.close()
            sys.exit(1)
        first_sigint_time[0] = now
        shutdown_event.set()
        console.print("\n[yellow]Shutting down safely... please wait.[/yellow]")

    signal.signal(signal.SIGINT, _sigint_handler)

    # --- 8. Upload ---
    progress_display = UploadProgress(
        total=pending_count,
        compression_enabled=config.compress,
    )
    progress_display.start()

    start_time = __import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc)
    stats_container: list[RunStats] = []

    def on_progress(stats: RunStats) -> None:
        stats.start_time = start_time  # type: ignore[assignment]
        stats_container.append(stats)
        progress_display.update(stats)

    interrupted = False
    try:
        final_stats = run_upload(config, state, on_progress, shutdown_event, source_dir=source)
        final_stats.start_time = start_time
    except QuotaExhaustedError as e:
        interrupted = True
        console.print(f"\n[red bold]Quota exhausted:[/red bold] {human_message(e)}")
    except AuthError as e:
        interrupted = True
        console.print(f"\n[red]Authentication error:[/red] {human_message(e)}")
    except DiskFullError as e:
        interrupted = True
        console.print(f"\n[red]Disk full:[/red] {human_message(e)}")
    except Exception as e:
        interrupted = True
        console.print(f"\n[red]Unexpected error:[/red] {e}")
    finally:
        progress_display.stop()
        if shutdown_event.is_set():
            interrupted = True
            state.revert_in_progress_to_pending()

    # --- 9. Final report ---
    console.print()
    final_stats = state.get_stats()
    final_stats.start_time = start_time
    failed_files = state.get_files_by_status(FileStatus.ERROR)

    level_label = f"{config.compression_level.value} (q={config.compression_level.jpeg_quality()})"

    show_final_report(
        final_stats,
        failed_files,
        interrupted=interrupted,
        compression_level_label=level_label if config.compress else "disabled",
    )

    # --- 10. Notify ---
    if config.notify_on_complete:
        notify_complete(final_stats, beep=config.beep_on_complete, notify=True)

    state.close()

    if interrupted:
        raise typer.Exit(1)
    if final_stats.errors > 0:
        raise typer.Exit(2)
