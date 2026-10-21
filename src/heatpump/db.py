"""SQLite storage: schema and small repository functions, plain parameterised SQL.

Timestamps are stored as ISO 8601 UTC strings (``2026-10-20T12:00:00+00:00``).
The format is fixed, so string order equals time order and range queries work
with plain comparisons.
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from heatpump.models import (
    ControlDecision,
    ControlMode,
    Pause,
    PauseEvent,
    PauseKind,
    Plan,
    Quarter,
    StoveReading,
    StoveState,
    TemperatureReading,
)

BUSY_TIMEOUT_MS = 5000
DEFAULT_SLOT_MINUTES = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    slot_start TEXT PRIMARY KEY,
    slot_minutes INTEGER NOT NULL,
    energy REAL NOT NULL,
    tax REAL NOT NULL,
    total REAL,
    currency TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stove_readings (
    ts TEXT PRIMARY KEY,
    temperature REAL NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outdoor_readings (
    ts TEXT PRIMARY KEY,
    temperature REAL NOT NULL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS heating_readings (
    ts TEXT PRIMARY KEY,
    temperature REAL NOT NULL,
    cold_junction REAL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    expected_saving_sek REAL NOT NULL,
    pauses_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pause_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS control_log (
    ts TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    relay_requested INTEGER NOT NULL,
    relay_actual INTEGER,
    guard_reason TEXT,
    stove_state TEXT,
    stove_temp REAL,
    outdoor_temp REAL,
    heating_temp REAL,
    price_now REAL,
    plan_id INTEGER
);
"""

# Table name -> the column holding its time axis (used by `db info`).
TABLES = {
    "prices": "slot_start",
    "stove_readings": "ts",
    "outdoor_readings": "ts",
    "heating_readings": "ts",
    "plans": "created_at",
    "pause_events": "started_at",
    "control_log": "ts",
}


@dataclass(frozen=True)
class PriceRange:
    """Quarters found in a time range, plus the sub-ranges for which no price is stored."""

    quarters: list[Quarter]
    gaps: list[tuple[datetime, datetime]]


@dataclass(frozen=True)
class TableSummary:
    table: str
    rows: int
    first: datetime | None
    last: datetime | None


def to_iso(moment: datetime) -> str:
    """Format an aware datetime as UTC ISO 8601 with whole seconds.

    :raises ValueError: if ``moment`` is naive.
    """
    if moment.tzinfo is None:
        raise ValueError("Naive datetime: all stored times must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _from_iso_or_none(text: str | None) -> datetime | None:
    return from_iso(text) if text is not None else None


def _to_iso_or_none(moment: datetime | None) -> str | None:
    return to_iso(moment) if moment is not None else None


def _bool_or_none(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def connect(path: Path | str, read_only: bool = False) -> sqlite3.Connection:
    """Open the database in WAL mode; a read-only connection works while the service runs.

    A writable connection creates the parent directory. ``":memory:"`` is allowed for tests.

    :raises FileNotFoundError: if ``read_only`` and the file does not exist.
    """
    if str(path) == ":memory:":
        connection = sqlite3.connect(":memory:")
    elif read_only:
        if not Path(path).exists():
            raise FileNotFoundError(f"Database {path} does not exist (run `heatpump db init`)")
        connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode = WAL")
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all tables that do not exist yet."""
    with conn:
        conn.executescript(SCHEMA)


# --- prices -----------------------------------------------------------------


def upsert_prices(
    conn: sqlite3.Connection, quarters: Iterable[Quarter], fetched_at: datetime
) -> None:
    rows = [
        (
            to_iso(q.start),
            q.slot_minutes,
            q.energy,
            q.tax,
            q.total,
            q.currency,
            to_iso(fetched_at),
        )
        for q in quarters
    ]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _quarter(row: sqlite3.Row) -> Quarter:
    return Quarter(
        start=from_iso(row["slot_start"]),
        energy=row["energy"],
        tax=row["tax"],
        total=row["total"],
        currency=row["currency"],
        slot_minutes=row["slot_minutes"],
    )


def prices_for_range(conn: sqlite3.Connection, start: datetime, end: datetime) -> PriceRange:
    """Quarters starting in ``[start, end)`` in time order, and the stretches with no price."""
    rows = conn.execute(
        "SELECT * FROM prices WHERE slot_start >= ? AND slot_start < ? ORDER BY slot_start",
        (to_iso(start), to_iso(end)),
    )
    quarters = [_quarter(row) for row in rows]
    gaps: list[tuple[datetime, datetime]] = []
    covered_until = start
    for quarter in quarters:
        if quarter.start > covered_until:
            gaps.append((covered_until, quarter.start))
        covered_until = max(covered_until, quarter.end)
    if covered_until < end:
        gaps.append((covered_until, end))
    return PriceRange(quarters, gaps)


def latest_price_slot(conn: sqlite3.Connection) -> datetime | None:
    """Start of the last stored quarter, used to decide whether to fetch again."""
    row = conn.execute("SELECT MAX(slot_start) FROM prices").fetchone()
    return _from_iso_or_none(row[0])


# --- readings ---------------------------------------------------------------


def insert_stove_reading(conn: sqlite3.Connection, reading: StoveReading) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stove_readings VALUES (?, ?, ?)",
            (to_iso(reading.ts), reading.temperature, reading.state.value),
        )


def latest_stove_reading(conn: sqlite3.Connection) -> StoveReading | None:
    row = conn.execute("SELECT * FROM stove_readings ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return StoveReading(from_iso(row["ts"]), row["temperature"], StoveState(row["state"]))


def _insert_temperature(conn: sqlite3.Connection, table: str, reading: TemperatureReading) -> None:
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)",
            (to_iso(reading.ts), reading.temperature, reading.detail),
        )


def _latest_temperature(conn: sqlite3.Connection, table: str) -> TemperatureReading | None:
    row = conn.execute(f"SELECT * FROM {table} ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return TemperatureReading(from_iso(row["ts"]), row["temperature"], row[2])


def insert_outdoor_reading(conn: sqlite3.Connection, reading: TemperatureReading) -> None:
    """Store an outdoor sample; ``detail`` is the source name."""
    _insert_temperature(conn, "outdoor_readings", reading)


def latest_outdoor_reading(conn: sqlite3.Connection) -> TemperatureReading | None:
    return _latest_temperature(conn, "outdoor_readings")


def insert_heating_reading(conn: sqlite3.Connection, reading: TemperatureReading) -> None:
    """Store a heating-circuit sample; ``detail`` is the cold-junction temperature."""
    _insert_temperature(conn, "heating_readings", reading)


def latest_heating_reading(conn: sqlite3.Connection) -> TemperatureReading | None:
    return _latest_temperature(conn, "heating_readings")


# --- plans ------------------------------------------------------------------


def insert_plan(conn: sqlite3.Connection, plan: Plan) -> int:
    """Store a planner run, even one without pauses, and return its id."""
    pauses = [
        {"kind": p.kind.value, "start": to_iso(p.start), "end": to_iso(p.end)} for p in plan.pauses
    ]
    with conn:
        cursor = conn.execute(
            "INSERT INTO plans (created_at, reason, horizon_start, horizon_end,"
            " expected_saving_sek, pauses_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                to_iso(plan.created_at),
                plan.reason,
                to_iso(plan.horizon_start),
                to_iso(plan.horizon_end),
                plan.expected_saving_sek,
                json.dumps(pauses),
            ),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _plan(row: sqlite3.Row) -> Plan:
    pauses = tuple(
        Pause(PauseKind(p["kind"]), from_iso(p["start"]), from_iso(p["end"]))
        for p in json.loads(row["pauses_json"])
    )
    return Plan(
        created_at=from_iso(row["created_at"]),
        reason=row["reason"],
        horizon_start=from_iso(row["horizon_start"]),
        horizon_end=from_iso(row["horizon_end"]),
        expected_saving_sek=row["expected_saving_sek"],
        pauses=pauses,
        id=row["id"],
    )


def latest_plan(conn: sqlite3.Connection) -> Plan | None:
    row = conn.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    return _plan(row) if row else None


def get_plan(conn: sqlite3.Connection, plan_id: int) -> Plan | None:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan(row) if row else None


# --- pause events -----------------------------------------------------------


def open_pause_event(
    conn: sqlite3.Connection, kind: PauseKind, started_at: datetime, planned_end: datetime
) -> int:
    """Record that a pause has started and return the event id."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO pause_events (kind, started_at, planned_end) VALUES (?, ?, ?)",
            (kind.value, to_iso(started_at), to_iso(planned_end)),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def close_pause_event(
    conn: sqlite3.Connection, event_id: int, ended_at: datetime, end_reason: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE pause_events SET ended_at = ?, end_reason = ? WHERE id = ?",
            (to_iso(ended_at), end_reason, event_id),
        )


def _pause_event(row: sqlite3.Row) -> PauseEvent:
    return PauseEvent(
        kind=PauseKind(row["kind"]),
        started_at=from_iso(row["started_at"]),
        planned_end=from_iso(row["planned_end"]),
        ended_at=_from_iso_or_none(row["ended_at"]),
        end_reason=row["end_reason"],
        id=row["id"],
    )


def pause_events_since(conn: sqlite3.Connection, since: datetime) -> list[PauseEvent]:
    """Pauses that were running at or after ``since`` (still open, or ended after it)."""
    rows = conn.execute(
        "SELECT * FROM pause_events WHERE ended_at IS NULL OR ended_at >= ? ORDER BY started_at",
        (to_iso(since),),
    )
    return [_pause_event(row) for row in rows]


def open_pause(conn: sqlite3.Connection) -> PauseEvent | None:
    """The pause that has no end yet; after a restart the controller resumes from it."""
    row = conn.execute(
        "SELECT * FROM pause_events WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return _pause_event(row) if row else None


# --- control log ------------------------------------------------------------


def insert_control_log(conn: sqlite3.Connection, decision: ControlDecision) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO control_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(decision.ts),
                decision.mode.value,
                decision.relay_requested,
                decision.relay_actual,
                decision.guard_reason,
                decision.stove_state.value if decision.stove_state else None,
                decision.stove_temp,
                decision.outdoor_temp,
                decision.heating_temp,
                decision.price_now,
                decision.plan_id,
            ),
        )


def latest_control_log(conn: sqlite3.Connection) -> ControlDecision | None:
    rows = conn.execute("SELECT * FROM control_log ORDER BY ts DESC LIMIT 1")
    return next(iter(_decisions(rows)), None)


def control_log_since(conn: sqlite3.Connection, since: datetime) -> list[ControlDecision]:
    rows = conn.execute("SELECT * FROM control_log WHERE ts >= ? ORDER BY ts", (to_iso(since),))
    return list(_decisions(rows))


def _decisions(rows: Iterable[sqlite3.Row]) -> Iterator[ControlDecision]:
    return (
        ControlDecision(
            ts=from_iso(row["ts"]),
            mode=ControlMode(row["mode"]),
            relay_requested=bool(row["relay_requested"]),
            relay_actual=_bool_or_none(row["relay_actual"]),
            guard_reason=row["guard_reason"],
            stove_state=StoveState(row["stove_state"]) if row["stove_state"] else None,
            stove_temp=row["stove_temp"],
            outdoor_temp=row["outdoor_temp"],
            heating_temp=row["heating_temp"],
            price_now=row["price_now"],
            plan_id=row["plan_id"],
        )
        for row in rows
    )


# --- export -----------------------------------------------------------------


def rows_between(
    conn: sqlite3.Connection, table: str, start: datetime | None, end: datetime | None
) -> list[sqlite3.Row]:
    """Rows of ``table`` whose time column lies in ``[start, end)``; open ends are unbounded.

    :raises KeyError: if ``table`` is not one of :data:`TABLES`.
    """
    time_column = TABLES[table]  # the whitelist keeps table names out of injection reach
    clauses, params = [], []
    if start is not None:
        clauses.append(f"{time_column} >= ?")
        params.append(to_iso(start))
    if end is not None:
        clauses.append(f"{time_column} < ?")
        params.append(to_iso(end))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"SELECT * FROM {table}{where} ORDER BY {time_column}", params).fetchall()


# --- summary ----------------------------------------------------------------


def table_summaries(conn: sqlite3.Connection) -> list[TableSummary]:
    """Row count and first/last timestamp for every table, in schema order."""
    summaries = []
    for table, time_column in TABLES.items():
        row = conn.execute(
            f"SELECT COUNT(*), MIN({time_column}), MAX({time_column}) FROM {table}"
        ).fetchone()
        summaries.append(
            TableSummary(table, row[0], _from_iso_or_none(row[1]), _from_iso_or_none(row[2]))
        )
    return summaries
