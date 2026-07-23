"""Host-agnostic MCP tool functions for the telegram store.

These are plain async functions operating on a :class:`TelegramStore`, with
frozen, JSON-serialisable dataclass inputs and outputs. They carry **no
dependency on any MCP SDK, Home Assistant or a web framework** — each consumer
(SpectrumKNX, Home Assistant, …) wraps them into its own MCP transport.

See :mod:`knx_telegram_store.mcp.tools` for the tool functions and
:mod:`knx_telegram_store.mcp.types` for the input/output models.
"""

from .tools import (
    count_telegrams,
    get_last_values,
    get_store_capabilities,
    get_store_stats,
    query_telegrams,
)
from .types import (
    CountResult,
    LastValuesInput,
    QueryTelegramsInput,
    QueryTelegramsResult,
    StoreCapabilitiesResult,
    StoreStatsResult,
    TelegramSummary,
)

__all__ = [
    # tools
    "query_telegrams",
    "get_last_values",
    "get_store_stats",
    "get_store_capabilities",
    "count_telegrams",
    # types
    "QueryTelegramsInput",
    "QueryTelegramsResult",
    "LastValuesInput",
    "StoreStatsResult",
    "StoreCapabilitiesResult",
    "CountResult",
    "TelegramSummary",
]
