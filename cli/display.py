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

    def stop(self, total: int) -> None:
        if self._live:
            self._live.stop()
        console.print(f"[green]✓[/green] Scan complete — [bold]{total:,}[/bold] files found.")


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

    lines = [
        f"Found [bold]{stats.total:,}[/bold] files.",
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


def ask_continue_previous_run() -> bool:
    """Ask the user whether to continue a previous run."""
    console.print(
        "[yellow]A previous upload session was found.[/yellow]"
    )
    response = console.input("Continue from where you left off? [Y/n] ").strip().lower()
    return response in ("", "y", "yes")


def ask_confirm_upload() -> bool:
    """Ask the user to confirm before starting the upload."""
    response = console.input("\nContinue? [Y/n] ").strip().lower()
    return response in ("", "y", "yes")
