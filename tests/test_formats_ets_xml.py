import io
from datetime import UTC, datetime

import pytest

from knx_telegram_store.formats import RawTelegramRecord, iter_communication_log, write_communication_log

ETS6_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<CommunicationLog xmlns="http://knx.org/xml/telegrams/01">
  <RecordStart Timestamp="2026-06-27T13:54:21.8676962Z" ConnectionName="Test" Mode="LinkLayer" />
  <Telegram Timestamp="2026-06-27T13:54:24.4096945Z" ConnectionName="Test" Service="L_Data.ind" FrameFormat="CommonEmi" RawData="2900BCD01129010302008019" TransportSecurity="true" />
  <Telegram Timestamp="2026-06-27T13:54:25.9198766Z" Service="L_Data.ind" FrameFormat="CommonEmi" RawData="2900BCE010180504010081" />
  <RecordStop Timestamp="2026-06-27T13:56:00Z" />
</CommunicationLog>
"""

GIRA_SAMPLE = """<CommunicationLog xmlns="http://knx.org/xml/telegrams/01">
<!-- timezone offset +01:00 hour -->
  <Telegram Timestamp="2026-03-05T00:00:38.994Z" Service="L_Busmon.ind" FrameFormat="CommonEmi" RawData="2B090301030604BAD64193BC1012143DE3008000C8C3" />
  <Telegram Timestamp="2026-03-05T00:00:38.999Z" Service="L_Busmon.ind" FrameFormat="CommonEmi" RawData="2B090301040604BAD6A0ACCC" />
</CommunicationLog>
"""


def _parse(text: str) -> list[RawTelegramRecord]:
    return list(iter_communication_log(io.BytesIO(text.encode())))


def test_parse_ets6_export():
    records = _parse(ETS6_SAMPLE)
    assert len(records) == 2  # RecordStart/RecordStop skipped
    first = records[0]
    assert first.service == "L_Data.ind"
    assert first.frame_format == "CommonEmi"
    assert first.raw_data == bytes.fromhex("2900BCD01129010302008019")
    # 7-digit fractional seconds truncated to microseconds, UTC-aware
    assert first.timestamp == datetime(2026, 6, 27, 13, 54, 24, 409694, tzinfo=UTC)


def test_parse_gira_log_applies_timezone_comment():
    records = _parse(GIRA_SAMPLE)
    assert len(records) == 2
    assert records[0].service == "L_Busmon.ind"
    # "2026-03-05T00:00:38.994Z" is actually local (+01:00) per the comment
    assert records[0].timestamp == datetime(2026, 3, 4, 23, 0, 38, 994000, tzinfo=UTC)
    assert records[1].raw_data.endswith(b"\xcc")


def test_parse_missing_rawdata_raises():
    broken = ETS6_SAMPLE.replace('RawData="2900BCD01129010302008019" ', "", 1)
    with pytest.raises(ValueError, match="missing Timestamp/RawData"):
        _parse(broken)


def test_write_round_trip():
    records = _parse(ETS6_SAMPLE)
    out = io.StringIO()
    written = write_communication_log(records, out, connection_name='My "Log"')
    assert written == 2
    text = out.getvalue()
    assert "ConnectionName='My \"Log\"'" in text
    reparsed = list(iter_communication_log(io.BytesIO(text.encode())))
    assert reparsed == records


def test_write_empty_log_is_valid():
    out = io.StringIO()
    assert write_communication_log([], out) == 0
    assert list(iter_communication_log(io.BytesIO(out.getvalue().encode()))) == []


def test_parse_xxe_payload_raises():
    xxe_payload = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE CommunicationLog [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<CommunicationLog xmlns="http://knx.org/xml/telegrams/01">
  <RecordStart Timestamp="2026-06-27T13:54:21.8676962Z" ConnectionName="&xxe;" Mode="LinkLayer" />
</CommunicationLog>
"""
    from defusedxml.common import EntitiesForbidden

    with pytest.raises(EntitiesForbidden):
        _parse(xxe_payload)
