"""`gphotos auth` subcommands."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

auth_app = typer.Typer(help="Manage Google Photos authentication.")


@auth_app.command("login")
def login(
    credentials: Annotated[
        Optional[Path],
        typer.Option("--credentials", help="Path to client_secret.json"),
    ] = None,
    token_out: Annotated[
        Optional[Path],
        typer.Option("--token-out", help="Where to save the token"),
    ] = None,
) -> None:
    """Force a fresh OAuth flow and save the new token."""
    from cli.display import console
    from uploader.auth import get_credentials
    from uploader.config import load as load_config
    from uploader.errors import AuthError, ConfigError

    try:
        config = load_config(
            credentials_path=credentials,
            token_path=token_out,
        )
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)

    creds_path = credentials or config.credentials_path
    tok_path = token_out or config.token_path

    if not creds_path.exists():
        console.print(
            f"[red]Error:[/red] Credentials file not found: {creds_path}\n"
            "Download client_secret.json from Google Cloud Console.\n"
            "See README.md for setup instructions."
        )
        raise typer.Exit(1)

    console.print("[cyan]Opening browser for Google authentication...[/cyan]")

    # Delete existing token to force a fresh flow
    if tok_path.exists():
        tok_path.unlink()

    try:
        get_credentials(creds_path, tok_path, interactive=True)
        console.print(f"[green]✓[/green] Authentication successful. Token saved to: {tok_path}")
    except AuthError as e:
        console.print(f"[red]Authentication failed:[/red] {e}")
        raise typer.Exit(1)


@auth_app.command("status")
def status(
    token: Annotated[
        Optional[Path],
        typer.Option("--token", help="Path to token.json"),
    ] = None,
) -> None:
    """Show the current authentication token status."""
    from cli.display import console
    from uploader.auth import token_info
    from uploader.config import load as load_config
    from uploader.errors import ConfigError

    try:
        config = load_config(token_path=token)
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)

    tok_path = token or config.token_path
    info = token_info(tok_path)

    if not tok_path.exists():
        console.print(f"[yellow]No token found at:[/yellow] {tok_path}")
        console.print("Run [bold]gphotos auth login[/bold] to authenticate.")
        raise typer.Exit(1)

    if info["valid"]:
        expiry = info["expiry"] or "unknown"
        console.print(f"[green]✓[/green] Token is [bold]valid[/bold]. Expires: {expiry}")
    elif info.get("expired"):
        console.print(
            "[yellow]Token is expired.[/yellow] "
            "It will be refreshed automatically on the next upload run."
        )
    else:
        console.print(
            "[red]Token is invalid or corrupted.[/red] "
            "Run [bold]gphotos auth login[/bold] to re-authenticate."
        )
        raise typer.Exit(1)
