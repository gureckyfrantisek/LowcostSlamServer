# GNSS connection and functions
import logging
import queue
import struct
import threading
import time
from app.core import config, i2c

log = logging.getLogger(__name__)

NMEA = "nmea"
UBX = "ubx"

_UBX_MAX_PAYLOAD = 4096
_NMEA_MAX_LEN = 100

# u-blox configuration keys (F9 interface description). The raw measurements, the time marks of the
# IMU interrupt (the IMU INT1 line is wired to the receiver too) and the PPS on the GPS time grid
# are what the post-processing needs; the NMEA output stays as it is.
_CFG_RATE_MEAS = 0x30210001             # U2: ms between measurements
_CFG_RATE_NAV = 0x30210002              # U2: measurements per navigation solution
_CFG_MSGOUT_RXM_RAWX_I2C = 0x209102A4   # U1: output RXM-RAWX every n-th epoch on I2C
_CFG_MSGOUT_RXM_SFRBX_I2C = 0x20910231
_CFG_MSGOUT_TIM_TM2_I2C = 0x20910178
_CFG_MSGOUT_TIM_TP_I2C = 0x2091017D
_CFG_TP_TIMEGRID_TP1 = 0x2005000C       # 0 = UTC, 1 = GPS
_VALUE_BYTES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}  # key bits 28..30 -> value bytes
_UBX_CFG_VALSET = (0x06, 0x8A)
_LAYER_RAM = 0x01  # gone at a power cycle, so it is applied again at every start

_BYTES_AVAILABLE = 0xFD  # 2 bytes, big endian: how much output the receiver has waiting
_MAX_CHUNK = 128  # a long read would hold the shared bus, and delay the IMU, for too long

_port: "DdcPort | None" = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# A single reader thread owns the receiver's output, splits the byte stream
# into frames and fans each timestamped frame out to every subscriber queue
# whose `kinds` filter matches.
_subscribers: dict[queue.Queue, tuple[frozenset[str], bool]] = {}
_subscribers_lock = threading.Lock()
_dropped_frames = 0
_raw_error: str | None = None  # why the raw output (RAWX, time marks) could not be switched on

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

    def write(self, data: bytes):
        """Sends a command to the receiver"""
        self.bus.write_stream(self.address, data)

    def close(self):
        if self.is_open:
            self.is_open = False
            i2c.release(self.bus)


def ubx_frame(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    body = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload)) + payload
    ck_a = ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xb5\x62" + body + bytes([ck_a, ck_b])


def valset_payload(values: dict[int, int]) -> bytes:
    """UBX-CFG-VALSET payload: version 0, the RAM layer, then the key/value pairs"""
    out = struct.pack("<BBH", 0, _LAYER_RAM, 0)
    for key, value in values.items():
        out += struct.pack("<I", key) + int(value).to_bytes(_VALUE_BYTES[key >> 28 & 7], "little")
    return out


def receiver_settings(rate_hz: int) -> dict[int, int]:
    return {
        _CFG_RATE_MEAS: round(1000 / rate_hz),
        _CFG_RATE_NAV: max(1, round(rate_hz)),  # a position fix at about 1 Hz
        _CFG_MSGOUT_RXM_RAWX_I2C: 1,
        _CFG_MSGOUT_RXM_SFRBX_I2C: 1,
        _CFG_MSGOUT_TIM_TM2_I2C: 1,
        _CFG_MSGOUT_TIM_TP_I2C: 1,
        _CFG_TP_TIMEGRID_TP1: 1,  # PPS and time marks on the GPS time grid
    }


def configure_receiver(port: DdcPort, rate_hz: int, timeout_s: float = 3.0):
    """Switches the raw output on. Raises RuntimeError when the receiver rejects it or does not answer"""
    port.write(ubx_frame(*_UBX_CFG_VALSET, valset_payload(receiver_settings(rate_hz))))
    parser = _FrameParser()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chunk = port.read(port.in_waiting or 1)
        for _, kind, frame in parser.feed(chunk, 0):
            if kind == UBX and frame[2] == 0x05 and frame[6:8] == bytes(_UBX_CFG_VALSET):  # UBX-ACK-ACK / NAK
                if frame[3] == 0x01:
                    return
                raise RuntimeError("the receiver rejected the raw output settings")
    raise RuntimeError("the receiver did not acknowledge the raw output settings")


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


def subscribe(kinds: list[str] | None = None, maxsize: int = 100, with_mono: bool = False) -> queue.Queue:
    """Returns a queue receiving (timestamp_ns, kind, frame_bytes) for every
    frame of the requested kinds, e.g. [NMEA], [UBX] or [NMEA, UBX] (default).
    With_mono adds a fourth value, the CLOCK_MONOTONIC time of the read that completed the frame:
    the clock of the PPS edges, so a PPS second can be labelled with the GPS time of the frame"""
    if kinds is None:
        kinds = [NMEA, UBX]
    elif isinstance(kinds, str):
        kinds = [kinds]  # frozenset("ubx") would split into characters
    q = queue.Queue(maxsize)
    with _subscribers_lock:
        _subscribers[q] = (frozenset(kinds), with_mono)
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
        ts, mono = time.time_ns(), time.monotonic_ns()  # timestamp at receipt, shared by all consumers
        for frame_ts, kind, data in parser.feed(chunk, ts):
            with _subscribers_lock:
                subs = list(_subscribers.items())
            for q, (kinds, with_mono) in subs:
                if kind not in kinds:
                    continue
                try:
                    q.put_nowait((frame_ts, kind, data, mono) if with_mono else (frame_ts, kind, data))
                except queue.Full:
                    _dropped_frames += 1  # slow consumer must never block the reader

def open_gnss_port():
    """Opens the GNSS receiver on the I2C bus and starts the reader thread"""
    global _port, _reader_thread, _raw_error
    if _port and _port.is_open:
        return
    _port = DdcPort(config.GNSS_I2C_BUS, config.GNSS_I2C_ADDRESS, config.GNSS_I2C_POLL_S)
    try:
        configure_receiver(_port, config.GNSS_RATE_HZ)
        _raw_error = None
    except (RuntimeError, OSError) as e:
        _raw_error = str(e)
        log.warning("GNSS raw output not available (%s): NMEA only, no post-processing", e)
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

def get_stats() -> dict:
    return dict(errors=_port.errors if _port else 0, dropped_frames=_dropped_frames,
                raw_output=_raw_error is None and _port is not None, raw_output_error=_raw_error)

def verify_connection():
    """The receiver is opened and answering on the bus"""
    return _port is not None and _port.is_open

def start_logging(project_path) -> int:
    """Logs the raw UBX stream untouched to gnss.ubx, plus gnss_index.csv
    mapping every frame to the host clock"""
    global _log_stop, _log_thread, _log_queue

    if _port is None or not _port.is_open:
        raise RuntimeError("GNSS port is not open")

    _log_queue = subscribe(kinds=[UBX], maxsize=10000, with_mono=True)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue

    def _log():
        with open(f"{project_path}/gnss.ubx", "wb") as raw, \
             open(f"{project_path}/gnss_index.csv", "w") as index:
            index.write("timestamp_ns,mono_ns,offset,length,ubx_class,ubx_id\n")
            offset = 0
            # Keep draining after stop is requested so no queued frame is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, _, frame, mono = q.get(timeout=0.1)
                except queue.Empty:
                    raw.flush()
                    index.flush()
                    continue
                raw.write(frame)
                index.write(f"{ts},{mono},{offset},{len(frame)},{frame[2]},{frame[3]}\n")
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
