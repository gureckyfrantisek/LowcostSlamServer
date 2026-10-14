# IMU connection and functions
import queue
import threading
import time
import serial
import json
import os
from datetime import datetime, timezone
import numpy as np
from app.core import config

# Columns of the log file, in order. The raw OpenLog Artemis line is
# rtcDate,rtcTime,aX,aY,aZ,gX,gY,gZ,mX,mY,mZ,imu_degC,output_Hz,
LOG_COLUMNS = ["ts", "aX", "aY", "aZ", "gX", "gY", "gZ", "mX", "mY", "mZ", "imu_degC", "output_Hz"]

# Columns the calibration offsets apply to
CALIBRATED_COLUMNS = ["aX", "aY", "aZ", "gX", "gY", "gZ", "mX", "mY", "mZ"]

_port: serial.Serial | None = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# The serial port is exclusive, so a single reader thread owns it and fans
# each timestamped line out to every subscriber queue.
_subscribers: set[queue.Queue] = set()
_subscribers_lock = threading.Lock()
_dropped_lines = 0
_invalid_lines = 0  # lines the logger could not parse (headers, partial lines, ...)

_log_stop: threading.Event | None = None
_log_thread: threading.Thread | None = None
_log_queue: queue.Queue | None = None

def subscribe(maxsize: int = 100) -> queue.Queue:
    """Returns a queue receiving (timestamp_ns, line_bytes) for every line"""
    q = queue.Queue(maxsize)
    with _subscribers_lock:
        _subscribers.add(q)
    return q

def unsubscribe(q: queue.Queue):
    with _subscribers_lock:
        _subscribers.discard(q)

def _read_loop():
    global _dropped_lines
    buf = b""
    while not _reader_stop.is_set():
        # Waits up to the port timeout for 1 byte, then grabs whatever else arrived
        chunk = _port.read(_port.in_waiting or 1)
        if not chunk:
            continue
        ts = time.time_ns()  # timestamp at receipt, shared by all consumers
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            with _subscribers_lock:
                subs = list(_subscribers)
            for q in subs:
                try:
                    q.put_nowait((ts, line))
                except queue.Full:
                    _dropped_lines += 1  # slow consumer must never block the reader

def open_imu_port():
    """Opens the IMU port and starts the reader thread"""
    global _port, _reader_thread
    if _port and _port.is_open:
        return
    _port = serial.Serial(config.IMU_PORT_1, int(config.IMU_BAUD_RATE_1), timeout=0.1)
    _reader_stop.clear()
    _reader_thread = threading.Thread(target=_read_loop, daemon=True)
    _reader_thread.start()

def close_imu_port():
    """Stops logging and the reader, then closes the port for clean shutdown"""
    global _port, _reader_thread
    stop_logging()
    _reader_stop.set()
    if _reader_thread:
        _reader_thread.join()
        _reader_thread = None
    if _port and _port.is_open:
        _port.close()
    _port = None

def is_port_open() -> bool:
    return _port is not None and _port.is_open

def is_logging() -> bool:
    return _log_thread is not None

def verify_connection():
    """Will implement with the sensor"""
    return True

def start_logging(project_path) -> int:
    global _log_stop, _log_thread, _log_queue

    if _port is None or not _port.is_open:
        raise RuntimeError("IMU port is not open")

    # Check everything before subscribing, so a failure leaves no queue behind
    calibration_data = load_calibration()
    if calibration_data is None:
        raise RuntimeError("IMU is not calibrated")

    _log_queue = subscribe(maxsize=10000)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue

    def _log():
        global _invalid_lines
        file_path = f"{project_path}/imu.csv"

        with open(file_path, "w") as f:
            f.write(columns_to_log(LOG_COLUMNS) + "\n")
            # Keep draining after stop is requested so no queued line is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, line = q.get(timeout=0.1)
                except queue.Empty:
                    f.flush()
                    continue

                try:
                    # Cleanup and correct the data here
                    raw_columns = line_to_columns(line.decode(errors="replace").strip())
                    clean_columns = cleanup_columns(ts, raw_columns)
                    corrected_columns = apply_correction(clean_columns, calibration_data)
                except ValueError:
                    # Sensor header row, a partial first line, menu text, ...
                    _invalid_lines += 1
                    continue

                f.write(columns_to_log(corrected_columns) + "\n")

    _log_thread = threading.Thread(target=_log, daemon=True)

    # Capture time before and after
    before_ns = time.time_ns()
    _log_thread.start()
    after_ns = time.time_ns()

    # Return the midpoint
    start_ns = (before_ns + after_ns) / 2

    return start_ns

def stop_logging():
    global _log_stop, _log_thread, _log_queue
    if _log_queue:
        unsubscribe(_log_queue)
        _log_queue = None
    if _log_stop:
        _log_stop.set()
    if _log_thread:
        _log_thread.join()
        _log_thread = None

def _checked(value, shape: tuple) -> list:
    array = np.array(value, dtype=float)
    if array.shape != shape or not np.isfinite(array).all():
        raise ValueError(f"expected finite values of shape {shape}")
    return array.tolist()

def load_calibration() -> dict | None:
    """Returns the stored calibration (gyro_bias, accel_bias, accel_matrix, mag_bias,
    mag_matrix as lists) and the time it was made (created, aware datetime), or None
    if the IMU was never calibrated (missing, unreadable, incomplete or old format
    calibration file)"""
    try:
        with open(config.IMU_CALIBRATION_PATH_1, "r") as f:
            data = json.load(f)
        return {
            "created": datetime.fromisoformat(data["created"]),
            "gyro_bias": _checked(data["gyro"]["bias"], (3,)),
            "accel_bias": _checked(data["accel"]["bias"], (3,)),
            "accel_matrix": _checked(data["accel"]["matrix"], (3, 3)),
            "mag_bias": _checked(data["mag"]["bias"], (3,)),
            "mag_matrix": _checked(data["mag"]["matrix"], (3, 3)),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None

def is_calibrated() -> bool:
    return load_calibration() is not None

def columns_to_log(columns: list[str]) -> str:
    """Formats the columns to the log string"""
    return ",".join(columns)

def line_to_columns(line: str) -> list[str]:
    """Splits a raw IMU line into its columns"""
    return line.split(",")

def cleanup_columns(timestamp: int, columns: list[str]) -> list[str]:
    """Transforms the raw IMU columns to our format

    The raw header is: rtcDate,rtcTime,aX,aY,aZ,gX,gY,gZ,mX,mY,mZ,imu_degC,output_Hz,

    We want: ts,aX,aY,aZ,gX,gY,gZ,mX,mY,mZ,imu_degC,output_Hz

    Raises ValueError for lines that are too short to be a data row."""
    wanted = len(LOG_COLUMNS) - 1  # everything but ts
    data_columns = columns[2:2 + wanted]  # drops rtcDate, rtcTime and the trailing empty column
    if len(data_columns) < wanted:
        raise ValueError(f"expected {wanted} data columns, got {len(data_columns)}")

    return [str(timestamp), *data_columns]

def calibrate_gyroscope(raw_data, calibration_data):
    """Calibrates the gyroscope: subtracts the bias"""
    return np.array(raw_data) - np.array(calibration_data["gyro_bias"])

def calibrate_accelerometer(raw_data, calibration_data):
    """Calibrates the accelerometer: subtracts the bias, then multiplies by the matrix"""
    corrected_data = np.array(raw_data) - np.array(calibration_data["accel_bias"])
    return np.dot(np.array(calibration_data["accel_matrix"]), corrected_data)

def calibrate_magnetometer(raw_data, calibration_data):
    """Calibrates the magnetometer: subtracts the hard iron bias, then multiplies by the soft iron matrix"""
    corrected_data = np.array(raw_data) - np.array(calibration_data["mag_bias"])
    return np.dot(np.array(calibration_data["mag_matrix"]), corrected_data)

def apply_correction(clean_columns: list[str], calibration_data: dict) -> list[str]:
    """Applies the calibration to the accelerometer, gyroscope and magnetometer columns.

    Raises ValueError if a value is not a number (e.g. the sensor header row)."""
    first = LOG_COLUMNS.index(CALIBRATED_COLUMNS[0])
    last = first + len(CALIBRATED_COLUMNS)
    raw = [float(column) for column in clean_columns[first:last]]

    cal_aX, cal_aY, cal_aZ = calibrate_accelerometer(raw[0:3], calibration_data)
    cal_gX, cal_gY, cal_gZ = calibrate_gyroscope(raw[3:6], calibration_data)
    cal_mX, cal_mY, cal_mZ = calibrate_magnetometer(raw[6:9], calibration_data)

    corrected = list(clean_columns)
    corrected[first:last] = [f"{value:.6f}" for value in
                             (cal_aX, cal_aY, cal_aZ, cal_gX, cal_gY, cal_gZ, cal_mX, cal_mY, cal_mZ)]
    return corrected


# --- Calibration steps ---
#
# Every step is numbered, independent and repeatable: its result is stored on
# its own, so the steps can be run in any order and a failed (or bad) step is
# fixed by just calling it again. Once every step has a result the calibration
# file is (re)written.
#
# Step 1 needs the device stationary (accelerometer and gyroscope). It is called
# once per side of the IMU: the side that points up is detected from gravity, so
# it works for any mounting. Step 2 needs the device moving (magnetometer).
# See docs/imu_calibration.md. Thresholds are in app/core/config.py (IMU_CAL_*).

GRAVITY_MG = 1000.0  # the OpenLog Artemis reports the accelerometer in milli-g
STEP_RESULTS_VERSION = 2

class StepFailed(Exception):
    """The step ran but its data was rejected. Fix the cause and repeat the step"""

class CalibrationUnavailable(Exception):
    """Calibration can not run right now (port closed, logging, another step running)"""

# The sides of the IMU: the sensor axis that points up while the device rests. The
# accelerometer reads +1 g on the axis that points up, so the side is detected from it.
SIDES = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]

_STATIC_PLACEMENT = (
    "Rest the device on a level, stable surface on one of its sides, then call the step. Repeat "
    "it once per side, each time with a different IMU axis pointing up (+X, -X, +Y, -Y, +Z, -Z): "
    "the server detects which one is up and reports which sides are still missing. Hold it "
    "completely still (supported, not in hand, no cables pulling) and wait ~2 s after placing it."
)
_MAG_PLACEMENT = (
    "Keep the device assembled as in use and away from magnets, speakers, phones, laptops and "
    "large metal objects (>= 1 m). Slowly rotate it through ALL orientations (every axis up and "
    "down, figure-eights) for the whole duration, roughly one turn every 2 s, without stopping."
)

STATIC_STEP = 1
MAG_STEP = 2
CALIBRATION_STEPS = [
    {"number": STATIC_STEP, "kind": "static", "name": "Rest on each side (6 times)",
     "placement": _STATIC_PLACEMENT},
    {"number": MAG_STEP, "kind": "moving", "name": "Magnetometer rotation",
     "placement": _MAG_PLACEMENT},
]

_calibration_lock = threading.Lock()  # one step at a time, also guards the step results file

def get_calibration_step(number: int) -> dict | None:
    return next((s for s in CALIBRATION_STEPS if s["number"] == number), None)

def list_calibration_steps() -> list[dict]:
    """The steps with their placement instructions and whether they have a result"""
    results = _load_step_results()
    steps = []
    for s in CALIBRATION_STEPS:
        keys = SIDES if s["kind"] == "static" else ["mag"]
        times = [results[key]["at"] for key in keys if key in results]
        step = {
            "number": s["number"],
            "kind": s["kind"],
            "name": s["name"],
            "placement": s["placement"],
            "done": all(key in results for key in keys),
            "at": max(times) if times else None,
        }
        if s["kind"] == "static":
            step["sides_done"] = [side for side in SIDES if side in results]
            step["sides_missing"] = [side for side in SIDES if side not in results]
        steps.append(step)
    return steps

def calibration_status() -> dict:
    """Whether the IMU is calibrated, when, and a warning if that is too long ago"""
    calibration = load_calibration()
    if calibration is None:
        return {"calibrated": False, "calibrated_at": None, "age_days": None, "outdated": False,
                "warning": "IMU is not calibrated"}

    created = calibration["created"]
    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    outdated = age_days > config.IMU_CAL_MAX_AGE_DAYS
    return {
        "calibrated": True,
        "calibrated_at": created.isoformat(timespec="seconds"),
        "age_days": round(age_days, 1),
        "outdated": outdated,
        "warning": (f"Calibration is {age_days:.0f} days old (limit {config.IMU_CAL_MAX_AGE_DAYS:g}), "
                    "calibrate again") if outdated else None,
    }

def run_calibration_step(number: int, duration: float | None = None) -> dict:
    """Runs one step (blocking), stores its result and recalibrates when all steps are done.

    `duration` only applies to the moving (magnetometer) step.
    Raises StepFailed (repeat the step) or CalibrationUnavailable."""
    step = get_calibration_step(number)
    if step is None:
        raise ValueError(f"unknown step {number}")

    if not _calibration_lock.acquire(blocking=False):
        raise CalibrationUnavailable("another calibration step is running")
    try:
        if not is_port_open():
            raise CalibrationUnavailable("IMU port is not open")
        if is_logging():
            raise CalibrationUnavailable("IMU is logging a measurement")

        if step["kind"] == "static":
            key, result = _capture_static()  # keyed by the detected side, a repeated side replaces it
        else:
            key, result = "mag", _capture_mag(duration or config.IMU_CAL_MAG_SECONDS)
        result["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        results = _load_step_results()
        results[key] = result
        _write_json(_step_results_path(), {"version": STEP_RESULTS_VERSION, "steps": results})

        steps = list_calibration_steps()
        response = {
            "step": number,
            "name": step["name"],
            "samples": result["samples"],
            "done": [s["number"] for s in steps if s["done"]],
            "missing": [s["number"] for s in steps if not s["done"]],
            "calibrated": False,
        }
        if step["kind"] == "static":
            response["side"] = key
            response["sides_done"] = [side for side in SIDES if side in results]
            response["sides_missing"] = [side for side in SIDES if side not in results]
        if not response["missing"]:
            try:
                _write_json(config.IMU_CALIBRATION_PATH_1, _fit_all(results))
                response["calibrated"] = True
            except StepFailed as e:
                # The data is stored, so only the step(s) behind the problem need repeating
                response["problem"] = str(e)
        return response
    finally:
        _calibration_lock.release()

def _collect_samples(seconds: float) -> np.ndarray:
    """Collects raw samples for `seconds`, one row per valid line:
    aX aY aZ gX gY gZ mX mY mZ imu_degC (raw, uncorrected)"""
    q = subscribe(maxsize=20000)
    rows = []
    deadline = time.monotonic() + seconds
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                ts, line = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            try:
                columns = cleanup_columns(ts, line_to_columns(line.decode(errors="replace").strip()))
                rows.append([float(c) for c in columns[1:11]])
            except ValueError:
                continue  # sensor header, partial line, ...
    finally:
        unsubscribe(q)

    data = np.array(rows[1:])  # the first line can be cut off in the middle
    if data.size:
        data = data[np.isfinite(data).all(axis=1)]
    return data

def _capture_static() -> tuple[str, dict]:
    """One rest: returns the detected side (the sensor axis pointing up, e.g. "+X") and its result"""
    seconds = config.IMU_CAL_STILL_SECONDS
    data = _collect_samples(seconds)
    if len(data) < config.IMU_CAL_MIN_STILL_SAMPLES:
        raise StepFailed(f"only {len(data)} samples in {seconds:g} s, is the IMU streaming?")

    accel, gyro = data[:, 0:3], data[:, 3:6]
    if (accel.std(axis=0).max() > config.IMU_CAL_MAX_ACCEL_STD_MG
            or gyro.std(axis=0).max() > config.IMU_CAL_MAX_GYRO_STD_DPS):
        raise StepFailed("the device moved during the measurement, keep it still and repeat")

    mean_accel = accel.mean(axis=0)
    norm = float(np.linalg.norm(mean_accel))
    if abs(norm - GRAVITY_MG) > config.IMU_CAL_MAX_GRAVITY_ERROR * GRAVITY_MG:
        raise StepFailed(f"acceleration is {norm:.0f} mg instead of ~{GRAVITY_MG:.0f} mg (moving, or wrong units)")

    # The side is the axis gravity points along most, and it has to be close to straight up
    axis = int(np.argmax(np.abs(mean_accel)))
    side = f"{'+' if mean_accel[axis] > 0 else '-'}{'XYZ'[axis]}"
    tilt = float(np.degrees(np.arccos(np.clip(abs(mean_accel[axis]) / norm, -1.0, 1.0))))
    if tilt > config.IMU_CAL_MAX_TILT_DEG:
        raise StepFailed(f"the device does not rest on one of its sides (closest is {side} up, "
                         f"{tilt:.0f} deg off), place it flat on a side and repeat")

    return side, {
        "accel": mean_accel.tolist(),
        "gyro": gyro.mean(axis=0).tolist(),
        "temp": float(data[:, 9].mean()),
        "samples": len(data),
    }

def _capture_mag(duration: float) -> dict:
    data = _collect_samples(duration)
    if len(data) < config.IMU_CAL_MAG_MIN_SAMPLES:
        raise StepFailed(f"only {len(data)} samples in {duration:g} s, is the IMU streaming?")

    mag = data[:, 6:9]
    octants = _octants_covered(mag)
    if octants < config.IMU_CAL_MAG_MIN_OCTANTS:
        raise StepFailed(f"rotation covered only {octants} of 8 octants (need {config.IMU_CAL_MAG_MIN_OCTANTS}), "
                         "turn the device through all orientations and repeat")
    _fit_ellipsoid(mag)  # reject a bad fit now instead of at the last step

    keep = min(len(mag), config.IMU_CAL_MAG_STORED_POINTS)
    stored = mag[np.linspace(0, len(mag) - 1, keep).astype(int)]
    return {"mag": stored.tolist(), "samples": len(mag), "octants": octants}

def _octants_covered(points: np.ndarray) -> int:
    """Number of the 8 octants (around the center of the data) holding a fair share of the samples.

    The center comes from a plain sphere fit, not from the min / max of the data: a
    patch of the sphere would otherwise look like it surrounds its own bounding box."""
    design = np.column_stack([2 * points, np.ones(len(points))])
    solution = np.linalg.lstsq(design, (points ** 2).sum(axis=1), rcond=None)[0]
    above = points > solution[:3]
    ids = above[:, 0] * 4 + above[:, 1] * 2 + above[:, 2]
    counts = np.bincount(ids, minlength=8)
    return int((counts >= max(5, 0.01 * len(points))).sum())

def _local_field_uT() -> float | None:
    """Total strength of the Earth's magnetic field at our location from the IGRF model.

    Offline: pyIGRF has the model coefficients built in. Returns None if it can not be computed."""
    # TODO: get the current location from the NMEA parser (config has Prague until then)
    latitude, longitude, altitude_m = config.IMU_CAL_LATITUDE, config.IMU_CAL_LONGITUDE, config.IMU_CAL_ALTITUDE_M
    try:
        import pyIGRF  # imported here so a missing library only costs the field scaling

        now = datetime.now(timezone.utc)
        year = now.year + (now.timetuple().tm_yday - 1) / 365.25
        year = min(year, config.IMU_CAL_IGRF_MAX_YEAR)  # the model does not go further
        # Returns declination, inclination, horizontal, X, Y, Z and total intensity (all nT)
        total_nT = pyIGRF.igrf_value(latitude, longitude, altitude_m / 1000.0, year)[6]
        return float(total_nT) / 1000.0
    except Exception:
        return None

def _fit_ellipsoid(points: np.ndarray, radius: float | None = None) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Least squares ellipsoid fit (hard + soft iron).

    Returns (bias, matrix, radius, rms) such that matrix * (point - bias) lies on a
    sphere of `radius`: the given one (the local field strength), or without it the
    geometric mean of the ellipsoid semi-axes (so the overall scale of the sensor is
    kept). rms is the radial residual of the corrected points."""
    x, y, z = points.T
    design = np.column_stack([x * x, y * y, z * z, 2 * y * z, 2 * x * z, 2 * x * y, 2 * x, 2 * y, 2 * z])
    v = np.linalg.lstsq(design, np.ones(len(x)), rcond=None)[0]

    # x'Qx + 2u'x = 1  ->  (x - c)'Q(x - c) = k
    q = np.array([[v[0], v[5], v[4]], [v[5], v[1], v[3]], [v[4], v[3], v[2]]])
    u = v[6:9]
    try:
        center = -np.linalg.solve(q, u)
        k = 1 + np.dot(u, np.linalg.solve(q, u))
    except np.linalg.LinAlgError:
        raise StepFailed("magnetometer data does not form an ellipsoid, repeat the rotation") from None

    w, vecs = np.linalg.eigh(q / k)
    if not (w > 0).all():
        raise StepFailed("magnetometer data does not form an ellipsoid, repeat the rotation")

    if radius is None:
        radius = float(np.prod(w) ** (-1 / 6))
    matrix = np.dot(radius * vecs * np.sqrt(w), vecs.T)

    corrected = np.linalg.norm(np.dot(points - center, matrix.T), axis=1)
    rms = float(np.sqrt(np.mean((corrected - radius) ** 2)))
    if rms > config.IMU_CAL_MAG_MAX_RESIDUAL * radius:
        raise StepFailed(f"magnetometer fit is poor ({100 * rms / radius:.1f} % residual), "
                         "rotate through all orientations and repeat")
    return center, matrix, radius, rms

def _fit_accel(results: dict) -> tuple[np.ndarray, np.ndarray]:
    """Bias and per-axis scale from the up / down rest of every axis"""
    bias, scale = np.zeros(3), np.zeros(3)
    for axis, name in enumerate("XYZ"):
        high = results[f"+{name}"]["accel"][axis]
        low = results[f"-{name}"]["accel"][axis]
        bias[axis] = (high + low) / 2
        scale[axis] = GRAVITY_MG / ((high - low) / 2)

    if not ((scale > config.IMU_CAL_MIN_SCALE) & (scale < config.IMU_CAL_MAX_SCALE)).all():
        raise StepFailed(f"accelerometer scale {np.round(scale, 3).tolist()} is outside "
                         f"{config.IMU_CAL_MIN_SCALE}..{config.IMU_CAL_MAX_SCALE}, "
                         "repeat step 1 on the sides that look wrong")
    return bias, np.diag(scale)

def _fit_all(results: dict) -> dict:
    """The calibration file content from the results of all steps"""
    accel_bias, accel_matrix = _fit_accel(results)

    static = [results[side] for side in SIDES]
    gyro_bias = np.mean([s["gyro"] for s in static], axis=0)
    temp = float(np.mean([s["temp"] for s in static]))

    field = _local_field_uT()
    mag_bias, mag_matrix, radius, rms = _fit_ellipsoid(np.array(results["mag"]["mag"]), field)

    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "temp_c": temp,
        "gyro": {"bias": gyro_bias.tolist()},
        "accel": {"bias": accel_bias.tolist(), "matrix": accel_matrix.tolist()},
        "mag": {"bias": mag_bias.tolist(), "matrix": mag_matrix.tolist(), "radius": radius, "rms": rms,
                "field_source": "igrf" if field is not None else "fit"},
    }

def _step_results_path() -> str:
    return os.path.splitext(config.IMU_CALIBRATION_PATH_1)[0] + ".steps.json"

def _load_step_results() -> dict:
    """Results done so far: one per rested side ("+X", ...) and "mag" for the rotation"""
    try:
        with open(_step_results_path(), "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STEP_RESULTS_VERSION:
        return {}
    steps = data.get("steps")
    return steps if isinstance(steps, dict) else {}

def _write_json(path: str, data: dict):
    """Atomic, a crash must never leave a half written file behind"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
