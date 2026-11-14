# NMEA sentences of the GNSS receiver -> Position. One epoch of a ZED-F9P is RMC, VTG, GGA, GSA, GSV,
# GLL (and GST when it is switched on), the position is published when its GGA arrives.
import calendar
import math
import time
from typing import Optional

from shared.models import Position

KNOT_MPS = 0.514444

# GGA fix quality -> fix_type
FIX_TYPES = {1: "gnss", 2: "dgnss", 3: "pps", 4: "rtk_fixed", 5: "rtk_float", 6: "dead_reckoning",
             7: "manual", 8: "simulation"}

NO_FIX = object()  # the receiver says it has no fix (as opposed to saying nothing)


def _field(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def _float(text: str) -> Optional[float]:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def coordinate(value: str, hemisphere: str) -> Optional[float]:
    """ddmm.mmmm / dddmm.mmmm and N/S/E/W -> signed degrees"""
    number = _float(value)
    if number is None or hemisphere not in ("N", "S", "E", "W") or number < 0:
        return None
    degrees = int(number // 100)
    minutes = number - degrees * 100
    if minutes >= 60:
        return None
    result = degrees + minutes / 60
    return -result if hemisphere in ("S", "W") else result


def utc_ns(date: str, clock: str) -> Optional[int]:
    """ddmmyy and hhmmss.ss (UTC) -> ns since the Unix epoch"""
    try:
        day, month, year = int(date[0:2]), int(date[2:4]), 2000 + int(date[4:6])
        hours, minutes, seconds = int(clock[0:2]), int(clock[2:4]), float(clock[4:])
        whole = calendar.timegm((year, month, day, hours, minutes, 0, 0, 0, 0))
    except (ValueError, IndexError):
        return None
    return round((whole + seconds) * 1e9)


class NmeaPosition:
    """Feed it the NMEA sentences in the order they arrive. feed() returns a Position when a GGA with a
    fix completes one, NO_FIX when a GGA says there is none, and None for everything else"""

    GST_MAX_AGE_S = 2.0

    def __init__(self):
        self._rmc: Optional[dict] = None
        self._gst: Optional[tuple[float, float]] = None   # (accuracy m, monotonic time)

    def feed(self, sentence: bytes | str):
        if isinstance(sentence, bytes):
            sentence = sentence.decode("ascii", errors="replace")
        sentence = sentence.strip()
        if not sentence.startswith("$") or "*" not in sentence:
            return None
        fields = sentence[1:sentence.rindex("*")].split(",")
        kind = fields[0][2:] if len(fields[0]) >= 5 else ""
        if kind == "RMC":
            self._rmc_sentence(fields)
        elif kind == "GST":
            self._gst_sentence(fields)
        elif kind == "GGA":
            return self._gga_sentence(fields)
        return None

    def _rmc_sentence(self, fields):
        valid = _field(fields, 2) == "A"
        speed = _float(_field(fields, 7))
        course = _float(_field(fields, 8))
        self._rmc = {
            "time": _field(fields, 1), "date": _field(fields, 9), "valid": valid,
            "speed": speed * KNOT_MPS if speed is not None else None,
            "course": course if course is not None and 0 <= course <= 360 else None,
        }

    def _gst_sentence(self, fields):
        lat, lon = _float(_field(fields, 6)), _float(_field(fields, 7))
        if lat is not None and lon is not None:
            self._gst = (math.hypot(lat, lon), time.monotonic())

    def _gga_sentence(self, fields):
        try:
            quality = int(_field(fields, 6) or 0)
        except ValueError:
            return None
        if quality == 0:
            return NO_FIX
        lat = coordinate(_field(fields, 2), _field(fields, 3))
        lon = coordinate(_field(fields, 4), _field(fields, 5))
        rmc = self._rmc
        if lat is None or lon is None or rmc is None or not rmc["date"]:
            return None  # no date yet: the first RMC of the epoch comes before the GGA, so this is the very start
        stamp = utc_ns(rmc["date"], _field(fields, 1))
        if stamp is None:
            return None
        accuracy = None
        if self._gst and time.monotonic() - self._gst[1] <= self.GST_MAX_AGE_S:
            accuracy = round(self._gst[0], 3)
        satellites = _field(fields, 7)
        same_epoch = rmc["time"] == _field(fields, 1)
        return Position(
            latitude=lat, longitude=lon, timestamp_ns=stamp,
            altitude_m=_float(_field(fields, 9)),
            horizontal_accuracy_m=accuracy,
            fix_type=FIX_TYPES.get(quality, "gnss"),
            satellites=int(satellites) if satellites.isdigit() else None,
            hdop=_float(_field(fields, 8)),
            speed_mps=rmc["speed"] if same_epoch and rmc["valid"] else None,
            course_deg=rmc["course"] if same_epoch and rmc["valid"] else None,
        )
