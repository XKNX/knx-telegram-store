"""Input/output dataclasses for the telegram-store MCP tools.

All fields are JSON-native (timestamps are ISO-8601 strings), so a consumer can
build inputs directly from tool arguments and serialise outputs with
:func:`dataclasses.asdict` without custom encoders.

Input fields carry their human-readable description as ``dataclasses.field``
metadata under the ``"description"`` key. This keeps the library free of any
schema/validation dependency while letting a consumer surface per-parameter
descriptions in its tool schema via ``dataclasses.fields(...)`` metadata.
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

    start_time: str | None = field(
        default=None,
        metadata={"description": "Inclusive lower bound timestamp in ISO-8601 format."},
    )
    end_time: str | None = field(
        default=None,
        metadata={"description": "Inclusive upper bound timestamp in ISO-8601 format."},
    )
    sources: list[str] = field(
        default_factory=list,
        metadata={"description": "Filter by source individual address(es)."},
    )
    destinations: list[str] = field(
        default_factory=list,
        metadata={"description": "Filter by destination group address(es)."},
    )
    telegram_types: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                'Filter by telegram type, accepts full name ("GroupValueWrite") or '
                'short name ("Write"/"Read"/"Response").'
            )
        },
    )
    directions: list[str] = field(
        default_factory=list,
        metadata={"description": 'Filter by direction ("Incoming" or "Outgoing").'},
    )
    dpt_mains: list[int] = field(
        default_factory=list,
        metadata={"description": "Filter by DPT main number(s), e.g. [9]; matches every subtype of each."},
    )
    dpts: list[str] = field(
        default_factory=list,
        metadata={"description": 'Filter by specific DPT main or main.sub strings, e.g. ["9.001"].'},
    )
    delta_before_ms: int = field(
        default=0,
        metadata={"description": "Time-delta context window before each matching telegram in milliseconds."},
    )
    delta_after_ms: int = field(
        default=0,
        metadata={"description": "Time-delta context window after each matching telegram in milliseconds."},
    )
    limit: int = field(
        default=100,
        metadata={"description": "Maximum number of results to return."},
    )
    offset: int = field(
        default=0,
        metadata={"description": "Number of results to skip, for pagination."},
    )
    order_descending: bool = field(
        default=True,
        metadata={"description": "Whether to order results newest first (default: True)."},
    )


@dataclass(frozen=True, slots=True)
class LastValuesInput:
    """Filter for :func:`~knx_telegram_store.mcp.tools.get_last_values`."""

    destinations: list[str] = field(
        default_factory=list,
        metadata={"description": "Restrict to these destination group addresses; empty means all."},
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
