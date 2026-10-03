# GNSS connection and functions
import queue
import threading
import time
import serial
from config import GNSS_PORT, GNSS_BAUD_RATE

_port: serial.Serial | None = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# The serial port is exclusive, so a single reader thread owns it and fans
# each timestamped line out to every subscriber queue.
_subscribers: set[queue.Queue] = set()
_subscribers_lock = threading.Lock()
_dropped_lines = 0

_log_stop: threading.Event | None = None
_log_thread: threading.Thread | None = None
_log_queue: queue.Queue | None = None

def subscribe(maxsize: int = 100) -> queue.Queue:
    """Returns a queue receiving (timestamp_ns, raw_line_bytes) for every line"""
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

def open_gnss_port():
    """Opens the GNSS port and starts the reader thread"""
    global _port, _reader_thread
    if _port and _port.is_open:
        return
    _port = serial.Serial(GNSS_PORT, GNSS_BAUD_RATE, timeout=0.1)
    _reader_stop.clear()
    _reader_thread = threading.Thread(target=_read_loop, daemon=True)
    _reader_thread.start()

def close_gnss_port():
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
        raise RuntimeError("GNSS port is not open")

    _log_queue = subscribe(maxsize=10000)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue

    def _log():
        file_path = f"{project_path}/gnss.txt"

        with open(file_path, "w") as f:
            f.write("timestamp_ns,data\n")
            # Keep draining after stop is requested so no queued line is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, line = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                f.write(f"{ts},{line.decode(errors='replace').rstrip()}\n")

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
