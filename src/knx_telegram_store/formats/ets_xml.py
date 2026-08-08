"""Streaming reader/writer for the KNX ``CommunicationLog`` XML container.

The format (namespace ``http://knx.org/xml/telegrams/01``) is produced by the
ETS group/bus monitor export and by Gira IP-Router data loggers. This module
handles only the *container*: telegrams are exposed as raw cEMI frames — no
protocol decoding happens here, keeping the library dependency-free.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO
from xml.sax.saxutils import quoteattr

from defusedxml.ElementTree import iterparse

KNX_TELEGRAM_NAMESPACE = "http://knx.org/xml/telegrams/01"
_TELEGRAM_TAG = f"{{{KNX_TELEGRAM_NAMESPACE}}}Telegram"


@dataclass(frozen=True, slots=True)
class RawTelegramRecord:
    """A single telegram entry of a communication log, undecoded."""

    timestamp: datetime  # timezone-aware UTC
    service: str  # e.g. "L_Data.ind" | "L_Busmon.ind"
    raw_data: bytes  # cEMI frame as logged
    frame_format: str = "CommonEmi"


def _parse_timestamp(value: str) -> datetime:
    """Parses ISO timestamps as found in ETS/Gira logs into aware UTC datetimes.

    ETS writes 7-digit fractional seconds ("...21.8676962Z") which
    ``datetime.fromisoformat`` rejects before Python 3.11 truncation rules;
    normalize to microseconds explicitly.
    """
    ts = value.strip()
    if ts.endswith(("Z", "z")):
        ts = ts[:-1] + "+00:00"
    if "." in ts:
        base, _, rest = ts.partition(".")
        frac = rest
        offset = ""
        for sep in ("+", "-"):
            idx = rest.find(sep)
            if idx != -1:
                frac, offset = rest[:idx], rest[idx:]
                break
        frac = (frac[:6]).ljust(6, "0")
        ts = f"{base}.{frac}{offset}"
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# Gira data loggers write *local* time with a "Z" suffix and declare the real
# offset in a comment: <!-- timezone offset +01:00 hour -->
_TIMEZONE_COMMENT = re.compile(r"timezone\s+offset\s+([+-])(\d{2}):(\d{2})")


def iter_communication_log(source: str | IO[bytes]) -> Iterator[RawTelegramRecord]:
    """Incrementally parses a communication log, yielding telegram records.

    ``source`` is a file path or a binary file object (e.g. a zip entry
    stream). Memory use is constant regardless of file size. Non-telegram
    elements (``RecordStart``/``RecordStop``) are skipped; telegram entries
    without ``RawData`` raise ``ValueError``. A Gira-style
    ``timezone offset`` comment is honored: the declared offset is subtracted
    from the (falsely "Z"-suffixed) local timestamps to yield true UTC.
    """
    utc_correction = timedelta(0)
    for _, element in iterparse(source, events=("end", "comment")):
        if element.tag is ET.Comment:
            match = _TIMEZONE_COMMENT.search(element.text or "")
            if match:
                sign = 1 if match.group(1) == "+" else -1
                utc_correction = sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
            continue
        if element.tag != _TELEGRAM_TAG:
            continue
        timestamp = element.get("Timestamp")
        raw_data = element.get("RawData")
        if timestamp is None or raw_data is None:
            raise ValueError(f"Telegram element missing Timestamp/RawData: {ET.tostring(element, encoding='unicode')}")
        yield RawTelegramRecord(
            timestamp=_parse_timestamp(timestamp) - utc_correction,
            service=element.get("Service", "L_Data.ind"),
            raw_data=bytes.fromhex(raw_data),
            frame_format=element.get("FrameFormat", "CommonEmi"),
        )
        element.clear()


def _format_timestamp(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


COMMUNICATION_LOG_HEADER = (
    f'<?xml version="1.0" encoding="utf-8"?>\n<CommunicationLog xmlns="{KNX_TELEGRAM_NAMESPACE}">\n'
)
COMMUNICATION_LOG_FOOTER = "</CommunicationLog>\n"


def format_telegram_element(record: RawTelegramRecord, *, connection_name: str = "") -> str:
    """Renders one record as an ETS-compatible ``<Telegram />`` element line."""
    connection_attr = f" ConnectionName={quoteattr(connection_name)}" if connection_name else ""
    return (
        f'  <Telegram Timestamp="{_format_timestamp(record.timestamp)}"{connection_attr}'
        f' Service="{record.service}" FrameFormat="{record.frame_format}"'
        f' RawData="{record.raw_data.hex().upper()}" />\n'
    )


def write_communication_log(
    records: Iterable[RawTelegramRecord],
    destination: IO[str],
    *,
    connection_name: str = "",
) -> int:
    """Streams records to ``destination`` as ETS-compatible XML.

    Returns the number of telegrams written. ``records`` may be a generator;
    output is written incrementally.
    """
    destination.write(COMMUNICATION_LOG_HEADER)
    count = 0
    for record in records:
        destination.write(format_telegram_element(record, connection_name=connection_name))
        count += 1
    destination.write(COMMUNICATION_LOG_FOOTER)
    return count
