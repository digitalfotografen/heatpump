import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from tibber.exceptions import FatalHttpExceptionError

from heatpump import db, prices
from heatpump.config import TibberSettings
from heatpump.prices import PriceDataError

FIXTURES = Path(__file__).parent / "fixtures"
ZONE = ZoneInfo("Europe/Stockholm")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- parsing ----------------------------------------------------------------


def test_parse_two_days_gives_192_utc_quarters_in_order() -> None:
    quarters = prices.parse_price_info(load("tibber_two_days.json"), home_index=0)
    assert len(quarters) == 192
    assert all(q.start.utcoffset() == timedelta(0) for q in quarters)
    assert [q.start for q in quarters] == sorted(q.start for q in quarters)
    assert quarters[0].start == datetime(2026, 10, 19, 22, 0, tzinfo=UTC)  # 00:00 CEST
    assert all(q.slot_minutes == 15 for q in quarters)


def test_parse_keeps_energy_tax_total_and_currency() -> None:
    first = prices.parse_price_info(load("tibber_two_days.json"), home_index=0)[0]
    assert (first.energy, first.tax, first.total, first.currency) == (0.5, 0.5, 1.0, "SEK")


def test_parse_today_only_when_tomorrow_is_not_published() -> None:
    quarters = prices.parse_price_info(load("tibber_today_only.json"), home_index=0)
    assert len(quarters) == 96


@pytest.mark.parametrize(
    ("fixture", "today_quarters"), [("tibber_dst_autumn.json", 100), ("tibber_dst_spring.json", 92)]
)
def test_parse_dst_days_keep_every_quarter_on_a_continuous_utc_grid(
    fixture: str, today_quarters: int
) -> None:
    quarters = prices.parse_price_info(load(fixture), home_index=0)
    assert len(quarters) == today_quarters + 96
    steps = {b.start - a.start for a, b in zip(quarters, quarters[1:], strict=False)}
    assert steps == {timedelta(minutes=15)}


def test_parse_rejects_unknown_home_index() -> None:
    with pytest.raises(PriceDataError, match="home 1"):
        prices.parse_price_info(load("tibber_two_days.json"), home_index=1)


def test_parse_rejects_response_without_homes() -> None:
    with pytest.raises(PriceDataError):
        prices.parse_price_info({"viewer": {"homes": []}}, home_index=0)


def test_parse_rejects_home_without_subscription() -> None:
    with pytest.raises(PriceDataError, match="subscription"):
        prices.parse_price_info({"viewer": {"homes": [{"currentSubscription": None}]}}, 0)


def test_parse_rejects_non_quarter_hourly_entries() -> None:
    data = load("tibber_two_days.json")
    entries = data["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]["today"]
    del entries[1]  # an hourly feed has 60-minute steps; one missing entry makes a 30-minute step
    with pytest.raises(PriceDataError, match="15"):
        prices.parse_price_info(data, home_index=0)


def test_stored_prices_round_trip_through_the_database() -> None:
    quarters = prices.parse_price_info(load("tibber_two_days.json"), home_index=0)
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.upsert_prices(conn, quarters, fetched_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    found = db.prices_for_range(conn, quarters[0].start, quarters[-1].end)
    assert found.quarters == quarters
    assert found.gaps == []


# --- when to fetch ----------------------------------------------------------


def local(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


AFTER = time(14, 0)


def last_slot_of(day: int) -> datetime:
    return datetime(2026, 10, day, 23, 45, tzinfo=ZONE).astimezone(UTC)


def test_should_fetch_at_startup_with_empty_database() -> None:
    assert prices.should_fetch(local(8), None, AFTER)


def test_should_fetch_when_stored_prices_ended_yesterday() -> None:
    assert prices.should_fetch(local(0, 5), last_slot_of(19), AFTER)


def test_should_not_fetch_before_14_when_today_is_complete() -> None:
    assert not prices.should_fetch(local(13, 59), last_slot_of(20), AFTER)


def test_should_fetch_from_14_when_tomorrow_is_missing() -> None:
    assert prices.should_fetch(local(14, 0), last_slot_of(20), AFTER)


def test_should_not_fetch_when_tomorrow_is_already_stored() -> None:
    assert not prices.should_fetch(local(15), last_slot_of(21), AFTER)


def test_should_fetch_when_the_current_quarter_has_no_price() -> None:
    last = datetime(2026, 10, 20, 11, 45, tzinfo=ZONE).astimezone(UTC)  # covers up to 12:00
    assert prices.should_fetch(local(12, 0), last, AFTER)


# --- fetching ---------------------------------------------------------------


class FakeTibber:
    """Stands in for ``tibber.Tibber``; replays responses or raises queued errors."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.queries: list[str] = []
        self.closed = False

    async def execute(self, document: str) -> dict:
        self.queries.append(document)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close_connection(self) -> None:
        self.closed = True


@pytest.fixture
def fake_tibber(monkeypatch: pytest.MonkeyPatch):
    def install(outcomes: list[object]) -> FakeTibber:
        client = FakeTibber(outcomes)
        monkeypatch.setattr(prices, "_open_client", lambda token, session: client)
        return client

    return install


async def test_fetch_prices_requests_quarter_hourly_and_closes_the_client(fake_tibber) -> None:
    client = fake_tibber([load("tibber_two_days.json")])
    quarters = await prices.fetch_prices("token", home_index=0)
    assert len(quarters) == 192
    assert "QUARTER_HOURLY" in client.queries[0]
    assert client.closed


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(prices, "_sleep", instant)


async def test_retry_recovers_from_network_errors(fake_tibber, no_waiting) -> None:
    client = fake_tibber(
        [aiohttp.ClientConnectionError("down"), TimeoutError(), load("tibber_two_days.json")]
    )
    quarters = await prices.fetch_prices_with_retry(
        "token", 0, give_up_at=time(23, 0), zone=ZONE, clock=lambda: local(15)
    )
    assert len(quarters) == 192
    assert len(client.queries) == 3


async def test_retry_does_not_retry_a_rejected_token(fake_tibber, no_waiting) -> None:
    client = fake_tibber([FatalHttpExceptionError(401, "bad token")])
    with pytest.raises(FatalHttpExceptionError):
        await prices.fetch_prices_with_retry(
            "token", 0, give_up_at=time(23, 0), zone=ZONE, clock=lambda: local(15)
        )
    assert len(client.queries) == 1


async def test_retry_does_not_retry_unparseable_data(fake_tibber, no_waiting) -> None:
    client = fake_tibber([{"viewer": {"homes": []}}])
    with pytest.raises(PriceDataError):
        await prices.fetch_prices_with_retry(
            "token", 0, give_up_at=time(23, 0), zone=ZONE, clock=lambda: local(15)
        )
    assert len(client.queries) == 1


async def test_retry_gives_up_at_the_local_cutoff_time(fake_tibber, no_waiting) -> None:
    client = fake_tibber([TimeoutError(), TimeoutError()])
    with pytest.raises(TimeoutError):
        await prices.fetch_prices_with_retry(
            "token", 0, give_up_at=time(23, 0), zone=ZONE, clock=lambda: local(23, 1)
        )
    assert len(client.queries) == 1


def test_backoff_runs_from_one_to_thirty_two_minutes() -> None:
    waits = [prices.backoff_seconds(attempt) for attempt in range(1, 9)]
    assert waits == [60, 120, 240, 480, 960, 1920, 1920, 1920]


async def test_fetch_and_store_writes_quarters_and_returns_them(fake_tibber) -> None:
    fake_tibber([load("tibber_two_days.json")])
    conn = db.connect(":memory:")
    db.create_schema(conn)
    now = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
    stored = await prices.fetch_and_store(
        conn, "token", 0, give_up_at=time(23, 0), zone=ZONE, now=now
    )
    assert len(stored) == 192
    assert db.latest_price_slot(conn) == stored[-1].start


# --- scheduler ---------------------------------------------------------------


def test_next_fetch_is_now_when_due() -> None:
    now = datetime(2026, 10, 21, 8, tzinfo=ZONE)
    assert prices.next_fetch_time(now, None, AFTER) == now


def test_next_fetch_is_today_at_14_while_only_today_is_stored() -> None:
    now = datetime(2026, 10, 21, 8, tzinfo=ZONE)
    last = datetime(2026, 10, 21, 23, 45, tzinfo=ZONE)
    assert prices.next_fetch_time(now, last, AFTER) == datetime(2026, 10, 21, 14, tzinfo=ZONE)


def test_next_fetch_is_tomorrow_at_14_once_tomorrow_is_stored() -> None:
    now = datetime(2026, 10, 21, 15, tzinfo=ZONE)
    last = datetime(2026, 10, 22, 23, 45, tzinfo=ZONE)
    assert prices.next_fetch_time(now, last, AFTER) == datetime(2026, 10, 22, 14, tzinfo=ZONE)


class SchedulerRun:
    """Drives ``run_scheduler`` for a few rounds with a fake clock and a fake fetch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail: bool = False) -> None:
        self.conn = db.connect(":memory:")
        db.create_schema(self.conn)
        self.fetches = 0
        self.sleeps: list[float] = []
        self.fail = fail
        monkeypatch.setattr(prices, "fetch_and_store", self.fetch_and_store)

    async def fetch_and_store(self, conn, token, home_index, give_up_at, zone) -> list:
        self.fetches += 1
        if self.fail:
            raise PriceDataError("no data")
        start = datetime(2026, 10, 21, 0, tzinfo=ZONE)
        quarters = [prices.Quarter(start + timedelta(minutes=15 * i), 1.0) for i in range(192)]
        db.upsert_prices(conn, quarters, fetched_at=start)
        return quarters

    async def run(self, rounds: int) -> None:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            if len(self.sleeps) == rounds:
                raise StopAsyncIteration

        now = datetime(2026, 10, 21, 15, tzinfo=ZONE)
        with pytest.raises(StopAsyncIteration):
            await prices.run_scheduler(
                self.conn, "token", TibberSettings(), ZONE, sleep, clock=lambda: now
            )


async def test_the_scheduler_fetches_once_when_due_and_then_just_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SchedulerRun(monkeypatch)
    await run.run(rounds=3)
    assert run.fetches == 1
    assert run.sleeps == [prices.SCHEDULER_CHECK_SECONDS] * 3


async def test_a_failed_fetch_is_retried_later_not_every_minute(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run = SchedulerRun(monkeypatch, fail=True)
    await run.run(rounds=2)
    assert run.fetches == 2
    assert run.sleeps == [prices.FETCH_RETRY_SECONDS] * 2
    assert "Price fetch failed" in caplog.text
