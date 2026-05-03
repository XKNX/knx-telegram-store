from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class StoredTelegram:
    """A KNX telegram in its stored/serialized form."""

    # ── Core identity ─────────────────────────────────────────────
    timestamp: datetime  # timezone-aware UTC

    # ── Addressing ────────────────────────────────────────────────
    source: str  # Individual address, e.g. "1.2.3"
    destination: str  # Group address, e.g. "1/2/3"

    # ── Telegram classification ───────────────────────────────────
    telegramtype: str  # "GroupValueWrite" | "GroupValueRead" | "GroupValueResponse"
    direction: str  # "Incoming" | "Outgoing"

    # ── Payload ───────────────────────────────────────────────────
    payload: int | tuple[int, ...] | None = None  # Raw KNX payload (DPTBinary int or DPTArray tuple)

    # ── DPT metadata ─────────────────────────────────────────────
    dpt_main: int | None = None
    dpt_sub: int | None = None
    dpt_name: str | None = None
    unit: str | None = None

    # ── Decoded value (consumer-enriched at write time) ───────────
    value: bool | str | int | float | dict[str, Any] | None = None

    # ── Numeric value for time-series queries (SQL backends) ──────
    value_numeric: float | None = None

    # ── Raw bytes (hex-encoded string for JSON safety) ────────────
    raw_data: str | None = None  # e.g. "0a1b2c"

    # ── Security ──────────────────────────────────────────────────
    data_secure: bool | None = None

    # ── Display names (consumer-enriched at write time) ───────────
    source_name: str = ""
    destination_name: str = ""
