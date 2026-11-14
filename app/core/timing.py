# Sample timing for the FIFO-buffered IMU, as validated by scripts/bench (tests T1 to T5):
#  - the kernel time-stamps the FIFO watermark edge of the sensor in its interrupt handler
#    (CLOCK_MONOTONIC, p99 16 us from the GNSS truth with the PM QoS request below)
#  - the samples between two edges get their time by linear interpolation between the edges,
#    which follows the real sample clock (it runs 0.5 % off nominal and wanders by 200 ppm)
#  - GPS time is not needed live: the PPS edges are logged on the same clock, so every time can be
#    mapped to GPS time afterwards
import collections
import logging
import os
import struct
from typing import Iterable

log = logging.getLogger(__name__)

NS = 1_000_000_000


class SampleClock:
    """Gives FIFO samples their time from anchors (sample index -> CLOCK_MONOTONIC ns).

    Samples are added in the order the sensor made them, anchors as they are measured. A sample is
    released when the anchor behind it is known, so it waits for at most one watermark period (two
    at the start). Samples before the first anchor are timed with the period of the first segment,
    the ones after the last anchor (flush) with the period of the last segment."""

    def __init__(self, odr_hz: float):
        self.nominal_period_ns = NS / odr_hz
        self._pending = collections.deque()  # (index, sample)
        self._next_index = 0
        self._anchor: tuple[int, int] | None = None  # last anchor (index, ns)
        self._period_ns: float | None = None  # of the last segment

    @property
    def next_index(self) -> int:
        """Index the next sample gets"""
        return self._next_index

    def add_samples(self, samples: Iterable):
        for sample in samples:
            self._pending.append((self._next_index, sample))
            self._next_index += 1

    def add_anchor(self, index: int, ns: int) -> list[tuple[int, object]]:
        """Sample `index` was made at `ns`. Returns the (ns, sample) of the samples that are now timed"""
        if self._anchor is None:
            self._anchor = (index, ns)  # the samples before it wait for the second anchor
            return []
        i0, t0 = self._anchor
        if index <= i0:
            return []  # two edges for one sample, or out of order: keep the first
        period = (ns - t0) / (index - i0)
        out = self._release(index, i0, t0, period)
        self._anchor, self._period_ns = (index, ns), period
        return out

    def _release(self, upto: int, i0: int, t0: int, period: float) -> list[tuple[int, object]]:
        out = []
        while self._pending and self._pending[0][0] <= upto:
            i, sample = self._pending.popleft()
            out.append((round(t0 + (i - i0) * period), sample))
        return out

    def flush(self, now_ns: int) -> list[tuple[int, object]]:
        """Times everything still waiting, by extrapolation. `now_ns` is when the last sample had
        been read: it is only used when no anchor exists at all"""
        out = []
        if self._pending:
            if self._anchor is not None:
                i0, t0 = self._anchor
                period = self._period_ns or self.nominal_period_ns
            else:
                last = self._pending[-1][0]
                i0, t0, period = last, now_ns, self.nominal_period_ns
            while self._pending:
                i, sample = self._pending.popleft()
                out.append((round(t0 + (i - i0) * period), sample))
        return out

    def restart(self, now_ns: int) -> list[tuple[int, object]]:
        """A gap in the samples (FIFO overflow, bus error): times what waits and forgets the anchors,
        the timing starts over with the next edge"""
        out = self.flush(now_ns)
        self._anchor = self._period_ns = None
        return out


class EdgeNumbering:
    """Which sample raised a watermark edge.

    The sensor raises INT1 when the FIFO reaches threshold + 1 samples (measured on the ROCK Pi X,
    the datasheet only says "equal or higher"), so after a read that emptied the FIFO the first edge
    is sample `popped + level`. The following ones add the time since the previous edge in sample
    periods. The sensor clock is within about 1 % of nominal, so even edges 50 samples apart are
    unambiguous. A missed edge just makes the step longer."""

    REFINE_AFTER = 200  # samples between the first and last edge before the measured period is used

    def __init__(self, odr_hz: float, level: int):
        self.level = level
        self._nominal_ns = NS / odr_hz
        self.restart()

    def restart(self):
        self._period_ns = self._nominal_ns
        self._first: tuple[int, int] | None = None  # (number, ns) of the first edge
        self._last: tuple[int, int] | None = None

    def number(self, ns: int, popped_before: int) -> int:
        """1-based number of the sample that raised the edge at `ns`. `popped_before` is how many
        samples had been read, the FIFO being empty, before the edge"""
        if self._last is None:
            number = popped_before + self.level
            self._first = (number, ns)
        else:
            number = self._last[0] + max(1, round((ns - self._last[1]) / self._period_ns))
            if number - self._first[0] >= self.REFINE_AFTER:
                self._period_ns = (ns - self._first[1]) / (number - self._first[0])
        self._last = (number, ns)
        return number


class PmQos:
    """Keeps the CPUs out of the deep C-states while held. Their wake-up takes up to 10 ms and delays
    the interrupt time stamps by up to ~1 ms. Needs write access to /dev/cpu_dma_latency (the udev
    rule of scripts/bench/setup_i2c_gpio.sh gives it to the gpio group)."""

    def __init__(self, device: str, latency_us: int):
        self.device, self.latency_us = device, latency_us
        self._fd: int | None = None
        self._holders = 0
        self.error: str | None = None  # why the request failed, None while it works or was never made

    def acquire(self):
        self._holders += 1
        if self._fd is None:
            try:
                fd = os.open(self.device, os.O_WRONLY)
                os.write(fd, struct.pack("=i", self.latency_us))
                self._fd, self.error = fd, None
            except OSError as e:
                self.error = f"{self.device}: {e.strerror or e}"
                log.warning("PM QoS not available (%s): interrupt time stamps can jitter by up to 1 ms. "
                            "Run scripts/bench/setup_i2c_gpio.sh once", self.error)

    def release(self):
        self._holders = max(0, self._holders - 1)
        if self._holders == 0 and self._fd is not None:
            os.close(self._fd)  # closing the file drops the request
            self._fd = None

    @property
    def active(self) -> bool:
        return self._fd is not None
