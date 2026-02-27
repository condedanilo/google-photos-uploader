"""OS-level completion notification and terminal beep.

Cross-platform strategy:
  - macOS/Linux/Windows: plyer (best effort; silently ignored on failure)
  - Beep: ASCII bell character \a (works in all terminals)

Failures in this module are always swallowed — notification is a nice-to-have
and must never interrupt the main upload flow.
"""
from __future__ import annotations

from uploader.models import RunStats


def notify_complete(stats: RunStats, *, beep: bool = True, notify: bool = True) -> None:
    """Emit an OS desktop notification and/or terminal beep on completion.

    Args:
        stats: Final run statistics to include in the notification body.
        beep: If True, print an ASCII bell character.
        notify: If True, attempt a desktop OS notification.
    """
    if beep:
        _beep()
    if notify:
        _os_notify(stats)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _beep() -> None:
    try:
        print("\a", end="", flush=True)
    except Exception:
        pass


def _os_notify(stats: RunStats) -> None:
    title = "Google Photos Upload Complete"
    duration = _format_duration(stats.elapsed_seconds)
    savings = _format_bytes(stats.bytes_saved)
    body = (
        f"Uploaded {stats.uploaded:,} files in {duration}. "
        f"Errors: {stats.errors}. "
        + (f"Saved {savings}." if stats.bytes_saved > 0 else "")
    ).strip()

    try:
        from plyer import notification  # type: ignore
        notification.notify(
            title=title,
            message=body,
            app_name="gphotos-uploader",
            timeout=10,
        )
        return
    except Exception:
        pass

    # Fallback: macOS osascript
    try:
        import subprocess, shutil
        if shutil.which("osascript"):
            safe_body = body.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_body}" with title "{title}"'],
                check=False, capture_output=True, timeout=5,
            )
            return
    except Exception:
        pass

    # Fallback: Linux notify-send
    try:
        import subprocess, shutil
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, body],
                check=False, capture_output=True, timeout=5,
            )
    except Exception:
        pass


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"
