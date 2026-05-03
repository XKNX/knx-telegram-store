from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import StoredTelegram


@dataclass(frozen=True, kw_only=True, slots=True)
class TelegramQuery:
    """Declarative query for telegram retrieval.

    Filter semantics:
    - Empty list = no restriction (pass-through)
    - Within a category = OR logic (any match passes)
    - Across categories = AND logic (must pass all active categories)
    """

    # ── Multi-value filters (OR within, AND across) ───────────────
    sources: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    telegram_types: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    dpt_mains: list[int] = field(default_factory=list)

    # ── Time range ────────────────────────────────────────────────
    start_time: datetime | None = None
    end_time: datetime | None = None

    # ── Time-delta context window (milliseconds) ──────────────────
    #    When set, rows matching the filters are found first, then
    #    ALL rows within ±delta of any matching row's timestamp are
    #    included — even if they don't match the filters themselves.
    delta_before_ms: int = 0
    delta_after_ms: int = 0

    # ── Pagination ────────────────────────────────────────────────
    limit: int = 25_000
    offset: int = 0

    # ── Ordering ──────────────────────────────────────────────────
    order_descending: bool = True  # newest first by default


@dataclass(frozen=True, kw_only=True, slots=True)
class TelegramQueryResult:
    """Result of a telegram query."""

    telegrams: list[StoredTelegram]
    total_count: int
    limit_reached: bool  # True = more results exist beyond limit
