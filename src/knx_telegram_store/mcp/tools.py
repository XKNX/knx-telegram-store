"""MCP tool functions for the telegram store.

Each function takes a :class:`~knx_telegram_store.store.TelegramStore` and a
typed input, and returns a JSON-serialisable dataclass. They are transport- and
host-agnostic: no MCP SDK, Home Assistant or web-framework imports.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..model import StoredTelegram
from ..query import TelegramQuery
from ..store import TelegramStore
from .types import (
    CountResult,
    LastValuesInput,
    QueryTelegramsInput,
    QueryTelegramsResult,
    StoreCapabilitiesResult,
    StoreStatsResult,
    TelegramSummary,
)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from err


def _iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 in UTC. The SQLite backend round-trips timestamps naive; they
    are UTC by convention, so tag them so consumers get an unambiguous offset."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _format_dpt(dpt_main: int | None, dpt_sub: int | None) -> str | None:
    """Render a DPT as ``main.sub`` (sub zero-padded), or just ``main``."""
    if dpt_main is None:
        return None
    if dpt_sub is None:
        return str(dpt_main)
    return f"{dpt_main}.{dpt_sub:03d}"


def _summarize(t: StoredTelegram) -> TelegramSummary:
    return TelegramSummary(
        timestamp=_iso_utc(t.timestamp),
        source=t.source,
        destination=t.destination,
        telegramtype=t.telegramtype,
        direction=t.direction,
        dpt=_format_dpt(t.dpt_main, t.dpt_sub),
        value=t.value,
        value_numeric=t.value_numeric,
        raw_data=t.raw_data,
        source_name=t.source_name,
        destination_name=t.destination_name,
    )


async def query_telegrams(store: TelegramStore, input: QueryTelegramsInput) -> QueryTelegramsResult:
    """Search historical telegrams with multi-field filtering."""
    query = TelegramQuery(
        sources=list(input.sources),
        destinations=list(input.destinations),
        telegram_types=list(input.telegram_types),
        directions=list(input.directions),
        dpt_mains=list(input.dpt_mains),
        start_time=_parse_dt(input.start_time),
        end_time=_parse_dt(input.end_time),
        limit=input.limit,
        offset=input.offset,
        order_descending=input.order_descending,
    )
    result = await store.query(query)
    return QueryTelegramsResult(
        telegrams=[_summarize(t) for t in result.telegrams],
        total_count=result.total_count,
        limit_reached=result.limit_reached,
    )


async def get_last_values(store: TelegramStore, input: LastValuesInput) -> list[TelegramSummary]:
    """Return the most recent telegram for each unique destination GA.

    With ``destinations`` set, only those group addresses are returned.
    """
    telegrams = await store.get_last_unique_telegrams()
    if input.destinations:
        wanted = set(input.destinations)
        telegrams = [t for t in telegrams if t.destination in wanted]
    return [_summarize(t) for t in telegrams]


async def get_store_stats(store: TelegramStore) -> StoreStatsResult:
    """Report the store's contents and storage footprint."""
    stats = await store.get_stats()
    return StoreStatsResult(
        telegram_count=stats.telegram_count,
        oldest_timestamp=_iso_utc(stats.oldest_timestamp),
        newest_timestamp=_iso_utc(stats.newest_timestamp),
        size_bytes=stats.size_bytes,
        backend=stats.backend,
        retention_days=stats.retention_days,
    )


async def get_store_capabilities(store: TelegramStore) -> StoreCapabilitiesResult:
    """Report what the store backend supports natively."""
    caps = store.capabilities
    return StoreCapabilitiesResult(
        supports_time_range=caps.supports_time_range,
        supports_time_delta=caps.supports_time_delta,
        supports_pagination=caps.supports_pagination,
        supports_count=caps.supports_count,
        supports_size_stats=caps.supports_size_stats,
        supports_optimize=caps.supports_optimize,
        read_only=caps.read_only,
        max_storage=caps.max_storage,
    )


async def count_telegrams(store: TelegramStore) -> CountResult:
    """Return the total number of stored telegrams."""
    return CountResult(count=await store.count())
