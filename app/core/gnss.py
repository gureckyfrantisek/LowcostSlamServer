# GNSS connection and functions
import queue
import threading
import time
import serial
from app.core import config

NMEA = "nmea"
UBX = "ubx"

_UBX_MAX_PAYLOAD = 4096
_NMEA_MAX_LEN = 100

_port: serial.Serial | None = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# The serial port is exclusive, so a single reader thread owns it, splits the
# byte stream into frames and fans each timestamped frame out to every
# subscriber queue whose `kinds` filter matches.
_subscribers: dict[queue.Queue, frozenset[str]] = {}
_subscribers_lock = threading.Lock()
_dropped_frames = 0

_log_stop: threading.Event | None = None
_log_thread: threading.Thread | None = None
_log_queue: queue.Queue | None = None


def _ubx_checksum_ok(frame: bytes) -> bool:
    ck_a = ck_b = 0
    for byte in frame[2:-2]:  # class, id, length, payload
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return frame[-2] == ck_a and frame[-1] == ck_b


def _nmea_checksum_ok(line: bytes) -> bool:
    star = line.rfind(b"*")
    if star < 0 or len(line) < star + 3:
        return False
    calc = 0
    for byte in line[1:star]:
        calc ^= byte
    try:
        return calc == int(line[star + 1:star + 3], 16)
    except ValueError:
        return False


class _FrameParser:
    """Splits a mixed NMEA/UBX byte stream into validated frames.

    NMEA frames start with '$' and end with '\\n'. UBX frames start with
    0xB5 0x62 and carry a length field. Binary payloads can contain '\\n' and
    '$' bytes, so the stream must never be split on newlines blindly.
    """

    def __init__(self):
        self._buf = bytearray()
        self._start_ts: int | None = None  # arrival time of the frame being assembled

    def feed(self, chunk: bytes, ts: int) -> list[tuple[int, str, bytes]]:
        buf = self._buf
        buf += chunk
        frames = []

        while buf:
            if self._start_ts is None:
                self._start_ts = ts

            if buf[0] == 0xB5:
                if len(buf) < 2:
                    break
                if buf[1] != 0x62:
                    self._drop_byte()
                    continue
                if len(buf) < 6:
                    break
                length = buf[4] | (buf[5] << 8)
                if length > _UBX_MAX_PAYLOAD:
                    self._drop_byte()
                    continue
                total = length + 8  # sync(2) + class/id(2) + len(2) + payload + checksum(2)
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                if _ubx_checksum_ok(frame):
                    frames.append((self._start_ts, UBX, frame))
                    del buf[:total]
                    self._start_ts = None
                else:
                    self._drop_byte()

            elif buf[0] == 0x24:  # '$'
                end = buf.find(b"\n")
                if end < 0:
                    if len(buf) > _NMEA_MAX_LEN:
                        self._drop_byte()
                        continue
                    break
                line = bytes(buf[:end + 1])
                if _nmea_checksum_ok(line.rstrip()):
                    frames.append((self._start_ts, NMEA, line))
                    del buf[:end + 1]
                    self._start_ts = None
                else:
                    self._drop_byte()

            else:
                self._drop_byte()  # junk between frames

        return frames

    def _drop_byte(self):
        del self._buf[0]
        self._start_ts = None


def subscribe(kinds: list[str] | None = None, maxsize: int = 100) -> queue.Queue:
    """Returns a queue receiving (timestamp_ns, kind, frame_bytes) for every
    frame of the requested kinds, e.g. [NMEA], [UBX] or [NMEA, UBX] (default)"""
    if kinds is None:
        kinds = [NMEA, UBX]
    elif isinstance(kinds, str):
        kinds = [kinds]  # frozenset("ubx") would split into characters
    q = queue.Queue(maxsize)
    with _subscribers_lock:
        _subscribers[q] = frozenset(kinds)
    return q

def unsubscribe(q: queue.Queue):
    with _subscribers_lock:
        _subscribers.pop(q, None)

def _read_loop():
    global _dropped_frames
    parser = _FrameParser()
    while not _reader_stop.is_set():
        # Waits up to the port timeout for 1 byte, then grabs whatever else arrived
        chunk = _port.read(_port.in_waiting or 1)
        if not chunk:
            continue
        ts = time.time_ns()  # timestamp at receipt, shared by all consumers
        for frame_ts, kind, data in parser.feed(chunk, ts):
            with _subscribers_lock:
                subs = list(_subscribers.items())
            for q, kinds in subs:
                if kind not in kinds:
                    continue
                try:
                    q.put_nowait((frame_ts, kind, data))
                except queue.Full:
                    _dropped_frames += 1  # slow consumer must never block the reader

def open_gnss_port():
    """Opens the GNSS port and starts the reader thread"""
    global _port, _reader_thread
    if _port and _port.is_open:
        return
    _port = serial.Serial(config.GNSS_PORT, config.GNSS_BAUD_RATE, timeout=0.1)
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
    """Logs the raw UBX stream untouched to gnss.ubx, plus gnss_index.csv
    mapping every frame to the host clock"""
    global _log_stop, _log_thread, _log_queue

    if _port is None or not _port.is_open:
        raise RuntimeError("GNSS port is not open")

    _log_queue = subscribe(kinds=[UBX], maxsize=10000)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue

    def _log():
        with open(f"{project_path}/gnss.ubx", "wb") as raw, \
             open(f"{project_path}/gnss_index.csv", "w") as index:
            index.write("timestamp_ns,offset,length,ubx_class,ubx_id\n")
            offset = 0
            # Keep draining after stop is requested so no queued frame is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, _, frame = q.get(timeout=0.1)
                except queue.Empty:
                    raw.flush()
                    index.flush()
                    continue
                raw.write(frame)
                index.write(f"{ts},{offset},{len(frame)},{frame[2]},{frame[3]}\n")
                offset += len(frame)

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
