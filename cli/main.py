"""CLI entry point.

Registered as the `gphotos` script in pyproject.toml.

Commands:
  gphotos upload <dir>    Bulk upload photos to Google Photos
  gphotos auth login      Authenticate with Google OAuth
  gphotos auth status     Show token validity
"""
from __future__ import annotations

import typer

from cli.commands.auth import auth_app
from cli.commands.upload import upload

app = typer.Typer(
    name="gphotos",
    help=(
        "Bulk upload photos to Google Photos.\n\n"
        "Quick start:\n\n"
        "  1. gphotos auth login          # authenticate once\n"
        "  2. gphotos upload /my/photos   # start uploading\n"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Register upload as a direct top-level command (not nested in a sub-typer)
app.command("upload")(upload)

# Register auth subcommands
app.add_typer(auth_app, name="auth")


if __name__ == "__main__":
    app()
