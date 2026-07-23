"""Input/output dataclasses for the telegram-store MCP tools.

All fields are JSON-native (timestamps are ISO-8601 strings), so a consumer can
build inputs directly from tool arguments and serialise outputs with
:func:`dataclasses.asdict` without custom encoders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class QueryTelegramsInput:
    """Filters for :func:`~knx_telegram_store.mcp.tools.query_telegrams`.

    Within a category the values are OR-ed; across categories they are AND-ed.
    An empty list means "no restriction".
    """

    start_time: str | None = None  # inclusive lower bound, ISO-8601
    end_time: str | None = None  # inclusive upper bound, ISO-8601
    sources: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    telegram_types: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    dpt_mains: list[int] = field(default_factory=list)
    limit: int = 100
    offset: int = 0
    order_descending: bool = True  # newest first by default


@dataclass(frozen=True, slots=True)
class LastValuesInput:
    """Filter for :func:`~knx_telegram_store.mcp.tools.get_last_values`."""

    destinations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TelegramSummary:
    """A JSON-serialisable view of a stored telegram."""

    timestamp: str  # ISO-8601, UTC
    source: str
    destination: str
    telegramtype: str
    direction: str
    dpt: str | None
    value: bool | str | int | float | dict[str, Any] | None
    value_numeric: float | None
    raw_data: str | None
    source_name: str
    destination_name: str


@dataclass(frozen=True, slots=True)
class QueryTelegramsResult:
    """Result of :func:`~knx_telegram_store.mcp.tools.query_telegrams`."""

    telegrams: list[TelegramSummary]
    total_count: int
    limit_reached: bool


@dataclass(frozen=True, slots=True)
class StoreStatsResult:
    """Result of :func:`~knx_telegram_store.mcp.tools.get_store_stats`."""

    telegram_count: int
    oldest_timestamp: str | None
    newest_timestamp: str | None
    size_bytes: int | None
    backend: str
    retention_days: int | None


@dataclass(frozen=True, slots=True)
class StoreCapabilitiesResult:
    """Result of :func:`~knx_telegram_store.mcp.tools.get_store_capabilities`."""

    supports_time_range: bool
    supports_time_delta: bool
    supports_pagination: bool
    supports_count: bool
    supports_size_stats: bool
    supports_optimize: bool
    read_only: bool
    max_storage: int | None


@dataclass(frozen=True, slots=True)
class CountResult:
    """Result of :func:`~knx_telegram_store.mcp.tools.count_telegrams`."""

    count: int
