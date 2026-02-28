"""CLI entry point.

Registered as the `gphotos` script in pyproject.toml.

Commands:
  gphotos init            Guided first-run setup
  gphotos upload <dir>    Bulk upload photos to Google Photos
  gphotos auth login      Authenticate with Google OAuth
  gphotos auth status     Show token validity
"""
from __future__ import annotations

import typer

from cli.commands.auth import auth_app
from cli.commands.init import init
from cli.commands.upload import upload

app = typer.Typer(
    name="gphotos",
    help=(
        "Bulk upload photos to Google Photos.\n\n"
        "New user? Run:\n\n"
        "  gphotos init                  # guided first-run setup\n\n"
        "Then:\n\n"
        "  gphotos upload /my/photos     # start uploading\n"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Register top-level commands
app.command("init")(init)
app.command("upload")(upload)

# Register auth subcommands
app.add_typer(auth_app, name="auth")


if __name__ == "__main__":
    app()
