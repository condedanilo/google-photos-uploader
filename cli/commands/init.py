"""`gphotos init` — guided first-run setup."""
from __future__ import annotations

import typer


def init() -> None:
    """Guided first-run setup: create config, verify credentials, and authenticate."""
    from cli.display import console, print_credentials_setup_panel
    from uploader.auth import get_credentials
    from uploader.config import USER_CONFIG_PATH, ensure_user_config, load as load_config
    from uploader.errors import AuthError, ConfigError

    console.print("\n[bold]gphotos init[/bold] — first-run setup\n")

    # Step 1: Config
    created = ensure_user_config()
    if created:
        console.print(f"[green]✓[/green] Created default config → {created}")
    else:
        console.print(f"[green]✓[/green] Config ready → {USER_CONFIG_PATH}")

    # Step 2: Load config to resolve paths
    try:
        config = load_config()
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)

    # Step 3: Check for credentials file
    creds_path = config.credentials_path
    if not creds_path.exists():
        console.print(f"[yellow]✗[/yellow] Credentials file not found at: {creds_path}\n")
        print_credentials_setup_panel(creds_path)
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Credentials file found → {creds_path}")

    # Step 4: Run OAuth flow
    console.print("[cyan]Opening browser for Google authentication...[/cyan]")
    tok_path = config.token_path
    if tok_path.exists():
        tok_path.unlink()

    try:
        get_credentials(creds_path, tok_path, interactive=True)
    except AuthError as e:
        console.print(f"[red]Authentication failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Authenticated. Token saved → {tok_path}")

    # Summary
    console.print(
        "\n[bold green]All set![/bold green] You're ready to upload:\n\n"
        "  [bold]gphotos upload[/bold] [italic]<path-to-your-photos>[/italic]\n"
    )
