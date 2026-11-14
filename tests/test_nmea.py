# NMEA sentences -> Position. The sentences are what the ZED-F9P on the Radxa sends.
import calendar
import math
import time

import pytest

from app.core import nmea
from app.core.nmea import NO_FIX, NmeaPosition, coordinate, utc_ns
from tests.fakes import nmea as sentence

# a captured epoch of the receiver (2026-11-14 11:58:40 UTC)
RMC = b"$GNRMC,115840.00,A,5005.89580,N,01422.01245,E,0.032,,141126,,,A,V*16\r\n"
GGA = sentence("GNGGA,115840.00,5005.89580,N,01422.01245,E,1,12,0.56,304.6,M,44.4,M,,")
EXPECTED_NS = calendar.timegm((2026, 11, 14, 11, 58, 40)) * 10**9


def test_real_rmc_and_gga_make_a_position():
    parser = NmeaPosition()
    assert parser.feed(RMC) is None
    position = parser.feed(GGA)
    assert position.latitude == pytest.approx(50 + 5.89580 / 60)
    assert position.longitude == pytest.approx(14 + 22.01245 / 60)
    assert position.timestamp_ns == EXPECTED_NS
    assert position.altitude_m == 304.6 and position.satellites == 12 and position.hdop == 0.56
    assert position.fix_type == "gnss"
    assert position.speed_mps == pytest.approx(0.032 * 0.514444) and position.course_deg is None


def test_the_checksum_of_the_captured_rmc_is_valid():
    body = RMC[1:RMC.index(b"*")]
    calc = 0
    for byte in body:
        calc ^= byte
    assert calc == 0x16


@pytest.mark.parametrize("value, hemisphere, expected", [
    ("5005.89580", "N", 50.0982633), ("5005.89580", "S", -50.0982633),
    ("01422.01245", "E", 14.3668742), ("01422.01245", "W", -14.3668742),
    ("0000.00000", "N", 0.0), ("18000.00000", "E", 180.0),
])
def test_coordinates(value, hemisphere, expected):
    assert coordinate(value, hemisphere) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("value, hemisphere", [("", "N"), ("5005.8958", ""), ("5005.8958", "X"), ("abc", "N"),
                                               ("5075.0000", "N"), ("-5005.0", "N"), ("nan", "N")])
def test_bad_coordinates_are_none(value, hemisphere):
    assert coordinate(value, hemisphere) is None


def test_utc_time_with_fraction_and_bad_input():
    assert utc_ns("141126", "115840.50") == EXPECTED_NS + 500_000_000
    assert utc_ns("311299", "235959.00") == calendar.timegm((2099, 12, 31, 23, 59, 59)) * 10**9
    for bad in (("", "115840.00"), ("141126", ""), ("991399", "115840.00"), ("141126", "xx")):
        assert utc_ns(*bad) is None


def test_no_fix_is_reported_as_no_fix():
    parser = NmeaPosition()
    parser.feed(RMC)
    assert parser.feed(sentence("GNGGA,115841.00,,,,,0,00,99.99,,,,,,")) is NO_FIX


def test_gga_before_any_rmc_waits():
    parser = NmeaPosition()
    assert parser.feed(GGA) is None                 # no date yet
    parser.feed(RMC)
    assert parser.feed(GGA) is not None


def test_fix_quality_names():
    parser = NmeaPosition()
    parser.feed(RMC)
    for quality, name in ((1, "gnss"), (2, "dgnss"), (4, "rtk_fixed"), (5, "rtk_float"), (6, "dead_reckoning")):
        text = f"GNGGA,115840.00,5005.89580,N,01422.01245,E,{quality},12,0.56,304.6,M,44.4,M,,"
        assert parser.feed(sentence(text)).fix_type == name


def test_southern_and_western_positions():
    parser = NmeaPosition()
    parser.feed(sentence("GNRMC,115840.00,A,3351.50000,S,15112.00000,E,0.0,,141126,,,A,V"))
    position = parser.feed(sentence("GNGGA,115840.00,3351.50000,S,15112.00000,E,1,10,0.9,20.0,M,0,M,,"))
    assert position.latitude < 0 < position.longitude


def test_accuracy_comes_from_gst_when_it_is_fresh():
    parser = NmeaPosition()
    parser.feed(sentence("GNGST,115840.00,2.0,1.5,1.0,90.0,0.03,0.04,0.10"))
    parser.feed(RMC)
    assert parser.feed(GGA).horizontal_accuracy_m == pytest.approx(0.05)


def test_stale_gst_is_not_used(monkeypatch):
    parser = NmeaPosition()
    parser.feed(sentence("GNGST,115840.00,2.0,1.5,1.0,90.0,0.03,0.04,0.10"))
    parser.feed(RMC)
    real = time.monotonic
    monkeypatch.setattr(nmea.time, "monotonic", lambda: real() + 10)
    assert parser.feed(GGA).horizontal_accuracy_m is None


def test_speed_and_course_only_from_the_same_epoch():
    parser = NmeaPosition()
    parser.feed(sentence("GNRMC,115839.00,A,5005.89580,N,01422.01245,E,5.0,90.0,141126,,,A,V"))
    position = parser.feed(GGA)                      # the GGA of the next second: the RMC is old
    assert position.speed_mps is None and position.course_deg is None
    parser.feed(sentence("GNRMC,115840.00,A,5005.89580,N,01422.01245,E,5.0,90.0,141126,,,A,V"))
    position = parser.feed(GGA)
    assert position.speed_mps == pytest.approx(5.0 * 0.514444) and position.course_deg == 90.0


@pytest.mark.parametrize("text", ["", "$", "garbage", "$GNGGA", "$GNGGA,1,2*", b"\xff\xfe$GNGGA*00", "$GN*00",
                                  "$GNGGA,115840.00,5005.89580,N,01422.01245,E,x,12,0.56,304.6,M,44.4,M,,*00"])
def test_garbage_does_not_raise(text):
    parser = NmeaPosition()
    parser.feed(RMC)
    assert parser.feed(text) in (None, NO_FIX)


def test_other_sentences_are_ignored():
    parser = NmeaPosition()
    for text in ("GNVTG,,T,,M,0.032,N,0.060,K,A", "GNGSA,A,3,24,23,15,10,19,22,17,14,,,,,1.16,0.56,1.02,1",
                 "GPGSV,3,1,12,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45", "GNGLL,5005.89580,N,01422.01245,E,115840.00,A,A"):
        assert NmeaPosition().feed(sentence(text)) is None
    assert math.isfinite(1)


def test_values_out_of_range_never_make_an_invalid_position():
    parser = NmeaPosition()
    parser.feed(RMC)
    with pytest.raises(Exception):
        # the feed in app/core/position.py catches this: pydantic rejects it
        parser.feed(sentence("GNGGA,115840.00,5005.89580,N,01422.01245,E,1,12,-3.0,304.6,M,44.4,M,,"))
