# IMU connection and functions: a LIS3DH accelerometer on the I2C bus
import queue
import threading
import time
from app.core import config, i2c

# Columns of the log file, in order. The values are the raw sensor output (no calibration or
# correction): 12 bit counts, 1 mg per count at +-2 g in high resolution mode.
LOG_COLUMNS = ["ts", "aX", "aY", "aZ"]

_WHO_AM_I = 0x0F
_CTRL_REG1 = 0x20
_CTRL_REG4 = 0x23
_STATUS_REG = 0x27  # followed by OUT_X_L ... OUT_Z_H, so one burst reads the status and the sample
_AUTO_INCREMENT = 0x80  # set on the register address to read consecutive registers
_ZYXDA = 0x08  # status bit: a new sample of all three axes is ready

_LIS3DH_ID = 0x33
_ODR_CODES = {1: 0x1, 10: 0x2, 25: 0x3, 50: 0x4, 100: 0x5, 200: 0x6, 400: 0x7}  # CTRL_REG1 bits 7:4


class Lis3dh:
    """LIS3DH accelerometer on the I2C bus. Interface for the reader loop: is_open, poll_s,
    read_line(), close()."""

    def __init__(self, bus_path, address: int, hz: int):
        if hz not in _ODR_CODES:
            raise ValueError(f"IMU_I2C_HZ must be one of {sorted(_ODR_CODES)}")
        self.address = address
        self.hz = hz
        self.poll_s = 1 / (2 * hz)  # twice per sample, the status bit says which poll has a new one
        self.errors = 0  # failed transfers, retried on the next poll
        try:
            self.bus = i2c.acquire(bus_path)
        except OSError as e:
            raise RuntimeError(f"cannot open the IMU I2C bus {bus_path}: {e}") from e
        self.is_open = True
        try:
            if self.bus.read_registers(address, _WHO_AM_I, 1)[0] != _LIS3DH_ID:
                raise RuntimeError(f"no LIS3DH at I2C address 0x{address:02X} (wrong WHO_AM_I)")
            self.bus.write_register(address, _CTRL_REG1, (_ODR_CODES[hz] << 4) | 0x07)  # rate, normal mode, XYZ on
            self.bus.write_register(address, _CTRL_REG4, 0x88)  # block data update, +-2 g, high resolution
        except (OSError, RuntimeError) as e:
            self.close()
            if isinstance(e, OSError):
                raise RuntimeError(f"IMU does not answer on I2C address 0x{address:02X}: {e}") from e
            raise

    def read_line(self) -> tuple[int, bytes] | None:
        """Returns (timestamp_ns, line) of a new sample, or None if there is none yet"""
        try:
            data = self.bus.read_registers(self.address, _STATUS_REG | _AUTO_INCREMENT, 7)
        except OSError:
            self.errors += 1
            return None
        ts = time.time_ns()  # right after the transfer, shared by all consumers
        if not data[0] & _ZYXDA:
            return None
        x, y, z = (_signed(data[i], data[i + 1]) >> 4 for i in (1, 3, 5))  # 12 bit value
        return ts, f"{x},{y},{z}".encode()

    def close(self):
        if self.is_open:
            self.is_open = False
            try:
                self.bus.write_register(self.address, _CTRL_REG1, 0x00)  # power down
            except OSError:
                pass
            i2c.release(self.bus)


def _signed(low: int, high: int) -> int:
    value = (high << 8) | low
    return value - 65536 if value & 0x8000 else value


_port: Lis3dh | None = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None

# A single reader thread owns the sensor and fans each timestamped line out
# to every subscriber queue.
_subscribers: set[queue.Queue] = set()
_subscribers_lock = threading.Lock()
_dropped_lines = 0

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

def _publish(ts: int, line: bytes):
    """Fans one timestamped line out to every subscriber"""
    global _dropped_lines
    with _subscribers_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait((ts, line))
        except queue.Full:
            _dropped_lines += 1  # slow consumer must never block the reader

def _read_loop():
    while not _reader_stop.is_set():
        sample = _port.read_line()  # (timestamp_ns, line), taken right after the transfer
        if sample:
            _publish(*sample)
        else:
            _reader_stop.wait(_port.poll_s)  # no new sample yet

def open_imu_port():
    """Opens the IMU on the I2C bus and starts the reader thread"""
    global _port, _reader_thread
    if _port and _port.is_open:
        return
    _port = Lis3dh(config.IMU_I2C_BUS, config.IMU_I2C_ADDRESS, config.IMU_I2C_HZ)
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
    """The sensor is opened and answering on the bus"""
    return is_port_open()

def start_logging(project_path) -> int:
    global _log_stop, _log_thread, _log_queue

    if _port is None or not _port.is_open:
        raise RuntimeError("IMU port is not open")

    _log_queue = subscribe(maxsize=10000)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue

    def _log():
        file_path = f"{project_path}/imu.csv"

        with open(file_path, "w") as f:
            f.write(",".join(LOG_COLUMNS) + "\n")
            # Keep draining after stop is requested so no queued line is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, line = q.get(timeout=0.1)
                except queue.Empty:
                    f.flush()
                    continue
                f.write(f"{ts},{line.decode()}\n")  # raw values, stored as they come

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
