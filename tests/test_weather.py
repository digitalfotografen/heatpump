import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from heatpump import db, weather
from heatpump.config import LocationSettings, WeatherSettings
from heatpump.models import TemperatureReading

NOW = datetime(2026, 10, 20, 20, 20, tzinfo=UTC)
RESPONSE = {
    "latitude": 59.33,
    "longitude": 18.07,
    "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C"},
    "current": {"time": "2026-10-20T20:15", "interval": 900, "temperature_2m": 7.7},
}


def provider_answering(handler) -> weather.OpenMeteoProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return weather.OpenMeteoProvider(59.33, 18.07, client=client)


def provider_returning(body: object, status: int = 200) -> weather.OpenMeteoProvider:
    return provider_answering(lambda request: httpx.Response(status, json=body))


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.create_schema(connection)
    return connection


# --- parsing ----------------------------------------------------------------


def test_parse_current_gives_a_utc_reading_with_the_source() -> None:
    reading = weather.parse_current(RESPONSE)
    assert reading == TemperatureReading(
        datetime(2026, 10, 20, 20, 15, tzinfo=UTC), 7.7, "open-meteo"
    )


def test_parse_current_accepts_a_negative_temperature() -> None:
    body = {"current": {"time": "2026-02-19T06:00", "temperature_2m": -12.4}}
    assert weather.parse_current(body).temperature == -12.4


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"current": None},
        {"current": {"time": "2026-10-20T20:15"}},
        {"current": {"temperature_2m": 7.7}},
        {"current": {"time": "yesterday", "temperature_2m": 7.7}},
        {"current": {"time": "2026-10-20T20:15", "temperature_2m": None}},
        {"current": {"time": "2026-10-20T20:15", "temperature_2m": "warm"}},
        [],
    ],
)
def test_unusable_responses_are_rejected(body: object) -> None:
    with pytest.raises(weather.WeatherError):
        weather.parse_current(body)


# --- the provider ------------------------------------------------------------------


async def test_fetch_asks_open_meteo_for_the_current_temperature_in_utc() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    reading = await provider_answering(handler).fetch()
    assert reading.temperature == 7.7
    params = seen[0].url.params
    assert seen[0].url.host == "api.open-meteo.com"
    assert seen[0].url.path == "/v1/forecast"
    assert (params["latitude"], params["longitude"]) == ("59.33", "18.07")
    assert params["current"] == "temperature_2m"
    assert params["timezone"] == "UTC"


async def test_a_server_error_becomes_a_weather_error() -> None:
    with pytest.raises(weather.WeatherError, match="500"):
        await provider_returning({"error": True}, status=500).fetch()


async def test_a_network_error_becomes_a_weather_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(weather.WeatherError, match="no route"):
        await provider_answering(handler).fetch()


async def test_a_body_that_is_not_json_becomes_a_weather_error() -> None:
    provider = provider_answering(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(weather.WeatherError):
        await provider.fetch()


def test_the_provider_is_built_from_the_location_settings() -> None:
    provider = weather.OpenMeteoProvider.from_settings(
        LocationSettings(latitude=1.5, longitude=2.5)
    )
    assert (provider.latitude, provider.longitude) == (1.5, 2.5)


# --- storing -------------------------------------------------------------------------


async def test_fetch_and_store_saves_the_reading(conn: sqlite3.Connection) -> None:
    reading = await weather.fetch_and_store(conn, provider_returning(RESPONSE))
    assert db.latest_outdoor_reading(conn) == reading


async def test_a_failed_fetch_stores_nothing(conn: sqlite3.Connection) -> None:
    with pytest.raises(weather.WeatherError):
        await weather.fetch_and_store(conn, provider_returning({}, status=503))
    assert db.latest_outdoor_reading(conn) is None


async def test_fetching_the_same_observation_twice_keeps_one_row(
    conn: sqlite3.Connection,
) -> None:
    provider = provider_returning(RESPONSE)
    await weather.fetch_and_store(conn, provider)
    await weather.fetch_and_store(conn, provider)
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


# --- staleness (informational) ---------------------------------------------------------


def test_a_recent_reading_is_not_stale() -> None:
    reading = TemperatureReading(NOW - timedelta(minutes=179), 5.0)
    assert not weather.is_stale(reading, NOW, WeatherSettings())


def test_a_reading_older_than_the_limit_is_stale() -> None:
    reading = TemperatureReading(NOW - timedelta(minutes=181), 5.0)
    assert weather.is_stale(reading, NOW, WeatherSettings())


def test_no_reading_is_stale() -> None:
    assert weather.is_stale(None, NOW, WeatherSettings())


# --- polling -----------------------------------------------------------------------------


class StopPolling(Exception):
    pass


async def test_the_poller_fetches_on_the_configured_interval(conn: sqlite3.Connection) -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            raise StopPolling

    with pytest.raises(StopPolling):
        await weather.run_poller(provider_returning(RESPONSE), conn, WeatherSettings(), sleep)
    assert delays == [30 * 60] * 3
    assert db.latest_outdoor_reading(conn) is not None


async def test_the_poller_survives_a_failed_fetch(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, json=RESPONSE)])
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise StopPolling

    provider = provider_answering(lambda request: next(answers))
    with pytest.raises(StopPolling):
        await weather.run_poller(provider, conn, WeatherSettings(), sleep)
    assert db.latest_outdoor_reading(conn) is not None
    assert "503" in caplog.text
