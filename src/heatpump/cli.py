"""Command line interface: ``heatpump <command>``.

Every command is a stub that names the phase of docs/plan.md in which it gets
implemented, so ``--help`` already shows the whole surface. Only ``run`` and
``relay set`` will ever change the relay.
"""

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from heatpump import db
from heatpump.config import ConfigError, Settings, load_settings
from heatpump.logging_setup import setup_logging

app = typer.Typer(
    name="heatpump",
    help="Heat pump price controller: pauses the pump through the Rego EXT input.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Create and inspect the database.", no_args_is_help=True)
prices_app = typer.Typer(help="Electricity prices from Tibber.", no_args_is_help=True)
plan_app = typer.Typer(help="The pause plan.", no_args_is_help=True)
relay_app = typer.Typer(help="The EXT relay on the HAT.", no_args_is_help=True)
hat_app = typer.Typer(help="The I2C devices on the HAT.", no_args_is_help=True)
mqtt_app = typer.Typer(help="MQTT publishing.", no_args_is_help=True)
service_app = typer.Typer(help="The systemd service.", no_args_is_help=True)
for name, sub_app in [
    ("db", db_app),
    ("prices", prices_app),
    ("plan", plan_app),
    ("relay", relay_app),
    ("hat", hat_app),
    ("mqtt", mqtt_app),
    ("service", service_app),
]:
    app.add_typer(sub_app, name=name)

JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON for machine use.")]
WatchOption = Annotated[bool, typer.Option("--watch", help="Keep running and print updates.")]
SinceOption = Annotated[str | None, typer.Option(help="Start of the range (ISO date or time).")]


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Config file (default: $HEATPUMP_CONFIG, ./config.yaml, then /etc/heatpump).",
        ),
    ] = None,
) -> None:
    """Remember the config path; the file is only read by commands that need it."""
    ctx.obj = config


def load_settings_or_exit(ctx: typer.Context) -> Settings:
    """Load the configuration chosen with ``--config``, exiting with code 1 if it is invalid."""
    try:
        settings = load_settings(ctx.obj)
    except (ConfigError, ValidationError) as error:
        typer.echo(f"Invalid configuration: {error}", err=True)
        raise typer.Exit(code=1) from error
    setup_logging(settings.control.log_level)
    return settings


def not_implemented(phase: int) -> None:
    """Tell the user which phase implements the command and exit with code 1."""
    typer.echo(f"Not implemented in this phase (planned for phase {phase}).", err=True)
    raise typer.Exit(code=1)


@app.command()
def run() -> None:
    """Run the controller service in the foreground (this is what systemd starts)."""
    not_implemented(9)


@app.command()
def stove(watch: WatchOption = False) -> None:
    """Show the latest stove reading and state."""
    not_implemented(5)


@app.command()
def weather(
    fetch: Annotated[bool, typer.Option("--fetch", help="Poll Open-Meteo once.")] = False,
) -> None:
    """Show the latest outdoor temperature and its age."""
    not_implemented(6)


@app.command()
def heating(watch: WatchOption = False) -> None:
    """Show the latest heating-circuit temperature."""
    not_implemented(7)


@app.command()
def status(as_json: JsonOption = False) -> None:
    """Show mode, relay, guard, stove, prices and pause budget on one screen."""
    not_implemented(9)


@app.command()
def pauses(since: SinceOption = None) -> None:
    """List the recorded pauses."""
    not_implemented(9)


@app.command()
def log(since: SinceOption = None, tail: Annotated[int | None, typer.Option()] = None) -> None:
    """Show the control log, one line per tick."""
    not_implemented(9)


@app.command()
def export(
    table: str,
    from_: Annotated[str | None, typer.Option("--from", help="Start of the range.")] = None,
    to: Annotated[str | None, typer.Option(help="End of the range.")] = None,
    format_: Annotated[str, typer.Option("--format", help="csv or json.")] = "csv",
    out: Annotated[Path | None, typer.Option(help="Output file (default: stdout).")] = None,
) -> None:
    """Export a table for a time range."""
    not_implemented(9)


@app.command()
def replay(date: Annotated[str, typer.Option(help="Day to replay, YYYY-MM-DD.")]) -> None:
    """Run the planner over a stored day and print what would have happened."""
    not_implemented(11)


@db_app.command("init")
def db_init(ctx: typer.Context) -> None:
    """Create the database schema (idempotent)."""
    settings = load_settings_or_exit(ctx)
    path = settings.database.path
    try:
        connection = db.connect(path)
    except OSError as error:
        typer.echo(f"Cannot open {path}: {error}", err=True)
        raise typer.Exit(code=1) from error
    with closing(connection):
        db.create_schema(connection)
    typer.echo(f"Database ready at {path}")


@db_app.command("info")
def db_info(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Show path, size, row counts and first/last timestamp per table."""
    settings = load_settings_or_exit(ctx)
    path = settings.database.path
    try:
        connection = db.connect(path, read_only=True)
    except FileNotFoundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    with closing(connection):
        summaries = db.table_summaries(connection)
    size_bytes = path.stat().st_size
    if as_json:
        typer.echo(json.dumps(_db_info_json(path, size_bytes, summaries), indent=2))
        return
    zone = ZoneInfo(settings.location.timezone)
    Console().print(_db_info_table(path, size_bytes, summaries, zone))


def _db_info_json(path: Path, size_bytes: int, summaries: list[db.TableSummary]) -> dict:
    return {
        "path": str(path),
        "size_bytes": size_bytes,
        "tables": {
            s.table: {
                "rows": s.rows,
                "first": db.to_iso(s.first) if s.first else None,
                "last": db.to_iso(s.last) if s.last else None,
            }
            for s in summaries
        },
    }


def _db_info_table(
    path: Path, size_bytes: int, summaries: list[db.TableSummary], zone: ZoneInfo
) -> Table:
    table = Table(title=f"{path} ({size_bytes / 1024:.0f} KiB)")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    table.add_column("First (local)")
    table.add_column("Last (local)")
    for s in summaries:
        table.add_row(s.table, str(s.rows), _local(s.first, zone), _local(s.last, zone))
    return table


def _local(moment: datetime | None, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M") if moment else "-"


@prices_app.command("fetch")
def prices_fetch(show: Annotated[bool, typer.Option("--show")] = False) -> None:
    """Fetch today's and tomorrow's prices from Tibber and store them."""
    not_implemented(2)


@prices_app.command("show")
def prices_show(date: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None) -> None:
    """Show the quarter-hour prices in local time."""
    not_implemented(2)


@plan_app.command("show")
def plan_show() -> None:
    """Show the stored plan."""
    not_implemented(4)


@plan_app.command("now")
def plan_now() -> None:
    """Run the planner on current data and print the result without storing it."""
    not_implemented(4)


@relay_app.command("status")
def relay_status() -> None:
    """Show the relay driver and the current relay state."""
    not_implemented(7)


@relay_app.command("set")
def relay_set(state: Annotated[str, typer.Argument(help="paused or running")]) -> None:
    """Set the relay by hand for bench testing; refused while the service runs."""
    not_implemented(7)


@hat_app.command("scan")
def hat_scan() -> None:
    """List the devices on the I2C bus with their expected roles."""
    not_implemented(7)


@mqtt_app.command("publish-state")
def mqtt_publish_state() -> None:
    """Publish the current state once, for debugging the broker side."""
    not_implemented(10)


@service_app.command("install")
def service_install() -> None:
    """Write or update the systemd unit from the template."""
    not_implemented(11)


@service_app.command("status")
def service_status() -> None:
    """Show ``systemctl status`` for the service."""
    not_implemented(11)
