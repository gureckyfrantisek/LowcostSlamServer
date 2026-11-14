# Control points: places the operator measures with a known position, to be tied to the exact coordinates
# in post-processing. A point is measured over a time: the operator stands on it and says "start", later
# "finish". The device stores both timestamps and the average of the GNSS positions in between as the
# approximate coordinates (the real ones come from the post-processing of the raw measurements).
#
# Timestamps are taken the moment the command arrives, on both clocks of the recording: the wall clock
# (the `ts` of imu.csv) and CLOCK_MONOTONIC (`mono_ns`, which the PPS edges of pps.csv are on).
# The positions are the ones that arrived between the two commands, so they lag the true time by the
# receiver's output delay (a fraction of a second): fine for a point the operator stands on.
import collections
import math
import os
import threading
import time
from typing import Optional

from shared.models import Position
from shared.names import valid_point_id

HEADER = ("point_id,start_ns,end_ns,start_mono_ns,end_mono_ns,latitude,longitude,altitude_m,"
          "positions,std_m,accuracy_m,fix_type")
M_PER_DEG = 111_320.0


class ControlPointError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _mean(values):
    return sum(values) / len(values) if values else None


def average(positions: list[Position]) -> dict:
    """The approximate coordinates of a point from the positions measured on it"""
    if not positions:
        return dict(latitude=None, longitude=None, altitude_m=None, positions=0, std_m=None, accuracy_m=None,
                    fix_type=None)
    lat = _mean([p.latitude for p in positions])
    lon = _mean([p.longitude for p in positions])
    # spread of the positions around the mean, in metres (a measure of how steady the fix was)
    dy = [(p.latitude - lat) * M_PER_DEG for p in positions]
    dx = [(p.longitude - lon) * M_PER_DEG * math.cos(math.radians(lat)) for p in positions]
    std = math.sqrt(sum(x * x + y * y for x, y in zip(dx, dy)) / len(positions))
    altitudes = [p.altitude_m for p in positions if p.altitude_m is not None]
    accuracies = [p.horizontal_accuracy_m for p in positions if p.horizontal_accuracy_m is not None]
    fix = collections.Counter(p.fix_type for p in positions if p.fix_type).most_common(1)
    return dict(latitude=lat, longitude=lon, altitude_m=_mean(altitudes), positions=len(positions),
                std_m=std, accuracy_m=_mean(accuracies), fix_type=fix[0][0] if fix else None)


def _csv(value) -> str:
    if value is None:
        return ""
    return f"{value:.9f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


class ControlPoints:
    def __init__(self):
        self._lock = threading.RLock()
        self._file = None
        self._path: Optional[str] = None
        self._open: Optional[dict] = None   # the point being measured
        self._positions: list[Position] = []
        self._done: list[dict] = []
        self.write_error: Optional[str] = None

    # --- the recording ----------------------------------------------------------------------------
    def begin_recording(self, project_path: str):
        with self._lock:
            self.end_recording()
            self._done, self._open, self._positions, self.write_error = [], None, [], None
            self._path = os.path.join(project_path, "control_points.csv")
            try:
                self._file = open(self._path, "w", buffering=1)
                self._file.write(HEADER + "\n")
            except OSError as e:
                self._file, self.write_error = None, str(e)

    def end_recording(self) -> list[str]:
        """Closes the file. A point still being measured is finished first. Returns the problems"""
        problems = []
        with self._lock:
            if self._open is not None:
                point = self._open["point_id"]
                try:
                    self.finish()
                    problems.append(f"control point {point} was still being measured and was finished by the stop")
                except ControlPointError as e:
                    problems.append(f"control point {point}: {e.message}")
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            if self.write_error:
                problems.append(f"control_points.csv could not be written: {self.write_error}")
        return problems

    @property
    def recording(self) -> bool:
        return self._path is not None and (self._file is not None or self.write_error is not None)

    # --- the operator ---------------------------------------------------------------------------------
    def start(self, point_id: str) -> dict:
        with self._lock:
            if not self.recording:
                raise ControlPointError("not_recording", "control points are measured during a measurement")
            if not valid_point_id(point_id):
                raise ControlPointError("invalid_id", "a point id is 1-32 letters, digits, '_', '.' or '-'")
            if self._open is not None:
                raise ControlPointError("already_open", f"point {self._open['point_id']} is still being measured: "
                                                        "finish or cancel it first")
            if any(p["point_id"] == point_id for p in self._done):
                raise ControlPointError("duplicate_id", f"point {point_id} was already measured in this measurement")
            self._open = {"point_id": point_id, "start_ns": time.time_ns(), "start_mono_ns": time.monotonic_ns()}
            self._positions = []
            return dict(self._open)

    def on_position(self, position: Position):
        """Called by the GNSS feed for every position"""
        with self._lock:
            if self._open is not None:
                self._positions.append(position)

    def finish(self) -> dict:
        with self._lock:
            if self._open is None:
                raise ControlPointError("not_started", "no control point is being measured")
            point = {**self._open, "end_ns": time.time_ns(), "end_mono_ns": time.monotonic_ns(),
                     **average(self._positions)}
            self._open, self._positions = None, []
            self._done.append(point)
            self._write(point)
            return point

    def cancel(self) -> dict:
        with self._lock:
            if self._open is None:
                raise ControlPointError("not_started", "no control point is being measured")
            point, self._open, self._positions = self._open, None, []
            return point

    def _write(self, p: dict):
        if self._file is None:
            return
        row = [p["point_id"], p["start_ns"], p["end_ns"], p["start_mono_ns"], p["end_mono_ns"], p["latitude"],
               p["longitude"], p["altitude_m"], p["positions"], p["std_m"], p["accuracy_m"], p["fix_type"]]
        try:
            self._file.write(",".join(_csv(v) if not isinstance(v, str) else v for v in row) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        except (OSError, ValueError) as e:  # the stick was pulled out, the disk is full
            self.write_error = str(e)

    # --- what the status says ---------------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            out = {"count": len(self._done), "open": None}
            if self._open is not None:
                out["open"] = {"point_id": self._open["point_id"],
                               "elapsed_s": round((time.monotonic_ns() - self._open["start_mono_ns"]) / 1e9, 1),
                               "positions": len(self._positions)}
            return out

    def points(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._done]


control_points = ControlPoints()
