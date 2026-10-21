"""Tibber prices: fetch quarter-hour prices, retry on network trouble, decide when to fetch.

Only this module talks to Tibber. The planner reads the stored quarters from the database.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import tibber
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type
from tibber.exceptions import HttpExceptionError, RetryableHttpExceptionError

from heatpump import db
from heatpump.config import TibberSettings
from heatpump.models import Quarter

logger = logging.getLogger(__name__)

USER_AGENT = "heatpump-controller"
SLOT_MINUTES = 15
BACKOFF_FIRST_SECONDS = 60
BACKOFF_MAX_SECONDS = 32 * 60
SCHEDULER_CHECK_SECONDS = 60
FETCH_RETRY_SECONDS = 10 * 60

PRICE_QUERY = """
{
  viewer {
    homes {
      currentSubscription {
        priceInfo(resolution: QUARTER_HOURLY) {
          today { startsAt energy tax total currency }
          tomorrow { startsAt energy tax total currency }
        }
      }
    }
  }
}
"""

# Errors worth retrying: the network or Tibber is down, or we are rate limited.
# A rejected token or an unexpected response will not fix itself.
RETRYABLE_ERRORS = (aiohttp.ClientError, TimeoutError, RetryableHttpExceptionError)


class PriceDataError(ValueError):
    """Tibber answered, but not with quarter-hour prices we can use."""


# Everything ``fetch_prices_with_retry`` can end in; the CLI reports these instead of crashing.
FETCH_ERRORS = (PriceDataError, HttpExceptionError, aiohttp.ClientError, TimeoutError)


def parse_price_info(data: dict, home_index: int) -> list[Quarter]:
    """Turn a ``PRICE_QUERY`` response into UTC quarters, today's first, then tomorrow's.

    :param data: the ``data`` part of the GraphQL response.
    :param home_index: which home on the account to read.
    :raises PriceDataError: if the home or its prices are missing, or the entries are not
        consecutive 15-minute slots (for example an hourly feed).
    """
    homes = (data.get("viewer") or {}).get("homes") or []
    if home_index >= len(homes):
        raise PriceDataError(f"Tibber account has no home {home_index} ({len(homes)} found)")
    subscription = homes[home_index].get("currentSubscription")
    if not subscription:
        raise PriceDataError(f"Tibber home {home_index} has no current subscription")
    price_info = subscription["priceInfo"]
    entries = [*price_info["today"], *price_info["tomorrow"]]
    quarters = [_quarter(entry) for entry in entries]
    _require_consecutive_quarters(quarters)
    return quarters


def _quarter(entry: dict) -> Quarter:
    start = datetime.fromisoformat(entry["startsAt"])
    if start.tzinfo is None:
        raise PriceDataError(f"Tibber slot {entry['startsAt']} has no UTC offset")
    return Quarter(
        start=start.astimezone(UTC),
        energy=entry["energy"],
        tax=entry["tax"],
        total=entry["total"],
        currency=entry["currency"],
        slot_minutes=SLOT_MINUTES,
    )


def _require_consecutive_quarters(quarters: list[Quarter]) -> None:
    """Refuse hourly or gappy data: the planner assumes one price per 15 minutes."""
    step = timedelta(minutes=SLOT_MINUTES)
    for before, after in zip(quarters, quarters[1:], strict=False):
        if after.start - before.start != step:
            raise PriceDataError(
                f"Expected {SLOT_MINUTES}-minute steps, got {after.start - before.start} "
                f"between {before.start} and {after.start}"
            )


def _open_client(token: str, session: aiohttp.ClientSession) -> tibber.Tibber:
    return tibber.Tibber(token, websession=session, user_agent=USER_AGENT)


async def fetch_prices(token: str, home_index: int) -> list[Quarter]:
    """Fetch today's and (once published) tomorrow's quarters from Tibber, once.

    :raises PriceDataError: if the response is unusable.
    :raises aiohttp.ClientError: on network failure.
    :raises tibber.exceptions.HttpExceptionError: if Tibber rejects the request.
    """
    async with aiohttp.ClientSession() as session:
        client = _open_client(token, session)
        try:
            data = await client.execute(PRICE_QUERY)
        finally:
            await client.close_connection()
    if data is None:
        raise PriceDataError("Tibber returned no data")
    return parse_price_info(data, home_index)


def backoff_seconds(attempt: int) -> int:
    """Wait after failed attempt number ``attempt``: 1, 2, 4 ... capped at 32 minutes."""
    return min(BACKOFF_FIRST_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def fetch_prices_with_retry(
    token: str,
    home_index: int,
    give_up_at: time,
    zone: ZoneInfo,
    clock: Callable[[], datetime] | None = None,
) -> list[Quarter]:
    """Fetch prices, retrying network errors with backoff until ``give_up_at`` local time.

    :param clock: returns the current time; replaced in tests.
    :raises: the last error once the cut-off passes or if the error is not retryable.
    """
    now = clock or (lambda: datetime.now(zone))

    def past_cut_off(state: RetryCallState) -> bool:
        return now().astimezone(zone).time() >= give_up_at

    def log_and_wait(state: RetryCallState) -> float:
        seconds = backoff_seconds(state.attempt_number)
        logger.warning(
            "Tibber fetch failed (%r); retrying in %d s", state.outcome.exception(), seconds
        )
        return seconds

    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        stop=past_cut_off,
        wait=log_and_wait,
        sleep=lambda seconds: _sleep(seconds),
        reraise=True,
    ):
        with attempt:
            return await fetch_prices(token, home_index)
    raise AssertionError("unreachable: AsyncRetrying either returns or reraises")


async def fetch_and_store(
    conn: sqlite3.Connection,
    token: str,
    home_index: int,
    give_up_at: time,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> list[Quarter]:
    """Fetch prices with retry and upsert them; returns the stored quarters."""
    quarters = await fetch_prices_with_retry(token, home_index, give_up_at, zone)
    db.upsert_prices(conn, quarters, fetched_at=now or datetime.now(UTC))
    logger.info("Stored %d price quarters", len(quarters))
    return quarters


def should_fetch(now_local: datetime, latest_slot: datetime | None, fetch_after: time) -> bool:
    """True if the current quarter has no price, or tomorrow's are due and still missing.

    :param now_local: current time in the configured timezone.
    :param latest_slot: start of the last stored quarter (``db.latest_price_slot``).
    :param fetch_after: local time from which tomorrow's prices are expected.
    """
    if latest_slot is None:
        return True
    slot = timedelta(minutes=SLOT_MINUTES)
    if latest_slot + slot <= now_local:
        return True
    tomorrow_missing = latest_slot.astimezone(now_local.tzinfo).date() <= now_local.date()
    return tomorrow_missing and now_local.time() >= fetch_after


def next_fetch_time(
    now_local: datetime, latest_slot: datetime | None, fetch_after: time
) -> datetime:
    """When the scheduler will next fetch: now if due, otherwise the next ``fetch_after``."""
    if should_fetch(now_local, latest_slot, fetch_after):
        return now_local
    covers_tomorrow = latest_slot is not None and (
        latest_slot.astimezone(now_local.tzinfo).date() > now_local.date()
    )
    day = now_local.date() + timedelta(days=1 if covers_tomorrow else 0)
    return datetime.combine(day, fetch_after, tzinfo=now_local.tzinfo)


async def fetch_if_due(
    conn: sqlite3.Connection,
    token: str,
    settings: TibberSettings,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> float:
    """Fetch if :func:`should_fetch` says so; return the seconds to wait before checking again.

    A fetch that fails, or that leaves prices still due (tomorrow's are not published yet), is
    retried after ``FETCH_RETRY_SECONDS`` so Tibber is not asked every minute.
    """
    after = settings.fetch_after_local_time
    if not should_fetch(now or datetime.now(zone), db.latest_price_slot(conn), after):
        return SCHEDULER_CHECK_SECONDS
    try:
        await fetch_and_store(
            conn, token, settings.home_index, settings.fetch_give_up_local_time, zone
        )
    except (*FETCH_ERRORS, OSError) as error:
        logger.warning("Price fetch failed: %s", error)
        return FETCH_RETRY_SECONDS
    due_again = should_fetch(now or datetime.now(zone), db.latest_price_slot(conn), after)
    return FETCH_RETRY_SECONDS if due_again else SCHEDULER_CHECK_SECONDS


async def run_scheduler(
    conn: sqlite3.Connection,
    token: str,
    settings: TibberSettings,
    zone: ZoneInfo,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Fetch prices whenever they are due, forever; failures are logged, never raised."""
    while True:
        wait = await fetch_if_due(conn, token, settings, zone, clock() if clock else None)
        await sleep(wait)
