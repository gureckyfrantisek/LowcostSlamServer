# IMU connection and functions
import queue
import threading
import time
import serial
import json
import os
from app.core import config

# Columns of the log file, in order. The raw OpenLog Artemis line is
# rtcDate,rtcTime,aX,aY,aZ,gX,gY,gZ,mX,mY,mZ,imu_degC,output_Hz,
LOG_COLUMNS = ["ts", "aX", "aY", "aZ", "gX", "gY", "gZ", "mX", "mY", "mZ", "imu_degC", "output_Hz"]

# Columns the calibration offsets apply to
CALIBRATED_COLUMNS = ["aX", "aY", "aZ", "gX", "gY", "gZ", "mX", "mY", "mZ"]

# TODO: placeholder until the real calibration procedure exists
_PLACEHOLDER_CALIBRATION = {
    "aX": 10,
    "aY": -2,
    "aZ": 3,
    "gX": 10,
    "gY": -2,
    "gZ": 3,
    "mX": 10,
    "mY": -2,
    "mZ": 3,
}

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
        file_path = f"{project_path}/imu.txt"

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

def load_calibration() -> dict | None:
    """Returns the stored calibration offsets, or None if the IMU was never
    calibrated (missing, unreadable or incomplete calibration file)"""
    try:
        with open(config.IMU_CALIBRATION_PATH_1, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    for column in CALIBRATED_COLUMNS:
        value = data.get(column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None

    return {column: data[column] for column in CALIBRATED_COLUMNS}

def is_calibrated() -> bool:
    return load_calibration() is not None

def calibrate():
    """Calibrates and stores/rewrites the calibration data into the filesystem"""
    # TODO: measure the real offsets, for now store the placeholder values
    path = config.IMU_CALIBRATION_PATH_1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(_PLACEHOLDER_CALIBRATION, f, indent=2)
    return True

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

def apply_correction(clean_columns: list[str], calibration_data: dict) -> list[str]:
    """Adds the calibration offset to every calibrated column.

    Raises ValueError if a value is not a number (e.g. the sensor header row)."""
    corrected = list(clean_columns)
    for column in CALIBRATED_COLUMNS:
        index = LOG_COLUMNS.index(column)
        corrected[index] = f"{float(corrected[index]) + calibration_data[column]:.6f}"

    return corrected
