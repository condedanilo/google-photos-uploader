"""CLI entry point.

Registered as the `gphotos` script in pyproject.toml.

Commands:
  gphotos init            Guided first-run setup
  gphotos upload <dir>    Bulk upload photos to Google Photos
  gphotos auth login      Authenticate with Google OAuth
  gphotos auth status     Show token validity
"""
from __future__ import annotations

import click
import typer

from cli.commands.auth import auth_app
from cli.commands.init import init
from cli.commands.upload import upload


# ---------------------------------------------------------------------------
# Expanded help rendering
# ---------------------------------------------------------------------------

def _render_command(console, display_name: str, cmd: click.BaseCommand) -> None:
    """Render a Rich panel for a single command showing all its options."""
    from rich.panel import Panel
    from rich.table import Table

    brief = cmd.help or ""

    table = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
    table.add_column(style="cyan", no_wrap=True, min_width=34)
    table.add_column()

    has_params = False
    for param in cmd.params:
        # Skip the built-in --help option
        if param.name == "help":
            continue
        if isinstance(param, click.Option):
            opt_str = ", ".join(param.opts)
            if param.is_flag:
                display = opt_str
            else:
                meta = param.make_metavar()
                display = f"{opt_str} {meta}"
            table.add_row(display, param.help or "")
            has_params = True
        elif isinstance(param, click.Argument):
            arg_help = getattr(param, "help", "") or ""
            table.add_row(f"<{param.name.upper()}>", arg_help)
            has_params = True

    console.print(Panel(
        table if has_params else "",
        title=f"  gphotos {display_name}  ",
        subtitle=f"[dim]{brief}[/dim]" if brief else None,
        border_style="blue",
        title_align="left",
        padding=(0, 1),
    ))


def _print_help() -> None:
    """Print an expanded help page showing all commands and their options."""
    import typer.main as _typer_main
    from cli.display import console
    from rich.panel import Panel

    header = (
        "Bulk upload photos to Google Photos.\n\n"
        "New user? Run:  [bold]gphotos init[/bold]\n"
        "Then:           [bold]gphotos upload [italic]/path/to/photos[/italic][/bold]"
    )
    console.print(Panel(
        header,
        title="[bold]gphotos[/bold]",
        border_style="blue",
        padding=(1, 2),
    ))

    click_group = _typer_main.get_command(app)
    root_ctx = click.Context(click_group, info_name="gphotos")

    for name in click_group.list_commands(root_ctx):
        cmd = click_group.get_command(root_ctx, name)
        if cmd is None:
            continue
        if isinstance(cmd, click.Group):
            sub_ctx = click.Context(cmd, parent=root_ctx, info_name=name)
            for sub_name in cmd.list_commands(sub_ctx):
                sub_cmd = cmd.get_command(sub_ctx, sub_name)
                if sub_cmd is not None:
                    _render_command(console, f"{name} {sub_name}", sub_cmd)
        else:
            _render_command(console, name, cmd)


def _help_callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if value and not ctx.resilient_parsing:
        _print_help()
        raise typer.Exit()


# ---------------------------------------------------------------------------
# App definition
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="gphotos",
    add_completion=False,
    add_help_option=False,   # we add our own --help in the callback below
    no_args_is_help=False,
    rich_markup_mode="rich",
)

# Register top-level commands
app.command("init")(init)
app.command("upload")(upload)

# Register auth subcommands
app.add_typer(auth_app, name="auth")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    _help: bool = typer.Option(
        False,
        "--help",
        "-h",
        is_eager=True,
        expose_value=False,
        help="Show this message and exit.",
        callback=_help_callback,
    ),
) -> None:
    """Bulk upload photos to Google Photos."""
    if ctx.invoked_subcommand is None:
        _print_help()
        raise typer.Exit()


if __name__ == "__main__":
    app()
