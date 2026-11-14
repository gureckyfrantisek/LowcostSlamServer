# FIFO reader and sample timing against a simulated sensor clock, edges and bus (no hardware)
import json
import os
import random

import pytest

from app.core import config, imu
from app.core.imu import Reader
from app.core.timing import EdgeNumbering, PmQos, SampleClock
from tests.fakes import FakeBus, FakeEdges

NS = 1_000_000_000
ODR = 100
TRUE_ODR = 100.75           # the sensor clock runs off nominal, as measured on the board (+7450 ppm)
T0 = 5 * NS                 # CLOCK_MONOTONIC when the sensor made sample 1
LATENCY_NS = 20_000         # interrupt latency
LEVEL = 9


def arrival(number):
    """CLOCK_MONOTONIC when the sensor made the sample with the 1-based `number`"""
    return round(T0 + (number - 1) * NS / TRUE_ODR)


# --- the pure parts ----------------------------------------------------------------------------

def test_clock_interpolates_between_anchors_on_the_real_sample_grid():
    clock = SampleClock(ODR)
    clock.add_samples(range(40))
    assert clock.add_anchor(9, arrival(10)) == []           # waits for the second anchor
    out = clock.add_anchor(18, arrival(19))
    out += clock.add_anchor(27, arrival(28))
    assert [s for _, s in out] == list(range(28))
    for t, s in out:
        assert abs(t - arrival(s + 1)) <= 1                 # also before the first anchor: its segment's period


def test_clock_holds_samples_until_the_anchor_behind_them_is_known():
    clock = SampleClock(ODR)
    clock.add_samples(range(20))
    assert clock.add_anchor(9, arrival(10)) == []
    assert clock.add_anchor(9, arrival(10)) == []           # a duplicate edge changes nothing
    assert len(clock.add_anchor(19, arrival(20))) == 20 and clock.flush(0) == []


def test_clock_flush_extrapolates_the_tail_with_the_last_period():
    clock = SampleClock(ODR)
    clock.add_samples(range(30))
    clock.add_anchor(9, arrival(10))
    clock.add_anchor(18, arrival(19))
    tail = clock.flush(0)
    assert [s for _, s in tail] == list(range(19, 30))
    assert abs(tail[-1][0] - arrival(30)) <= 2


def test_clock_flush_without_any_anchor_counts_back_from_now():
    clock = SampleClock(ODR)
    clock.add_samples(range(5))
    out = clock.flush(1_000_000_000)
    assert out[-1][0] == 1_000_000_000 and out[0][0] == 1_000_000_000 - 4 * 10_000_000


def test_clock_restart_times_the_rest_and_forgets_the_anchors():
    clock = SampleClock(ODR)
    clock.add_samples(range(15))
    clock.add_anchor(9, arrival(10))
    clock.add_anchor(12, arrival(13))
    assert len(clock.restart(arrival(16))) == 2             # samples 13 and 14 were waiting
    clock.add_samples(range(15, 25))
    assert clock.add_anchor(24, arrival(30)) == []          # the first anchor again waits for a second one
    assert [s for _, s in clock.add_anchor(33, arrival(39))] == list(range(15, 25))


def test_edge_numbering_follows_the_sample_grid_with_missed_edges():
    numbering = EdgeNumbering(ODR, LEVEL)
    assert numbering.number(arrival(10), 1) == 10           # popped 1 + level 9
    assert numbering.number(arrival(19), 10) == 19
    assert numbering.number(arrival(46), 19) == 46          # three edges missed: the step is longer
    for number in range(55, 400, 9):
        assert numbering.number(arrival(number), number - LEVEL) == number
    numbering.restart()
    assert numbering.number(arrival(500), 491) == 500       # starts over from the read count


def test_pm_qos_holds_and_releases_the_request(tmp_path):
    device = tmp_path / "cpu_dma_latency"
    qos = PmQos(str(device), 10)
    device.write_bytes(b"")
    qos.acquire()
    qos.acquire()
    assert qos.active and qos.error is None and device.read_bytes() == (10).to_bytes(4, "little")
    qos.release()
    assert qos.active                                       # still held by the second user
    qos.release()
    assert not qos.active


def test_pm_qos_failure_is_reported_not_raised(tmp_path):
    qos = PmQos(str(tmp_path / "missing" / "cpu_dma_latency"), 10)
    qos.acquire()
    assert not qos.active and "cpu_dma_latency" in qos.error
    qos.release()


# --- the reader with simulated sensor and edges ------------------------------------------------

class Rig:
    """A sensor producing samples on the real sample grid, edges stamped with interrupt jitter"""

    def __init__(self, bus, jitter_ns=5_000, seed=1):
        self.bus, self.rng = bus, random.Random(seed)
        self.jitter_ns = jitter_ns
        self.port = imu.Lis3dh("imu", 0x18, ODR)
        self.edges = FakeEdges()
        self.out, self.pps = [], []
        self.reader = Reader(self.port, self.edges, lambda ts, line: self.out.append((ts, line)),
                             lambda ns, seq: self.pps.append((ns, seq)))
        self.reader.wake_ms = 1
        self.reader.ignore_before = 0
        self.made = 0           # samples the sensor made
        self.seq = 0
        self.pps_seq = 0
        self.reader.start = lambda: None
        self.reader._poller = __import__("select").poll()
        self.reader._poller.register(self.edges.fd, 1)

    def make(self, n):
        """The sensor makes n samples (the sample value is its number)"""
        self.bus.produce([(self.made + i + 1, 0, 0) for i in range(n)])
        self.made += n

    def edge(self, number):
        self.seq += 1
        self.edges.push(config.GPIO_LINE_IMU_INT, arrival(number) + LATENCY_NS + round(self.rng.gauss(0, self.jitter_ns)),
                        self.seq)

    def pps_edge(self, ns):
        self.pps_seq += 1
        self.edges.push(config.GPIO_LINE_PPS, ns, self.pps_seq)

    def run_normal(self, samples):
        """Every 9 samples the FIFO reaches the level, the edge rises, the reader runs"""
        while self.made + LEVEL <= samples:
            self.make(LEVEL)
            self.edge(self.made)
            self.reader.step()

    def close(self):
        self.reader.finish()
        self.edges.close()
        self.port.close()

    def times(self):
        """(sample value, time error against the truth in us), the line is mono,x,y,z"""
        out = []
        for ts, line in self.out:
            mono, x, *_ = line.decode().split(",")
            out.append((int(x), (int(mono) - arrival(int(x)) - LATENCY_NS) / 1000))
        return out


@pytest.fixture
def rig(bus):
    r = Rig(bus)
    yield r
    r.edges.closed or r.close()


def test_every_sample_is_read_once_in_order_with_a_time_near_the_truth(rig):
    rig.run_normal(900)
    rig.close()
    values = [v for v, _ in rig.times()]
    assert values == list(range(1, len(values) + 1)) and len(values) >= 895
    errors = [abs(e) for _, e in rig.times()[9:]]
    assert max(errors) < 60                                   # us, edge jitter is 5 us (sigma)
    assert sorted(errors)[int(0.99 * len(errors))] < 25
    stats = rig.reader.stats
    assert stats["gaps"] == stats["lost_samples"] == stats["overruns"] == stats["lost_edges"] == 0


def test_a_late_read_costs_no_samples(rig):
    rig.make(9)
    rig.edge(9)
    rig.make(20)                      # the reader was busy: 29 samples wait, the FIFO holds 32
    rig.reader.step()
    rig.make(9)
    rig.edge(38)
    rig.reader.step()
    rig.run_normal(300)
    rig.close()
    values = [v for v, _ in rig.times()]
    assert values == list(range(1, len(values) + 1))
    assert rig.reader.stats["gaps"] == 0


def test_a_missed_edge_does_not_break_the_timing(rig):
    rig.run_normal(180)
    rig.make(9)                       # edge not delivered
    rig.run_normal(540)
    rig.close()
    values = [v for v, _ in rig.times()]
    assert values == list(range(1, len(values) + 1))
    assert max(abs(e) for _, e in rig.times()[40:]) < 60


def test_fifo_overflow_is_a_gap_and_the_timing_recovers(rig):
    rig.run_normal(180)
    rig.make(45)                      # 13 lost: the FIFO holds 32
    rig.edge(rig.made)
    rig.reader.step()
    stats = rig.reader.stats
    assert stats["overruns"] == 1 and stats["gaps"] == 1
    rig.run_normal(rig.made + 400)
    rig.close()
    values = [v for v, _ in rig.times()]
    gap = [b - a for a, b in zip(values, values[1:]) if b - a != 1]
    assert gap == [14]                                        # sample numbers jump by the 13 lost
    late = [abs(e) for v, e in rig.times() if v > 270]
    assert max(late) < 80


def test_pps_edges_are_published_and_counted(rig):
    rig.pps_edge(11 * NS)
    rig.pps_edge(12 * NS + 1500)
    rig.reader.step()
    assert rig.pps == [(11 * NS, 1), (12 * NS + 1500, 2)] and rig.reader.stats["pps"] == 2


def test_lost_interrupt_events_are_counted(rig):
    rig.make(9)
    rig.edge(9)
    rig.seq += 3                      # the kernel dropped 3 events
    rig.make(9)
    rig.edge(18)
    rig.reader.step()
    assert rig.reader.stats["lost_edges"] == 3


def test_edges_before_the_settling_time_are_ignored(bus):
    rig = Rig(bus)
    rig.reader.ignore_before = arrival(5)
    rig.make(9)
    rig.edge(9)                       # earlier than ignore_before? arrival(9) > arrival(5): kept
    rig.edge(3)
    rig.reader.step()
    assert rig.reader.stats["edges"] == 1
    rig.close()


def test_silent_interrupt_is_rescued_by_the_timer(rig):
    rig.make(12)                      # the FIFO holds more than the watermark, but no edge came
    rig.reader.step()
    assert rig.reader.stats["rescued"] == 1 and rig.reader.clock.next_index == 12


def test_bus_error_is_a_gap_and_reading_goes_on(rig):
    rig.run_normal(90)
    rig.make(9)
    rig.edge(rig.made)
    rig.bus.fail = 1
    rig.reader.step()
    assert rig.reader.stats["errors"] == 1
    rig.bus.fifo.clear()              # whatever the failed read left is gone
    rig.made = rig.made
    rig.run_normal(rig.made + 200)
    rig.close()
    assert len(rig.out) > 150


# --- polling when there are no edges -----------------------------------------------------------

def test_without_edges_the_reader_polls_and_still_times_the_samples(bus):
    port = imu.Lis3dh("imu", 0x18, 400)
    out = []
    reader = Reader(port, None, lambda ts, line: out.append(line), lambda *_: None)
    reader.wake_ms = 1
    for _ in range(5):
        bus.produce([(i, 0, 0) for i in range(8)])
        reader.step()
    reader.finish()
    assert len(out) == 40
    monos = [int(line.decode().split(",")[0]) for line in out]
    assert monos == sorted(monos)
    port.close()


# --- logging with edges ------------------------------------------------------------------------

def test_log_files_hold_samples_pps_and_info(bus, tmp_path, monkeypatch):
    fake = FakeEdges()
    monkeypatch.setattr(imu, "open_edges", lambda: (fake, None))
    monkeypatch.setattr(config, "IMU_I2C_HZ", ODR)
    imu.open_imu_port()
    try:
        imu.start_logging(str(tmp_path))
        base = imu._reader.ignore_before
        import time
        for k in range(1, 12):
            bus.produce([(100 * k + i, 0, 0) for i in range(9)])
            fake.push(config.GPIO_LINE_IMU_INT, base + k * 90_000_000, k)
            end = time.time() + 2
            while imu._reader.clock.next_index < 9 * k and time.time() < end:
                time.sleep(0.005)         # the reader takes the batch out before the next one is made
        fake.push(config.GPIO_LINE_PPS, base + 500_000_000, 1)
        end = time.time() + 3
        while time.time() < end and (imu.get_stats()["samples"] < 90 or imu.get_stats()["pps"] < 1):
            time.sleep(0.02)
        stats = imu.get_stats()
        assert stats["timing"] == "edges" and stats["pps"] == 1 and stats["edges"] == 11
        imu.stop_logging()
    finally:
        imu.close_imu_port()
    rows = (tmp_path / "imu.csv").read_text().splitlines()
    assert rows[0] == "ts,mono_ns,aX,aY,aZ" and len(rows) > 90
    assert (tmp_path / "pps.csv").read_text().splitlines()[1].startswith(str(base + 500_000_000))
    info = json.loads((tmp_path / "imu_info.json").read_text())
    assert info["timing"] == "edges" and info["counters"]["gaps"] == 0 and info["counters"]["pps"] == 1
    assert fake.closed
