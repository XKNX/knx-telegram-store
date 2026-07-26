"""Input/output dataclasses for the telegram-store MCP tools.

All fields are JSON-native (timestamps are ISO-8601 strings), so a consumer can
build inputs directly from tool arguments and serialise outputs with
:func:`dataclasses.asdict` without custom encoders.

Input fields carry their human-readable description as :data:`typing.Annotated`
metadata (a plain string). This keeps the library free of any schema/validation
dependency while letting a consumer surface per-parameter descriptions in its
tool schema via ``typing.get_type_hints(..., include_extras=True)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any


@dataclass(frozen=True, slots=True)
class QueryTelegramsInput:
    """Filters for :func:`~knx_telegram_store.mcp.tools.query_telegrams`.

    Within a category the values are OR-ed; across categories they are AND-ed.
    An empty list means "no restriction".
    """

    start_time: Annotated[str | None, "Inclusive lower time bound, ISO-8601."] = None
    end_time: Annotated[str | None, "Inclusive upper time bound, ISO-8601."] = None
    sources: Annotated[list[str], "Source individual addresses to match."] = field(default_factory=list)
    destinations: Annotated[list[str], "Destination group addresses to match."] = field(default_factory=list)
    telegram_types: Annotated[
        list[str],
        'Telegram types: full names ("GroupValueWrite") or short "Write"/"Read"/"Response".',
    ] = field(default_factory=list)
    directions: Annotated[list[str], 'Directions to match, e.g. "Incoming"/"Outgoing".'] = field(default_factory=list)
    dpt_mains: Annotated[list[int], "DPT main numbers; matches every subtype of each."] = field(default_factory=list)
    dpts: Annotated[list[str], 'Specific DPTs as "main" or "main.sub" strings, e.g. "9" or "9.001".'] = field(
        default_factory=list
    )
    delta_before_ms: Annotated[int, "Include telegrams up to this many ms before each match (context window)."] = 0
    delta_after_ms: Annotated[int, "Include telegrams up to this many ms after each match (context window)."] = 0
    limit: Annotated[int, "Maximum number of telegrams to return."] = 100
    offset: Annotated[int, "Number of telegrams to skip, for pagination."] = 0
    order_descending: Annotated[bool, "Newest first when true."] = True


@dataclass(frozen=True, slots=True)
class LastValuesInput:
    """Filter for :func:`~knx_telegram_store.mcp.tools.get_last_values`."""

    destinations: Annotated[list[str], "Restrict to these destination group addresses; empty means all."] = field(
        default_factory=list
    )


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
    offset: int
    next_offset: int | None  # pass as ``offset`` for the next page; ``None`` when exhausted
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
