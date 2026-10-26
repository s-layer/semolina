"""
Semolina command-line interface.

Entry point for the `semolina` CLI command. Registers subcommands
for code generation and future tooling.
"""

import typer

from .codegen import codegen
from .dto_codegen import codegen_dto

__all__ = [
    "app",
    "version_callback",
    "main",
]


app = typer.Typer(
    name="semolina",
    help="Semolina — warehouse-native semantic view tooling.",
    no_args_is_help=True,
    add_completion=False,
)

app.command(
    "codegen",
    epilog=(
        "[bold]Exit codes[/bold]\n\n"
        "  [green]0[/green]  Success\n\n"
        "  [yellow]1[/yellow]  Unexpected error, including generated source that does not "
        "parse or that an installed ruff fails on, a committed file under "
        "[bold]--check[/bold] that does not parse, or S_LAYER_* tags that do not make a "
        "valid format for their member\n\n"
        # Not "invalid backend": a --check with no file to read also exits 2, and a message
        # naming only the backend sends you looking in the wrong place.
        "  [yellow]2[/yellow]  Invalid option -- an unrecognized or omitted "
        "[bold]--backend[/bold], connection config that could not be assembled, a backend "
        "whose driver is not installed or that cannot be built with no arguments, "
        "[bold]--backend duckdb[/bold] with no database path, an [bold]--output[/bold] "
        "directory that does not exist, "
        "[bold]--check[/bold] with no file (or two) to read or one that does not exist, "
        "[bold]--model[/bold] without [bold]--check[/bold], a custom engine's class names "
        "that are invalid or shadow a name the generated module imports, or views whose "
        "class names collide\n\n"
        "  [red]3[/red]  View not found in the warehouse\n\n"
        "  [red]4[/red]  Connection or authentication failure\n\n"
        # Yellow, not red: the colour convention here is green for success, yellow for a
        # caller-actionable outcome, red for a warehouse-side failure. Drift is the caller's
        # to fix. This table is duplicated in docs/src/how-to/codegen.rst; the two must agree.
        "  [yellow]5[/yellow]  Drift -- a committed model no longer matches the warehouse: "
        "an annotation, a role, a column or a metadata argument\n\n"
        # 6 means the same as in the codegen-dto table: the warehouse driver failed while
        # describing what was asked for. Built-in engines translate their driver errors, so
        # this is mostly a custom --backend engine's.
        "  [red]6[/red]  The warehouse driver failed while describing a view, or refused the "
        "lookup of its S_LAYER_* tags -- nothing was written -- or [bold]--check[/bold]'s "
        "result-schema probe failed and it compared against warehouse metadata instead"
    ),
)(codegen)

app.command(
    "codegen-dto",
    epilog=(
        "[bold]Exit codes[/bold]\n\n"
        "  [green]0[/green]  Success\n\n"
        "  [yellow]1[/yellow]  Unexpected error, including generated source that does not "
        "parse or that an installed ruff fails on, or a committed file under "
        "[bold]--check[/bold] that does not parse\n\n"
        # Same wording as the codegen table's 2 for the shared half, plus this command's own
        # pairings. A message naming only the backend would send you looking in the wrong
        # place when it was really the query path, the view, or the config file.
        "  [yellow]2[/yellow]  Invalid option -- an unrecognized or omitted "
        "[bold]--backend[/bold], a backend whose driver is not installed or that cannot be "
        "built with no arguments, a [bold]QUERY_PATH[/bold] that does not resolve to a query, "
        "a [bold]--view[/bold] field list a model could not declare, a malformed "
        r"[bold]\[tool.semolina.dto][/bold] section, combined routes, "
        "[bold]--name[/bold] passed with more than one DTO, class names that are invalid, "
        "shadow a name the generated module imports, or collide, an [bold]--output[/bold] "
        "directory that does not exist, or [bold]--check[/bold] with no file to read or one "
        "that does not exist\n\n"
        "  [red]3[/red]  View not found in the warehouse\n\n"
        "  [red]4[/red]  Connection or authentication failure\n\n"
        # 5 means the same thing here as in the codegen table, which is why it is spelled
        # the same and coloured the same. Yellow: drift is a caller-actionable outcome, and
        # a `--check` run that finds it has worked correctly.
        "  [yellow]5[/yellow]  Annotation drift -- a committed DTO no longer matches the "
        "result schema\n\n"
        # Red, not yellow: the colour convention is green for success, yellow for a
        # caller-actionable outcome, red for a warehouse-side failure, and a probe that
        # fails is the warehouse declining to describe the query. This table is duplicated
        # in docs/src/how-to/dto-codegen.rst; the two must agree.
        "  [red]6[/red]  Probe failed, or a projected field matched no result column -- no "
        "DTO was written"
    ),
)(codegen_dto)


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        from semolina import __version__

        typer.echo(f"semolina {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Semolina — warehouse-native semantic view tooling."""
