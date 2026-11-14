# IMU connection and functions: a LIS3DH accelerometer on the I2C bus.
#
# The sensor buffers its samples in a 32 sample FIFO and raises INT1 when it holds a few of them
# (the watermark). The kernel time-stamps that edge, one read per edge takes the samples out and
# the samples between two edges get interpolated times (app/core/timing.py). A late read costs no
# samples, the FIFO holds 58 ms at 400 Hz, and the reader wakes about 44 times a second instead of
# once per sample. See docs/timing.md for the measurements behind this.
import glob
import json
import logging
import queue
import select
import threading
import time
from typing import NamedTuple

from app.core import config, i2c
from app.core.timing import EdgeNumbering, PmQos, SampleClock

log = logging.getLogger(__name__)

# Columns of the log file, in order. ts is the wall clock, mono_ns the CLOCK_MONOTONIC time the
# PPS edges of pps.csv are on (the GPS time of a sample is found through them). The values are
# the raw sensor output (no calibration or correction): 12 bit counts, see imu_info.json for mg per count.
LOG_COLUMNS = ["ts", "mono_ns", "aX", "aY", "aZ"]

_WHO_AM_I = 0x0F
_CTRL_REG1 = 0x20
_CTRL_REG3 = 0x22
_CTRL_REG4 = 0x23
_CTRL_REG5 = 0x24
_OUT_X_L = 0x28  # ... OUT_Z_H, read with the auto increment bit
_FIFO_CTRL = 0x2E
_FIFO_SRC = 0x2F  # bit 6: FIFO full (samples were lost), bits 4..0: unread samples
_AUTO_INCREMENT = 0x80  # set on the register address to read consecutive registers

_LIS3DH_ID = 0x33
_ODR_CODES = {1: 0x1, 10: 0x2, 25: 0x3, 50: 0x4, 100: 0x5, 200: 0x6, 400: 0x7}  # CTRL_REG1 bits 7:4
_RANGES = {2: (0x0, 1), 4: (0x1, 2), 8: (0x2, 4), 16: (0x3, 12)}  # +-g -> (CTRL_REG4 FS, mg per count)
_I1_WTM = 0x04  # CTRL_REG3: FIFO watermark on INT1
_FIFO_EN = 0x40  # CTRL_REG5
_FIFO_STREAM = 0x80  # FIFO_CTRL: stream mode, the oldest sample is dropped when the FIFO is full
_FIFO_DEPTH = 32
_SETTLE_SAMPLES = 7  # high resolution mode settles after 7 samples (datasheet table 12)


class Drain(NamedTuple):
    samples: list  # [(x, y, z)] 12 bit counts, oldest first
    first_fill: int  # unread samples at the first look
    look_ns: int  # CLOCK_MONOTONIC at the first look
    read_ns: int  # CLOCK_MONOTONIC after the last read
    overrun: bool  # the FIFO was full: older samples were lost


class Lis3dh:
    """LIS3DH accelerometer on the I2C bus, with its FIFO and watermark interrupt on"""

    def __init__(self, bus_path, address: int, hz: int, range_g: int = 2, fifo_threshold: int = 8):
        if hz not in _ODR_CODES:
            raise ValueError(f"IMU_I2C_HZ must be one of {sorted(_ODR_CODES)}")
        if range_g not in _RANGES:
            raise ValueError(f"IMU_RANGE_G must be one of {sorted(_RANGES)}")
        if not 1 <= fifo_threshold < _FIFO_DEPTH - 1:
            raise ValueError(f"IMU_FIFO_THRESHOLD must be 1 to {_FIFO_DEPTH - 2}")
        self.address = address
        self.hz = hz
        self.range_g = range_g
        self.mg_per_count = _RANGES[range_g][1]
        self.fifo_threshold = fifo_threshold
        self.errors = 0  # failed transfers
        try:
            self.bus = i2c.acquire(bus_path)
        except OSError as e:
            raise RuntimeError(f"cannot open the IMU I2C bus {bus_path}: {e}") from e
        self.is_open = True
        try:
            if self.bus.read_registers(address, _WHO_AM_I, 1)[0] != _LIS3DH_ID:
                raise RuntimeError(f"no LIS3DH at I2C address 0x{address:02X} (wrong WHO_AM_I)")
            self._write(_CTRL_REG4, 0x80 | (_RANGES[range_g][0] << 4) | 0x08)  # block data update, range, high resolution
            self._write(_CTRL_REG1, (_ODR_CODES[hz] << 4) | 0x07)  # rate, normal mode, XYZ on
            self._write(_FIFO_CTRL, 0)  # bypass: empties the FIFO
            self._write(_CTRL_REG5, _FIFO_EN)
            self._write(_FIFO_CTRL, _FIFO_STREAM | fifo_threshold)
            self._write(_CTRL_REG3, _I1_WTM)  # INT1 rises when the FIFO holds threshold + 1 samples
        except (OSError, RuntimeError) as e:
            self.close()
            if isinstance(e, OSError):
                raise RuntimeError(f"IMU does not answer on I2C address 0x{address:02X}: {e}") from e
            raise

    def _write(self, register: int, value: int):
        self.bus.write_register(self.address, register, value)

    @property
    def edge_level(self) -> int:
        """FIFO fill at which INT1 rises (measured: threshold + 1)"""
        return self.fifo_threshold + 1

    @property
    def settle_s(self) -> float:
        return _SETTLE_SAMPLES / self.hz

    def drain(self) -> Drain:
        """Reads the FIFO empty. Raises OSError if a transfer fails (what was read is lost)"""
        samples, first, overrun = [], None, False
        while True:
            src = self.bus.read_registers(self.address, _FIFO_SRC, 1)[0]
            now = time.monotonic_ns()
            n = src & 0x1F
            if src & 0x40:
                overrun = True
                n = n or _FIFO_DEPTH  # 32 does not fit the 5 bit count
            if first is None:
                first = (n, now)
            if n == 0:
                return Drain(samples, first[0], first[1], now, overrun)
            data = self.bus.read_registers(self.address, _OUT_X_L | _AUTO_INCREMENT, 6 * n)
            for i in range(n):
                x, y, z = (_signed(data[6 * i + k], data[6 * i + k + 1]) >> 4 for k in (0, 2, 4))
                samples.append((x, y, z))

    def close(self):
        if self.is_open:
            self.is_open = False
            try:
                self._write(_CTRL_REG3, 0)
                self._write(_CTRL_REG5, 0)
                self._write(_FIFO_CTRL, 0)
                self._write(_CTRL_REG1, 0x00)  # power down
            except OSError:
                pass
            i2c.release(self.bus)


def _signed(low: int, high: int) -> int:
    value = (high << 8) | low
    return value - 65536 if value & 0x8000 else value


# --- GPIO edges --------------------------------------------------------------------------------

class Edge(NamedTuple):
    line: int
    ns: int  # CLOCK_MONOTONIC, taken by the kernel in the interrupt handler
    seqno: int  # counts the edges of this line, a jump means the kernel lost some


class GpioEdges:
    """Rising edges of the PPS line and the IMU interrupt line, time-stamped by the kernel"""

    def __init__(self, chip_path: str, lines: tuple[int, ...]):
        import gpiod  # imported here so the module loads without libgpiod (Linux only)
        from gpiod.line import Direction, Edge as GpiodEdge
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=GpiodEdge.RISING)
        self._request = gpiod.request_lines(chip_path, consumer="lowcost-slam",
                                            config={tuple(lines): settings}, event_buffer_size=1024)
        self.fd = self._request.fd

    def read(self) -> list[Edge]:
        return [Edge(e.line_offset, e.timestamp_ns, e.line_seqno) for e in self._request.read_edge_events()]

    def close(self):
        self._request.release()


def find_gpio_chip() -> str:
    """The device of the header pin controller (INT33FF:02). /dev/gpiochipN changes between boots"""
    if config.GPIO_CHIP:
        return config.GPIO_CHIP
    import gpiod
    for path in sorted(glob.glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(path) as chip:
                if chip.get_info().label == config.GPIO_CHIP_LABEL:
                    return path
        except OSError:
            continue
    raise RuntimeError(f"no GPIO chip labelled {config.GPIO_CHIP_LABEL}")


def open_edges() -> tuple[GpioEdges | None, str | None]:
    """The edge source, or (None, why not). Without it the reader polls the FIFO on a timer and the
    sample times are only good to about a sample period"""
    try:
        return GpioEdges(find_gpio_chip(), (config.GPIO_LINE_PPS, config.GPIO_LINE_IMU_INT)), None
    except Exception as e:  # missing libgpiod, no permission, line busy, wrong chip
        return None, f"{type(e).__name__}: {e}"


# --- the reader --------------------------------------------------------------------------------

class Reader:
    """Takes the samples out of the sensor and puts a time on each of them. `step()` is one turn of
    the reader thread: wait for an edge (or a timer), read, time, publish."""

    def __init__(self, port: Lis3dh, edges, publish, publish_pps):
        self.port, self.edges = port, edges
        self.publish, self.publish_pps = publish, publish_pps
        self.clock = SampleClock(port.hz)
        self.numbering = EdgeNumbering(port.hz, port.edge_level)
        # time out after two watermark periods without an edge
        self.wake_ms = max(1, 2 * port.fifo_threshold * 1000 // port.hz)
        self.stats = dict(samples=0, edges=0, lost_edges=0, lost_samples=0, overruns=0, gaps=0,
                          rescued=0, pps=0, errors=0)
        self.wall_offset_ns = 0
        self._next_offset = 0.0
        self._last_seqno: dict[int, int] = {}
        self._poller = None
        self.ignore_before = 0
        self._lock = threading.Lock()  # step() runs in the reader thread, finish() can come from another

    def start(self):
        """Lets the sensor settle, drops what it made so far, and starts the edge detection"""
        time.sleep(self.port.settle_s)
        self.port.drain()
        self.ignore_before = time.monotonic_ns()  # edges of the settling samples are dropped
        if self.edges:
            self._poller = select.poll()
            self._poller.register(self.edges.fd, select.POLLIN)
        self._refresh_offset()

    def _refresh_offset(self):
        if time.monotonic() >= self._next_offset:
            self.wall_offset_ns = time.time_ns() - time.monotonic_ns()
            self._next_offset = time.monotonic() + 1

    def _emit(self, timed):
        for mono, (x, y, z) in timed:
            self.stats["samples"] += 1
            self.publish(mono + self.wall_offset_ns, f"{mono},{x},{y},{z}".encode())

    def _gap(self, drain: Drain):
        """Samples were lost: time what waits and start the timing over with the next edge"""
        self.stats["gaps"] += 1
        self._emit(self.clock.restart(drain.read_ns))
        self.numbering.restart()

    def step(self):
        with self._lock:
            self._refresh_offset()
            try:
                if self.edges:
                    self._step_edges()
                else:
                    self._step_poll()
            except OSError:
                self.stats["errors"] += 1
                self.port.errors += 1
                self._emit(self.clock.restart(time.monotonic_ns()))  # what was read is timed, the rest is a gap
                self.numbering.restart()
                time.sleep(0.01)

    def _step_edges(self):
        if not self._poller.poll(self.wake_ms):
            # no watermark edge for two thresholds' worth of samples: look anyway
            drain = self.port.drain()
            self.clock.add_samples(drain.samples)
            if drain.first_fill >= self.port.fifo_threshold:
                self.stats["rescued"] += 1
            if drain.overrun:
                self.stats["overruns"] += 1
                self._gap(drain)
            return
        watermark = []
        for edge in self.edges.read():
            last = self._last_seqno.get(edge.line)
            if last is not None and edge.seqno > last + 1 and edge.line == config.GPIO_LINE_IMU_INT:
                self.stats["lost_edges"] += edge.seqno - last - 1
            self._last_seqno[edge.line] = edge.seqno
            if edge.line == config.GPIO_LINE_PPS:
                self.stats["pps"] += 1
                self.publish_pps(edge.ns, edge.seqno)
            elif edge.line == config.GPIO_LINE_IMU_INT and edge.ns >= self.ignore_before:
                watermark.append(edge.ns)
                self.stats["edges"] += 1
        if not watermark:
            return
        popped_before = self.clock.next_index
        numbers = [self.numbering.number(ns, popped_before) for ns in watermark]
        drain = self.port.drain()
        self.clock.add_samples(drain.samples)
        lost = numbers[0] - self.port.edge_level - popped_before  # samples the sensor made and we never read
        if drain.overrun or lost > 0:
            self.stats["overruns"] += int(drain.overrun)
            self.stats["lost_samples"] += max(lost, 0)
            self._gap(drain)
            return
        timed = []
        for number, ns in zip(numbers, watermark):
            timed += self.clock.add_anchor(number - 1, ns)
        self._emit(timed)

    def _step_poll(self):
        """No edges: read on a timer. The newest sample is taken to be the time of the first look"""
        time.sleep(self.wake_ms / 1000)
        drain = self.port.drain()
        self.clock.add_samples(drain.samples)
        if drain.overrun:
            self.stats["overruns"] += 1
            self._gap(drain)
            return
        if drain.samples:
            self._emit(self.clock.add_anchor(self.clock.next_index - 1, drain.look_ns))

    def finish(self):
        """Reads what is left and times it (the last samples would wait for an edge that comes later)"""
        with self._lock:
            try:
                drain = self.port.drain()
                self.clock.add_samples(drain.samples)
                now = drain.read_ns
            except OSError:
                now = time.monotonic_ns()
            self._emit(self.clock.flush(now))


_port: Lis3dh | None = None
_reader: Reader | None = None
_edges: GpioEdges | None = None
_edges_error: str | None = None
_reader_stop: threading.Event = threading.Event()
_reader_thread: threading.Thread | None = None
_qos = PmQos(config.PM_QOS_DEVICE, config.PM_QOS_LATENCY_US)

# A single reader thread owns the sensor and fans each timestamped line out
# to every subscriber queue.
_subscribers: set[queue.Queue] = set()
_pps_subscribers: set[queue.Queue] = set()
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
        _pps_subscribers.discard(q)

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

def _publish_pps(mono_ns: int, seqno: int):
    with _subscribers_lock:
        subs = list(_pps_subscribers)
    for q in subs:
        try:
            q.put_nowait((mono_ns, seqno))
        except queue.Full:
            pass

def _read_loop():
    while not _reader_stop.is_set():
        _reader.step()
    _reader.finish()

def open_imu_port():
    """Opens the IMU on the I2C bus and starts the reader thread"""
    global _port, _reader, _edges, _edges_error, _reader_thread
    if _port and _port.is_open:
        return
    _port = Lis3dh(config.IMU_I2C_BUS, config.IMU_I2C_ADDRESS, config.IMU_I2C_HZ,
                   config.IMU_RANGE_G, config.IMU_FIFO_THRESHOLD)
    _edges, _edges_error = open_edges()
    if _edges is None:
        log.warning("no GPIO edges (%s): IMU samples are timed by polling, good to about a sample period",
                    _edges_error)
    _reader = Reader(_port, _edges, _publish, _publish_pps)
    try:
        _reader.start()
    except OSError as e:
        _release()
        raise RuntimeError(f"IMU does not answer on I2C address 0x{config.IMU_I2C_ADDRESS:02X}: {e}") from e
    _reader_stop.clear()
    _reader_thread = threading.Thread(target=_read_loop, daemon=True)
    _reader_thread.start()

def _release():
    global _port, _reader, _edges
    if _edges:
        _edges.close()
        _edges = None
    if _port and _port.is_open:
        _port.close()
    _port = None
    _reader = None

def close_imu_port():
    """Stops logging and the reader, then closes the port for clean shutdown"""
    global _reader_thread
    stop_logging()
    _reader_stop.set()
    if _reader_thread:
        _reader_thread.join()
        _reader_thread = None
    _release()

def is_port_open() -> bool:
    return _port is not None and _port.is_open

def is_logging() -> bool:
    return _log_thread is not None

def verify_connection():
    """The sensor is opened and the reader is running"""
    return is_port_open() and _reader_thread is not None and _reader_thread.is_alive()

def get_stats() -> dict:
    """Counters of the reader since the port was opened, and how the samples are timed"""
    stats = dict(_reader.stats) if _reader else {}
    stats.update(timing="edges" if _edges else "polling", timing_error=_edges_error,
                 pm_qos=_qos.active, pm_qos_error=_qos.error, dropped_lines=_dropped_lines)
    return stats

def start_logging(project_path) -> int:
    global _log_stop, _log_thread, _log_queue

    if _port is None or not _port.is_open:
        raise RuntimeError("IMU port is not open")

    _qos.acquire()  # no deep C-states while recording
    _log_queue = subscribe(maxsize=10000)
    pps_queue = queue.Queue(1000)
    with _subscribers_lock:
        _pps_subscribers.add(pps_queue)
    _log_stop = threading.Event()
    stop, q = _log_stop, _log_queue
    stats_before = dict(_reader.stats)
    info = dict(odr_hz=_port.hz, range_g=_port.range_g, mg_per_count=_port.mg_per_count,
                fifo_threshold=_port.fifo_threshold, columns=LOG_COLUMNS,
                timing="edges" if _edges else "polling", timing_error=_edges_error)

    def _log():
        reader, last_flush = _reader, time.monotonic()
        with open(f"{project_path}/imu.csv", "w") as f, open(f"{project_path}/pps.csv", "w") as pps:
            f.write(",".join(LOG_COLUMNS) + "\n")
            pps.write("mono_ns,seqno\n")
            # Keep draining after stop is requested so no queued line is lost
            while not stop.is_set() or not q.empty():
                try:
                    ts, line = q.get(timeout=0.1)
                    f.write(f"{ts},{line.decode()}\n")  # raw values, stored as they come
                except queue.Empty:
                    pass
                while not pps_queue.empty():
                    pps.write("%d,%d\n" % pps_queue.get_nowait())
                if time.monotonic() - last_flush >= 1.0:
                    f.flush()
                    pps.flush()
                    last_flush = time.monotonic()
        counters = {k: reader.stats[k] - stats_before[k] for k in reader.stats}
        with open(f"{project_path}/imu_info.json", "w") as f:
            json.dump({**info, "counters": counters, "pm_qos": _qos.active, "pm_qos_error": _qos.error},
                      f, indent=2)

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
    if _log_thread and _reader:
        _reader.finish()  # the newest samples are published before the logger stops
    if _log_queue:
        unsubscribe(_log_queue)
        _log_queue = None
    if _log_stop:
        _log_stop.set()
    if _log_thread:
        _log_thread.join()
        _log_thread = None
        _qos.release()
