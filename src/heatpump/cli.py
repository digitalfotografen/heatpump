"""Command line interface: ``heatpump <command>``.

Only ``run`` and ``relay set`` ever change the relay.
"""

import asyncio
import csv
import getpass
import io
import json
import os
import sqlite3
import sys
import time as clock
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from heatpump import budget, db, heating, mqtt, planner, prices, replay, service
from heatpump import status as status_module
from heatpump.config import (
    ConfigError,
    MissingSecretError,
    Settings,
    load_settings,
    mqtt_credentials,
    tibber_token,
)
from heatpump.controller import run_service
from heatpump.i2c import shared_bus
from heatpump.logging_setup import setup_logging
from heatpump.mcp9600 import Mcp9600NotFoundError
from heatpump.models import (
    ControlDecision,
    Plan,
    Quarter,
    StoveReading,
    StoveState,
    TemperatureReading,
)
from heatpump.relay import RelayMismatch, ServiceAlreadyRunning, create_relay, service_running
from heatpump.service import DEFAULT_CONFIG_PATH, DEFAULT_ENV_FILE, DEFAULT_UNIT_PATH, SERVICE_NAME
from heatpump.stove import StoveListener, run_listener
from heatpump.weather import OpenMeteoProvider, WeatherError, fetch_and_store, is_stale

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


@app.command()
def run(ctx: typer.Context) -> None:
    """Run the controller service in the foreground (this is what systemd starts)."""
    settings = load_settings_or_exit(ctx)
    try:
        asyncio.run(run_service(settings))
    except ServiceAlreadyRunning as error:
        typer.echo(f"The service is already running: {error}", err=True)
        raise typer.Exit(code=1) from error
    except (MissingSecretError, OSError) as error:
        typer.echo(f"Cannot start the service: {error}", err=True)
        raise typer.Exit(code=1) from error


@app.command()
def stove(ctx: typer.Context, watch: WatchOption = False) -> None:
    """Show the latest stove reading and state; with --watch, follow the MQTT feed live."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    if watch:
        _watch_stove(settings, zone)
        return
    with closing(_open_read_only(settings)) as connection:
        reading = db.latest_stove_reading(connection)
    if reading is None:
        typer.echo("No stove readings stored; try `heatpump stove --watch`.", err=True)
        raise typer.Exit(code=1)
    age = datetime.now(UTC) - reading.ts
    state = reading.state
    if age > timedelta(seconds=settings.stove.stale_after_seconds):
        state = StoveState.STALE
    typer.echo(f"{_stove_line(reading, zone, state)} ({int(age.total_seconds())} s ago)")


def _stove_line(reading: StoveReading, zone: ZoneInfo, state: StoveState | None = None) -> str:
    local = reading.ts.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    return f"{local}  {reading.temperature:6.1f} °C  {state or reading.state}"


def _watch_stove(settings: Settings, zone: ZoneInfo) -> None:
    """Print every stove reading from the broker until interrupted; nothing is stored."""
    try:
        credentials = mqtt_credentials(settings)
    except MissingSecretError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    listener = StoveListener(
        settings.stove, on_reading=lambda reading: typer.echo(_stove_line(reading, zone))
    )
    mqtt = settings.mqtt
    typer.echo(f"Listening to {settings.stove.temperature_topic} on {mqtt.host}:{mqtt.port} ...")
    try:
        asyncio.run(run_listener(listener, mqtt, credentials))
    except KeyboardInterrupt:
        typer.echo("Stopped.")


@app.command()
def weather(
    ctx: typer.Context,
    fetch: Annotated[bool, typer.Option("--fetch", help="Poll Open-Meteo once.")] = False,
) -> None:
    """Show the latest outdoor temperature and its age; with --fetch, poll Open-Meteo first."""
    settings = load_settings_or_exit(ctx)
    if fetch:
        provider = OpenMeteoProvider.from_settings(settings.location)
        try:
            with closing(db.connect(settings.database.path)) as connection:
                db.create_schema(connection)
                asyncio.run(fetch_and_store(connection, provider))
        except (WeatherError, OSError) as error:
            typer.echo(f"Fetching the outdoor temperature failed: {error}", err=True)
            raise typer.Exit(code=1) from error
    with closing(_open_read_only(settings)) as connection:
        reading = db.latest_outdoor_reading(connection)
    if reading is None:
        typer.echo("No outdoor temperature stored; try `heatpump weather --fetch`.", err=True)
        raise typer.Exit(code=1)
    now = datetime.now(UTC)
    local = reading.ts.astimezone(ZoneInfo(settings.location.timezone))
    stale = " (stale)" if is_stale(reading, now, settings.weather) else ""
    age_minutes = int((now - reading.ts).total_seconds() // 60)
    typer.echo(
        f"{local:%Y-%m-%d %H:%M}  {reading.temperature:5.1f} °C  {reading.detail}, "
        f"{age_minutes} min ago{stale}"
    )


HEATING_WATCH_INTERVAL_SECONDS = 1


@app.command("heating")
def heating_command(ctx: typer.Context, watch: WatchOption = False) -> None:
    """Show the latest heating-circuit temperature; with --watch, sample the thermocouple live."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    if watch:
        _watch_heating(settings, zone)
        return
    with closing(_open_read_only(settings)) as connection:
        reading = db.latest_heating_reading(connection)
    if reading is None:
        typer.echo("No heating-circuit readings stored; try `heatpump heating --watch`.", err=True)
        raise typer.Exit(code=1)
    age = int((datetime.now(UTC) - reading.ts).total_seconds())
    typer.echo(f"{_heating_line(reading, zone)} ({age} s ago)")


def _heating_line(reading: TemperatureReading, zone: ZoneInfo) -> str:
    local = reading.ts.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    return f"{local}  {reading.temperature:6.2f} °C  (cold junction {reading.detail:.1f} °C)"


def _watch_heating(settings: Settings, zone: ZoneInfo) -> None:
    """Print a live sample every second until interrupted; nothing is stored."""
    if not settings.heating.enabled:
        typer.echo("heating.enabled is false in the configuration.", err=True)
        raise typer.Exit(code=1)
    try:
        sensor = heating.open_sensor(settings.heating)
    except (OSError, Mcp9600NotFoundError) as error:
        typer.echo(f"Cannot use the heating-circuit sensor: {error}", err=True)
        raise typer.Exit(code=1) from error
    try:
        while True:
            reading = heating.sample(sensor)
            typer.echo(_heating_line(reading, zone) if reading else "no valid reading")
            clock.sleep(HEATING_WATCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("Stopped.")


DEFAULT_LOG_RANGE = timedelta(hours=24)
DEFAULT_PAUSES_RANGE = timedelta(days=7)
EXPORT_FORMATS = ("csv", "json")


def _parse_moment(text: str | None, zone: ZoneInfo) -> datetime | None:
    """A date or date-time as given on the command line; without a zone it is local time."""
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        typer.echo(
            f"Not a date or time: {text!r} (use e.g. 2026-10-21 or 2026-10-21T14:30)", err=True
        )
        raise typer.Exit(code=1) from error
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment


@app.command()
def status(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Show mode, relay, guard, stove, prices and pause budget on one screen."""
    settings = load_settings_or_exit(ctx)
    with closing(_open_read_only(settings)) as connection:
        report = status_module.collect(
            connection,
            settings,
            datetime.now(UTC),
            service_running(settings.control.lock_file),
        )
    if report is None:
        typer.echo("No ticks logged yet; start the service with `heatpump run`.", err=True)
        raise typer.Exit(code=1)
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return
    Console().print(_status_table(report, ZoneInfo(settings.location.timezone)))


def _status_table(report: status_module.Status, zone: ZoneInfo) -> Table:
    last = report.last
    minutes = report.paused_24h
    next_pause = (
        f"{_local(report.next_pause.start, zone)} to {_local(report.next_pause.end, zone)}"
        if report.next_pause
        else "none planned"
    )
    since = int((report.now - report.mode_since).total_seconds() // 60)
    rows = {
        "Service": "running" if report.service_running else "[red]not running[/red]",
        "Last tick": f"{_local(last.ts, zone)} ({int(report.tick_age.total_seconds())} s ago)",
        "Mode": f"{last.mode} (for {since} min)",
        "Relay": f"requested {_paused_text(last.relay_requested)}, "
        f"actual {_paused_text(last.relay_actual)}",
        "Guard": last.guard_reason or "-",
        "Stove": f"{_number(last.stove_temp, ' °C')}, {last.stove_state or 'no state'}",
        "Outdoor": _number(last.outdoor_temp, " °C"),
        "Heating circuit": _number(last.heating_temp, " °C"),
        "Price now": _number(last.price_now, " SEK/kWh", 2),
        "Paused, last 24 h": f"price {minutes.price:.0f} min, stove {minutes.stove:.0f} min, "
        f"total {minutes.total:.0f} min",
        "Next price pause": next_pause,
        "Next price fetch": _local(report.next_price_fetch, zone),
    }
    table = Table(show_header=False, title="heatpump")
    for name, value in rows.items():
        table.add_row(name, value)
    return table


def _paused_text(paused: bool | None) -> str:
    return "unknown" if paused is None else ("paused" if paused else "running")


def _number(value: float | None, unit: str, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}{unit}"


@app.command()
def pauses(ctx: typer.Context, since: SinceOption = None) -> None:
    """List the recorded pauses (default: the last 7 days)."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    now = datetime.now(UTC)
    start = _parse_moment(since, zone) or now - DEFAULT_PAUSES_RANGE
    with closing(_open_read_only(settings)) as connection:
        events = db.pause_events_since(connection, start)
    table = Table(title=f"Pauses since {_local(start, zone)}")
    for heading in ("Kind", "Start (local)", "End (local)", "Minutes", "End reason"):
        table.add_column(heading)
    for event in events:
        end = event.ended_at or now
        table.add_row(
            event.kind.value,
            _local(event.started_at, zone),
            _local(event.ended_at, zone) if event.ended_at else "open",
            f"{(end - event.started_at).total_seconds() / 60:.0f}",
            event.end_reason or "-",
        )
    Console().print(table)


@app.command()
def log(
    ctx: typer.Context,
    since: SinceOption = None,
    tail: Annotated[int | None, typer.Option(help="Only the last N ticks.")] = None,
) -> None:
    """Show the control log, one line per tick (default: the last 24 hours)."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    start = _parse_moment(since, zone) or datetime.now(UTC) - DEFAULT_LOG_RANGE
    with closing(_open_read_only(settings)) as connection:
        ticks = db.control_log_since(connection, start)
    if tail is not None:
        ticks = ticks[-tail:] if tail > 0 else []
    if not ticks:
        typer.echo("No ticks logged in that range.", err=True)
        raise typer.Exit(code=1)
    for tick in ticks:
        typer.echo(_log_line(tick, zone))


def _log_line(tick: ControlDecision, zone: ZoneInfo) -> str:
    local = tick.ts.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    relay = f"{_paused_text(tick.relay_requested)}/{_paused_text(tick.relay_actual)}"
    price = "-" if tick.price_now is None else f"{tick.price_now:.2f}"
    stove = "-" if tick.stove_temp is None else f"{tick.stove_temp:.0f}"
    return (
        f"{local}  {tick.mode:<12} relay {relay:<16} price {price:>6}  stove {stove:>4} "
        f"{tick.stove_state or '-'}  {tick.guard_reason or ''}"
    ).rstrip()


@app.command()
def export(
    ctx: typer.Context,
    table: str,
    from_: Annotated[str | None, typer.Option("--from", help="Start of the range.")] = None,
    to: Annotated[str | None, typer.Option(help="End of the range (exclusive).")] = None,
    format_: Annotated[str, typer.Option("--format", help="csv or json.")] = "csv",
    out: Annotated[Path | None, typer.Option(help="Output file (default: stdout).")] = None,
) -> None:
    """Export a table for a time range, for plots and analysis."""
    settings = load_settings_or_exit(ctx)
    if table not in db.TABLES:
        typer.echo(f"Unknown table {table!r}; choose from {', '.join(db.TABLES)}.", err=True)
        raise typer.Exit(code=1)
    if format_ not in EXPORT_FORMATS:
        typer.echo(f"Unknown format {format_!r}; choose csv or json.", err=True)
        raise typer.Exit(code=1)
    zone = ZoneInfo(settings.location.timezone)
    with closing(_open_read_only(settings)) as connection:
        rows = db.rows_between(
            connection, table, _parse_moment(from_, zone), _parse_moment(to, zone)
        )
    text = _rows_as_json(rows) if format_ == "json" else _rows_as_csv(rows, table, settings)
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {len(rows)} rows to {out}", err=True)


def _rows_as_json(rows: list[sqlite3.Row]) -> str:
    return json.dumps([dict(row) for row in rows], indent=2) + "\n"


def _rows_as_csv(rows: list[sqlite3.Row], table: str, settings: Settings) -> str:
    """CSV with a header even for an empty range."""
    header = list(rows[0].keys()) if rows else _column_names(table, settings)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(tuple(row) for row in rows)
    return buffer.getvalue()


def _column_names(table: str, settings: Settings) -> list[str]:
    with closing(_open_read_only(settings)) as connection:
        return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


@app.command("replay")
def replay_command(
    ctx: typer.Context,
    date: Annotated[
        datetime, typer.Option(formats=["%Y-%m-%d"], help="Local day to replay, YYYY-MM-DD.")
    ],
) -> None:
    """Run the planner over a stored day and print what would have happened (relay untouched)."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    day = date.date()
    day_start = datetime.combine(day, time(), tzinfo=zone)
    day_end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    with closing(_open_read_only(settings)) as connection:
        found = db.prices_for_range(connection, day_start, day_end + planner.LOOKAHEAD)
    if not any(day_start <= quarter.start < day_end for quarter in found.quarters):
        typer.echo(f"No prices stored for {day}; run `heatpump prices fetch`.", err=True)
        raise typer.Exit(code=1)
    try:
        result = replay.replay_day(found.quarters, day_start, day_end, settings.price_pause)
    except ValueError as error:
        typer.echo(f"Cannot replay: {error}", err=True)
        raise typer.Exit(code=1) from error
    console = Console()
    console.print(f"Replay of {day}: {len(result.pauses)} price pauses")
    console.print(f"Expected saving: {result.saving_sek:.2f} SEK")
    if result.pauses:
        table = Table(title="Price pauses")
        table.add_column("Start (local)")
        table.add_column("End (local)")
        table.add_column("Minutes", justify="right")
        for pause in result.pauses:
            table.add_row(
                _local(pause.start, zone), _local(pause.end, zone), f"{pause.minutes:.0f}"
            )
        console.print(table)


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
def prices_fetch(
    ctx: typer.Context,
    show: Annotated[bool, typer.Option("--show", help="Print the prices.")] = False,
) -> None:
    """Fetch today's and tomorrow's prices from Tibber and store them."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    try:
        token = tibber_token(settings)
        with closing(db.connect(settings.database.path)) as connection:
            db.create_schema(connection)
            quarters = asyncio.run(
                prices.fetch_and_store(
                    connection,
                    token,
                    settings.tibber.home_index,
                    settings.tibber.fetch_give_up_local_time,
                    zone,
                )
            )
    except (MissingSecretError, OSError, *prices.FETCH_ERRORS) as error:
        typer.echo(f"Fetching prices failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Stored {len(quarters)} quarters in {settings.database.path}")
    if show:
        Console().print(_prices_table("Fetched prices", quarters, zone))


@prices_app.command("show")
def prices_show(
    ctx: typer.Context,
    date: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Local day, YYYY-MM-DD (default: today)."),
    ] = None,
) -> None:
    """Show the quarter-hour prices of one local day."""
    settings = load_settings_or_exit(ctx)
    zone = ZoneInfo(settings.location.timezone)
    day = (date or datetime.now(zone)).date()
    start = datetime.combine(day, time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    try:
        connection = db.connect(settings.database.path, read_only=True)
    except FileNotFoundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    with closing(connection):
        found = db.prices_for_range(connection, start, end)
    if not found.quarters:
        typer.echo(f"No prices stored for {day}; run `heatpump prices fetch`.", err=True)
        raise typer.Exit(code=1)
    console = Console()
    console.print(_prices_table(f"Prices for {day}", found.quarters, zone))
    for gap_start, gap_end in found.gaps:
        console.print(
            f"[red]Missing prices:[/red] {_local(gap_start, zone)} to {_local(gap_end, zone)}"
        )


def _prices_table(title: str, quarters: list[Quarter], zone: ZoneInfo) -> Table:
    """One row per quarter in local time; the quarter that is running now is marked."""
    now = datetime.now(UTC)
    table = Table(title=title)
    table.add_column("Start (local)")
    table.add_column("Energy", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("")
    for quarter in quarters:
        total = f"{quarter.total:.2f}" if quarter.total is not None else "-"
        marker = "<- now" if quarter.start <= now < quarter.end else ""
        local_start = quarter.start.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
        table.add_row(local_start, f"{quarter.energy:.2f}", total, marker)
    return table


@plan_app.command("show")
def plan_show(ctx: typer.Context) -> None:
    """Show the stored plan."""
    settings = load_settings_or_exit(ctx)
    with closing(_open_read_only(settings)) as connection:
        plan = db.latest_plan(connection)
    if plan is None:
        typer.echo("No plan stored yet; try `heatpump plan now`.", err=True)
        raise typer.Exit(code=1)
    _print_plan(plan, ZoneInfo(settings.location.timezone))


@plan_app.command("now")
def plan_now(ctx: typer.Context) -> None:
    """Run the planner on current data and print the result without storing it."""
    settings = load_settings_or_exit(ctx)
    now = datetime.now(UTC)
    first_slot = planner.floor_to_quarter(now)
    with closing(_open_read_only(settings)) as connection:
        found = db.prices_for_range(connection, first_slot, first_slot + planner.LOOKAHEAD)
        history = db.pause_events_since(connection, now - budget.WINDOW)
    if not found.quarters:
        typer.echo("No prices stored from now on; run `heatpump prices fetch`.", err=True)
        raise typer.Exit(code=1)
    try:
        plan = planner.plan_pauses(found.quarters, history, now, settings.price_pause)
    except ValueError as error:
        typer.echo(f"Cannot plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    _print_plan(plan, ZoneInfo(settings.location.timezone))


def _open_read_only(settings: Settings) -> sqlite3.Connection:
    """Open the configured database for reading, exiting with code 1 if it does not exist."""
    try:
        return db.connect(settings.database.path, read_only=True)
    except FileNotFoundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error


def _print_plan(plan: Plan, zone: ZoneInfo) -> None:
    console = Console()
    console.print(
        f"Plan made {_local(plan.created_at, zone)} ({plan.reason}), "
        f"horizon {_local(plan.horizon_start, zone)} to {_local(plan.horizon_end, zone)}"
    )
    console.print(f"Expected saving: {plan.expected_saving_sek:.2f} SEK")
    if not plan.pauses:
        console.print("No price pauses in this plan.")
        return
    table = Table(title="Price pauses")
    table.add_column("Start (local)")
    table.add_column("End (local)")
    table.add_column("Minutes", justify="right")
    for pause in plan.pauses:
        table.add_row(_local(pause.start, zone), _local(pause.end, zone), f"{pause.minutes:.0f}")
    console.print(table)


class RelayState(StrEnum):
    PAUSED = "paused"
    RUNNING = "running"


@relay_app.command("status")
def relay_status(ctx: typer.Context) -> None:
    """Show the relay driver and the current relay state; never changes the relay."""
    settings = load_settings_or_exit(ctx)
    try:
        paused = create_relay(settings.relay).read()
    except OSError as error:
        typer.echo(f"Cannot read the relay: {error}", err=True)
        raise typer.Exit(code=1) from error
    relay = settings.relay
    where = f" ({relay.i2c_address:#04x} {relay.pin})" if relay.driver == "mcp23017" else ""
    typer.echo(f"Driver: {relay.driver}{where}")
    typer.echo(f"Relay: {RelayState.PAUSED if paused else RelayState.RUNNING}")
    if service_running(settings.control.lock_file):
        typer.echo("The service is running.")


@relay_app.command("set")
def relay_set(
    ctx: typer.Context,
    state: Annotated[RelayState, typer.Argument(help="paused or running")],
) -> None:
    """Set the relay by hand for bench testing; refused while the service runs."""
    settings = load_settings_or_exit(ctx)
    if service_running(settings.control.lock_file):
        typer.echo("The service is running and owns the relay; stop it first.", err=True)
        raise typer.Exit(code=1)
    try:
        create_relay(settings.relay).set(state is RelayState.PAUSED)
    except (OSError, RelayMismatch) as error:
        typer.echo(f"Setting the relay failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Relay set to {state}.")


@hat_app.command("scan")
def hat_scan(ctx: typer.Context) -> None:
    """List the devices on the I2C bus with their expected roles."""
    settings = load_settings_or_exit(ctx)
    expected = {
        (settings.relay.i2c_bus, settings.relay.i2c_address): "MCP23017 (relay)",
        (settings.heating.i2c_bus, settings.heating.i2c_address): "MCP9600 (heating circuit)",
    }
    table = Table(title="I2C devices")
    table.add_column("Bus", justify="right")
    table.add_column("Address")
    table.add_column("Role")
    missing = False
    for bus_number in sorted({bus for bus, _ in expected}):
        try:
            found = shared_bus(bus_number).scan()
        except OSError as error:
            typer.echo(f"Cannot open I2C bus {bus_number}: {error}", err=True)
            raise typer.Exit(code=1) from error
        for address in found:
            table.add_row(
                str(bus_number), f"{address:#04x}", expected.get((bus_number, address), "other")
            )
        for (bus, address), role in expected.items():
            if bus == bus_number and address not in found:
                missing = True
                table.add_row(str(bus), f"{address:#04x}", f"[red]{role}: NOT FOUND[/red]")
    Console().print(table)
    if missing:
        raise typer.Exit(code=1)


@mqtt_app.command("publish-state")
def mqtt_publish_state(ctx: typer.Context) -> None:
    """Publish the latest stored state once, for debugging the broker side; no relay access."""
    settings = load_settings_or_exit(ctx)
    with closing(_open_read_only(settings)) as connection:
        messages = mqtt.snapshot_messages(connection, settings, datetime.now(UTC))
    if not messages:
        typer.echo("No ticks logged yet; start the service with `heatpump run`.", err=True)
        raise typer.Exit(code=1)
    try:
        asyncio.run(mqtt.publish_once(settings.mqtt, mqtt_credentials(settings), messages))
    except (MissingSecretError, mqtt.aiomqtt.MqttError) as error:
        typer.echo(
            f"Cannot publish to {settings.mqtt.host}:{settings.mqtt.port}: {error}", err=True
        )
        raise typer.Exit(code=1) from error
    for message in messages:
        typer.echo(f"{message.topic}: {message.payload}")


@service_app.command("install")
def service_install(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="Where to write the unit.")] = DEFAULT_UNIT_PATH,
    user: Annotated[str | None, typer.Option(help="User to run as (default: you).")] = None,
    env_file: Annotated[Path, typer.Option(help="File with the secrets.")] = DEFAULT_ENV_FILE,
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the unit; write nothing.")
    ] = False,
) -> None:
    """Write or update the systemd unit from the template (needs sudo for /etc/systemd)."""
    unit = service.render_unit(
        user=user or os.environ.get("SUDO_USER") or getpass.getuser(),
        executable=Path(sys.executable).parent / "heatpump",
        config=(ctx.obj or DEFAULT_CONFIG_PATH).absolute(),
        env_file=env_file,
    )
    if print_only:
        typer.echo(unit, nl=False)
        return
    try:
        output.write_text(unit, encoding="utf-8")
    except OSError as error:
        typer.echo(f"Cannot write {output}: {error}. Try again with sudo.", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Wrote {output}. Start it with:")
    typer.echo(f"  sudo systemctl daemon-reload && sudo systemctl enable --now {SERVICE_NAME}")


@service_app.command("status")
def service_status() -> None:
    """Show ``systemctl status`` for the service."""
    try:
        code = service.systemctl_status()
    except FileNotFoundError as error:
        typer.echo("systemctl not found; this is not a systemd machine.", err=True)
        raise typer.Exit(code=1) from error
    raise typer.Exit(code=code)
