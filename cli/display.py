"""All Rich terminal UI components.

The core library (uploader/) never imports this module — it stays in the CLI layer.
This module owns:
  - ScanDisplay: live spinner + file count during scan
  - UploadProgress: live progress bar during upload
  - show_pre_upload_summary: panel shown before upload starts
  - show_final_report: summary + compression + error tables shown at the end
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from uploader.models import FileRecord, FileStatus, RunStats


console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m:02d}m"


def _fmt_time_ago(dt_str: str) -> str:
    """Return 'X ago  (YYYY-MM-DD HH:MM)' from an ISO datetime string."""
    try:
        dt = datetime.datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        diff = now - dt
        total_seconds = int(diff.total_seconds())
        if total_seconds < 60:
            ago = "just now"
        elif total_seconds < 3600:
            m = total_seconds // 60
            ago = f"{m} minute{'s' if m != 1 else ''} ago"
        elif total_seconds < 86400:
            h = total_seconds // 3600
            ago = f"{h} hour{'s' if h != 1 else ''} ago"
        else:
            d = total_seconds // 86400
            ago = f"{d} day{'s' if d != 1 else ''} ago"
        formatted = dt.strftime("%Y-%m-%d %H:%M")
        return f"{ago}  ({formatted})"
    except (ValueError, TypeError):
        return dt_str or "unknown"


_EXIT_REASON_LABELS: dict[str, str] = {
    "completed": "Completed normally",
    "quota_exhausted": "Google Photos quota exhausted",
    "user_interrupted": "Interrupted by user (Ctrl+C)",
    "auth_error": "Authentication error",
    "disk_full": "Disk full",
    "unexpected_error": "Unexpected error",
    "ffmpeg_not_found": "ffmpeg not found",
}


# ---------------------------------------------------------------------------
# Scan display
# ---------------------------------------------------------------------------

class ScanDisplay:
    """Displays a spinner and running file count during directory scan."""

    def __init__(self):
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]Scanning...[/bold cyan] {task.description}"),
            console=console,
            transient=True,
        )
        self._task_id: Optional[TaskID] = None
        self._live: Optional[Live] = None

    def start(self) -> None:
        self._live = Live(self._progress, console=console, refresh_per_second=10)
        self._live.start()
        self._task_id = self._progress.add_task("", total=None)

    def update(self, count: int, path: str) -> None:
        if self._task_id is not None:
            self._progress.update(
                self._task_id,
                description=f"[green]{count:,}[/green] files found",
            )

    def stop(
        self,
        total: int,
        photos: int = 0,
        videos: int = 0,
        total_size_bytes: int = 0,
        estimated_skipped: int = 0,
    ) -> None:
        if self._live:
            self._live.stop()
        breakdown = ""
        if photos > 0 or videos > 0:
            breakdown = (
                f" ([cyan]{photos:,}[/cyan] photo{'s' if photos != 1 else ''}"
                f" · [magenta]{videos:,}[/magenta] video{'s' if videos != 1 else ''})"
            )
        size_str = f" · [dim]{_fmt_bytes(total_size_bytes)}[/dim]" if total_size_bytes else ""
        skip_str = (
            f" · [yellow]~{estimated_skipped:,} already uploaded (estimate)[/yellow]"
            if estimated_skipped
            else ""
        )
        console.print(
            f"[green]✓[/green] Scan complete — [bold]{total:,}[/bold] files found"
            f"{size_str}{skip_str}{breakdown}."
        )


# ---------------------------------------------------------------------------
# Upload progress
# ---------------------------------------------------------------------------

class UploadProgress:
    """Live progress bar showing upload stats including compression savings."""

    def __init__(self, total: int, compression_enabled: bool):
        self._total = total
        self._compression_enabled = compression_enabled

        columns = [
            SpinnerColumn(),
            TextColumn("[bold blue]Uploading[/bold blue]"),
            BarColumn(bar_width=30),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            MofNCompleteColumn(),
            TextColumn("[red]✗ {task.fields[errors]}[/red]"),
        ]

        if compression_enabled:
            columns.append(TextColumn("[green]Saved {task.fields[saved]}[/green]"))

        columns += [
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        ]

        self._progress = Progress(*columns, console=console)
        self._task_id: Optional[TaskID] = None
        self._live: Optional[Live] = None

    def start(self) -> None:
        self._live = Live(self._progress, console=console, refresh_per_second=4)
        self._live.start()
        self._task_id = self._progress.add_task(
            "",
            total=self._total,
            errors=0,
            saved="0 B (0%)",
        )

    def update(self, stats: RunStats) -> None:
        if self._task_id is None:
            return
        done = stats.uploaded + stats.skipped + stats.errors
        saved_str = ""
        if self._compression_enabled and stats.total_original_bytes > 0:
            pct = stats.compression_ratio * 100
            saved_str = f"{_fmt_bytes(stats.bytes_saved)} ({pct:.0f}%)"
        else:
            saved_str = "0 B (0%)"

        self._progress.update(
            self._task_id,
            completed=done,
            errors=stats.errors,
            saved=saved_str,
        )

    def stop(self) -> None:
        if self._live:
            self._live.stop()


# ---------------------------------------------------------------------------
# Pre-upload summary
# ---------------------------------------------------------------------------

def show_pre_upload_summary(stats: RunStats, workers: int) -> None:
    """Display the 'Found X photos' panel before asking for confirmation."""
    pending = stats.total - stats.skipped - stats.errors - stats.uploaded
    eta = stats.eta_seconds
    if eta is None and pending > 0 and workers > 0:
        # Rough estimate: assume ~5 files/min per worker as a baseline
        eta = (pending / (workers * 5)) * 60

    # Photo/video breakdown (only shown when both types are present)
    type_breakdown = ""
    if stats.total_photos > 0 and stats.total_videos > 0:
        type_breakdown = (
            f"  → [cyan]{stats.total_photos:,}[/cyan] photo{'s' if stats.total_photos != 1 else ''}"
            f" · [magenta]{stats.total_videos:,}[/magenta] video{'s' if stats.total_videos != 1 else ''}."
        )

    lines = [
        f"Found [bold]{stats.total:,}[/bold] files.",
    ]
    if type_breakdown:
        lines.append(type_breakdown)
    lines += [
        f"  → [yellow]{stats.skipped:,}[/yellow] already uploaded (will be skipped).",
        f"  → [bold green]{pending:,}[/bold green] files will be uploaded now.",
    ]
    if eta:
        h = int(eta // 3600)
        m = int((eta % 3600) // 60)
        if h:
            time_str = f"~{h}h{m:02d}min"
        else:
            time_str = f"~{m}min"
        lines.append(f"  → Estimated time: [cyan]{time_str}[/cyan] with {workers} workers.")

    content = "\n".join(lines)
    console.print(Panel(content, title="[bold]Upload Summary[/bold]", border_style="blue"))


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

def show_final_report(
    stats: RunStats,
    failed_files: list[FileRecord],
    *,
    interrupted: bool = False,
    compression_level_label: str = "",
) -> None:
    """Display the final summary panel and optional error table."""
    title = "[bold red]Upload Interrupted[/bold red]" if interrupted else "[bold green]Upload Complete[/bold green]"

    # Main stats table
    stats_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    stats_table.add_column("Total")
    stats_table.add_column("Uploaded")
    stats_table.add_column("Skipped")
    stats_table.add_column("Errors")
    stats_table.add_column("Duration")
    stats_table.add_column("Files/min")

    fpm = stats.files_per_minute
    stats_table.add_row(
        str(stats.total),
        f"[green]{stats.uploaded}[/green]",
        str(stats.skipped),
        f"[red]{stats.errors}[/red]" if stats.errors else "0",
        _fmt_duration(stats.elapsed_seconds),
        f"{fpm:.1f}",
    )

    console.print(Panel(stats_table, title=title, border_style="green" if not interrupted else "red"))

    # Photo/video breakdown (only shown when both types are present)
    if stats.total_photos > 0 and stats.total_videos > 0:
        type_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        type_table.add_column("Type")
        type_table.add_column("Uploaded", justify="right")
        type_table.add_column("Skipped", justify="right")
        type_table.add_column("Errors", justify="right")
        type_table.add_row(
            "[cyan]Photos[/cyan]",
            f"[green]{stats.uploaded_photos}[/green]",
            str(stats.skipped_photos),
            f"[red]{stats.errors_photos}[/red]" if stats.errors_photos else "0",
        )
        type_table.add_row(
            "[magenta]Videos[/magenta]",
            f"[green]{stats.uploaded_videos}[/green]",
            str(stats.skipped_videos),
            f"[red]{stats.errors_videos}[/red]" if stats.errors_videos else "0",
        )
        console.print(Panel(type_table, title="[bold]By Type[/bold]", border_style="dim"))

    # Compression summary
    if stats.total_original_bytes > 0:
        pct = stats.compression_ratio * 100
        level_note = f" (level: {compression_level_label})" if compression_level_label else ""
        comp_table = Table(show_header=False, box=None, padding=(0, 2))
        comp_table.add_column(style="bold cyan")
        comp_table.add_column()
        comp_table.add_row("Original:", _fmt_bytes(stats.total_original_bytes))
        comp_table.add_row("Uploaded:", _fmt_bytes(stats.total_compressed_bytes))
        comp_table.add_row(
            "Saved:",
            f"[bold green]{_fmt_bytes(stats.bytes_saved)}[/bold green] "
            f"([bold]{pct:.0f}%[/bold]){level_note}",
        )
        console.print(Panel(comp_table, title="[bold]Compression Summary[/bold]", border_style="cyan"))

    # Error table
    if failed_files:
        err_table = Table(
            title=f"[bold red]Failed Files ({len(failed_files)})[/bold red]",
            show_header=True,
            header_style="bold red",
        )
        err_table.add_column("Path", style="dim", overflow="fold")
        err_table.add_column("Attempts", justify="right")
        err_table.add_column("Error")

        # Sort by error message to group similar errors
        for rec in sorted(failed_files, key=lambda r: r.error_msg or ""):
            err_table.add_row(
                rec.path,
                str(rec.attempts),
                rec.error_msg or "Unknown error",
            )
        console.print(err_table)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def print_credentials_setup_panel(creds_path: Path) -> None:
    """Print a Rich panel with step-by-step Google Cloud setup instructions."""
    content = (
        "You need a Google Cloud OAuth credential to use this tool.\n\n"
        "[bold]1.[/bold] Go to [bold]https://console.cloud.google.com[/bold]\n"
        "[bold]2.[/bold] Create a new project (or select an existing one)\n"
        "[bold]3.[/bold] Enable the [bold]Google Photos Library API[/bold]\n"
        "   APIs & Services → Library → search [italic]Photos Library API[/italic]\n"
        "[bold]4.[/bold] Create OAuth credentials\n"
        "   APIs & Services → Credentials → Create → OAuth client ID\n"
        "   Application type: [bold]Desktop app[/bold]\n"
        f"[bold]5.[/bold] Download the JSON and save it to:\n"
        f"   [bold cyan]{creds_path}[/bold cyan]\n\n"
        "Then re-run: [bold]gphotos init[/bold]"
    )
    console.print(
        Panel(
            content,
            title="[bold yellow]Google Cloud Setup Required[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def ask_continue_previous_run(stats: RunStats, last_run: Optional[dict]) -> bool:
    """Ask the user whether to continue a previous run, showing a summary panel."""
    # --- Build info lines (timing + cause) ---
    info_lines: list[str] = []

    if last_run:
        dt_str = last_run.get("finished_at") or last_run.get("started_at")
        if dt_str:
            info_lines.append(f"Last activity   [dim]{_fmt_time_ago(dt_str)}[/dim]")
        reason_key = last_run.get("exit_reason")
        reason_label = _EXIT_REASON_LABELS.get(reason_key or "", "Unknown (session may have crashed)")
        color = "red" if reason_key not in ("completed", None) else "dim"
        info_lines.append(f"Stopped         [{color}]{reason_label}[/{color}]")
    else:
        info_lines.append("[dim]No run history recorded.[/dim]")

    # --- Build stats table ---
    stats_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    stats_table.add_column("Uploaded")
    stats_table.add_column("Skipped")
    stats_table.add_column("Errors")
    stats_table.add_column("Remaining")
    stats_table.add_row(
        f"[green]{stats.uploaded:,}[/green]",
        f"{stats.skipped:,}",
        f"[red]{stats.errors:,}[/red]" if stats.errors else "0",
        f"[yellow]{stats.remaining:,}[/yellow]",
    )

    # --- Compose panel content ---
    from rich.console import Group
    from rich.text import Text as RichText

    info_text = RichText.from_markup("\n".join(info_lines))
    spacer = RichText("")
    panel_content = Group(info_text, spacer, stats_table)

    console.print(Panel(
        panel_content,
        title="[bold yellow]Previous Session Found[/bold yellow]",
        border_style="yellow",
        padding=(0, 1),
    ))
    response = console.input("Continue from where you left off? [Y/n] ").strip().lower()
    return response in ("", "y", "yes")


def ask_confirm_upload() -> bool:
    """Ask the user to confirm before starting the upload."""
    response = console.input("\nContinue? [Y/n] ").strip().lower()
    return response in ("", "y", "yes")
