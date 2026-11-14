# GNSS connection and functions
import queue
import threading
import time
from app.core import config, i2c

NMEA = "nmea"
UBX = "ubx"

_UBX_MAX_PAYLOAD = 4096
_NMEA_MAX_LEN = 100

_BYTES_AVAILABLE = 0xFD  # 2 bytes, big endian: how much output the receiver has waiting
_MAX_CHUNK = 128  # a long read would hold the shared bus, and delay the IMU, for too long

_port: "DdcPort | None" = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# A single reader thread owns the receiver's output, splits the byte stream
# into frames and fans each timestamped frame out to every subscriber queue
# whose `kinds` filter matches.
_subscribers: dict[queue.Queue, frozenset[str]] = {}
_subscribers_lock = threading.Lock()
_dropped_frames = 0

_log_stop: threading.Event | None = None
_log_thread: threading.Thread | None = None
_log_queue: queue.Queue | None = None


class DdcPort:
    """The GNSS receiver (u-blox ZED-F9P) on the I2C bus. Interface for the reader loop:
    in_waiting, read, is_open, close.

    The receiver buffers its output and the host polls it: the register 0xFD / 0xFE holds the
    number of waiting bytes and a read without a register address returns them. Reading more
    than is waiting returns 0xFF filler, so it never does."""

    def __init__(self, bus_path, address: int, poll_s: float):
        self.address = address
        self.poll_s = poll_s
        self.errors = 0  # failed transfers (NACK while the receiver is busy), retried on the next poll
        self._announced = 0
        try:
            self.bus = i2c.acquire(bus_path)
        except OSError as e:
            raise RuntimeError(f"cannot open the GNSS I2C bus {bus_path}: {e}") from e
        self.is_open = True
        try:
            self.bus.read_registers(address, _BYTES_AVAILABLE, 2)  # fails right here if nothing answers
        except OSError as e:
            self.close()
            raise RuntimeError(f"GNSS receiver does not answer on I2C address 0x{address:02X}: {e}") from e

    @property
    def in_waiting(self) -> int:
        """Bytes waiting in the receiver. The following read() takes from this count"""
        try:
            high, low = self.bus.read_registers(self.address, _BYTES_AVAILABLE, 2)
            self._announced = (high << 8) | low
        except OSError:
            self.errors += 1
            self._announced = 0
        return self._announced

    def read(self, size: int = 1) -> bytes:
        """Returns up to `size` waiting bytes, or after the poll interval nothing if none are"""
        n = min(size, self._announced, _MAX_CHUNK)
        if n <= 0:
            time.sleep(self.poll_s)
            return b""
        try:
            data = self.bus.read_stream(self.address, n)
        except OSError:
            self.errors += 1
            self._announced = 0
            return b""
        self._announced -= n
        return data

    def close(self):
        if self.is_open:
            self.is_open = False
            i2c.release(self.bus)


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
        # Returns what the receiver has waiting, or nothing after the poll interval
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
    """Opens the GNSS receiver on the I2C bus and starts the reader thread"""
    global _port, _reader_thread
    if _port and _port.is_open:
        return
    _port = DdcPort(config.GNSS_I2C_BUS, config.GNSS_I2C_ADDRESS, config.GNSS_I2C_POLL_S)
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
    """The receiver is opened and answering on the bus"""
    return _port is not None and _port.is_open

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
