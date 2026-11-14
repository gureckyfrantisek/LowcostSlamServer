# IMU and GNSS over a simulated I2C bus (no hardware needed)
import json
import time

import pytest

from app.core import config, gnss, i2c, imu
from tests.fakes import FakeBus, nmea, trickle, ubx


def wait_for(condition, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def closed_after():
    yield
    imu.close_imu_port()
    gnss.close_gnss_port()


# --- IMU ---------------------------------------------------------------------------------------

def test_imu_configures_fifo_range_and_watermark_interrupt(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100, range_g=2, fifo_threshold=8)
    assert bus.registers[0x20] == 0x57       # 100 Hz, normal mode, XYZ on
    assert bus.registers[0x23] == 0x88       # block data update, +-2 g, high resolution
    assert bus.registers[0x24] == 0x40       # FIFO on
    assert bus.registers[0x2E] == 0x88       # stream mode, watermark 8
    assert bus.registers[0x22] == 0x04       # watermark on INT1
    assert sensor.mg_per_count == 1 and sensor.edge_level == 9
    sensor.close()
    assert bus.registers[0x20] == 0x00       # powered down
    assert bus.registers[0x22] == 0x00       # interrupt off


def test_imu_range_sets_the_scale(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100, range_g=8)
    assert bus.registers[0x23] == 0xA8 and sensor.mg_per_count == 4
    sensor.close()


@pytest.mark.parametrize("kwargs", [dict(hz=123), dict(hz=100, range_g=3), dict(hz=100, fifo_threshold=0),
                                    dict(hz=100, fifo_threshold=31)])
def test_imu_bad_settings_are_rejected_before_touching_the_bus(bus, kwargs):
    with pytest.raises(ValueError):
        imu.Lis3dh("imu", 0x18, **kwargs)
    assert bus.transfers == 0


def test_imu_drain_reads_the_fifo_empty(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100)
    bus.produce([(i, -i, 1000 + i) for i in range(11)])
    drain = sensor.drain()
    assert drain.samples == [(i, -i, 1000 + i) for i in range(11)]
    assert drain.first_fill == 11 and not drain.overrun
    assert sensor.drain().samples == []      # empty now
    sensor.close()


def test_imu_drain_reports_a_full_fifo_as_overrun(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100)
    bus.produce([(i, 0, 0) for i in range(40)])   # 8 more than the FIFO holds
    drain = sensor.drain()
    assert drain.overrun and len(drain.samples) == 32
    assert drain.samples[0][0] == 8              # the oldest 8 were dropped
    sensor.close()


def test_imu_wrong_chip_is_reported_and_releases_the_bus(bus):
    bus.who_am_i = 0x44
    with pytest.raises(RuntimeError, match="wrong WHO_AM_I"):
        imu.Lis3dh("imu", 0x18, 100)


def test_imu_missing_sensor_is_reported_and_releases_the_bus(bus):
    bus.absent.add(0x18)
    with pytest.raises(RuntimeError, match="does not answer"):
        imu.Lis3dh("imu", 0x18, 100)


def test_imu_transfer_error_in_drain_is_raised(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100)
    bus.produce([(1, 2, 3)])
    bus.fail = 1
    with pytest.raises(OSError):
        sensor.drain()
    sensor.close()


def test_imu_logs_raw_values_with_time_and_info(bus, tmp_path):
    imu.open_imu_port()
    imu.start_logging(str(tmp_path))
    bus.produce([(i, -i, 1000 + i) for i in range(20)])
    time.sleep(0.15)
    imu.stop_logging()                       # the samples still waiting for an anchor are timed and written
    lines = (tmp_path / "imu.csv").read_text().splitlines()
    assert lines[0] == "ts,mono_ns,aX,aY,aZ"
    rows = [line.split(",") for line in lines[1:]]
    assert len(rows) == 20 and [r[2:] for r in rows][:3] == [["0", "0", "1000"], ["1", "-1", "1001"], ["2", "-2", "1002"]]
    monos = [int(r[1]) for r in rows]
    assert monos == sorted(monos) and len(set(monos)) == len(monos)
    assert all(int(r[0]) - int(r[1]) == int(rows[0][0]) - int(rows[0][1]) for r in rows)  # one wall clock offset
    info = json.loads((tmp_path / "imu_info.json").read_text())
    assert info["mg_per_count"] == 1 and info["timing"] == "polling" and info["counters"]["samples"] == 20
    assert (tmp_path / "pps.csv").read_text().startswith("mono_ns,seqno")


def test_imu_log_needs_an_open_port(tmp_path):
    with pytest.raises(RuntimeError, match="not open"):
        imu.start_logging(str(tmp_path))


def test_imu_subscriber_that_is_too_slow_loses_lines_not_the_reader(bus):
    imu.open_imu_port()
    q = imu.subscribe(maxsize=2)
    trickle(bus, 4, size=8)
    assert wait_for(lambda: imu.get_stats()["samples"] >= 24)
    assert q.qsize() == 2 and imu._dropped_lines > 0
    imu.unsubscribe(q)


def test_imu_keeps_reading_after_a_bus_error(bus):
    imu.open_imu_port()
    bus.fail = 3
    bus.produce([(1, 1, 1)] * 5)
    assert wait_for(lambda: imu.get_stats()["errors"] >= 1)
    trickle(bus, 4, size=8)
    assert wait_for(lambda: imu.get_stats()["samples"] >= 16)
    assert imu.verify_connection()


# --- GNSS --------------------------------------------------------------------------------------

def test_gnss_port_reads_only_what_the_receiver_announces(bus):
    bus.stream += b"hello"
    port = gnss.DdcPort("gnss", 0x42, 0.001)
    assert port.in_waiting == 5
    assert port.read(100) == b"hello"
    assert port.read(10) == b""              # nothing announced: no read, so no 0xFF filler
    assert port.in_waiting == 0
    port.close()


def test_gnss_port_splits_long_reads_to_keep_the_bus_free(bus):
    bus.stream += bytes(range(256)) * 2
    port = gnss.DdcPort("gnss", 0x42, 0.001)
    assert port.in_waiting == 512
    chunk = port.read(512)
    assert len(chunk) == 128                 # the bus is shared with the IMU
    port.close()


def test_gnss_port_missing_receiver_is_reported(bus):
    bus.absent.add(0x42)
    with pytest.raises(RuntimeError, match="does not answer"):
        gnss.DdcPort("gnss", 0x42, 0.001)


def test_gnss_port_nack_is_counted_and_retried(bus):
    bus.stream += b"abc"
    port = gnss.DdcPort("gnss", 0x42, 0.001)
    bus.fail = 1
    assert port.in_waiting == 0 and port.errors == 1
    assert port.in_waiting == 3
    assert port.read(3) == b"abc"
    port.close()


def test_gnss_frames_are_split_and_logged(bus, tmp_path):
    gnss.open_gnss_port()
    gnss.start_logging(str(tmp_path))
    q = gnss.subscribe(kinds=[gnss.NMEA])
    sentence = nmea("GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    frame = ubx(0x02, 0x15, bytes(range(40)))     # a UBX frame whose payload holds '\n' and '$' bytes
    bus.stream += frame[:10]                      # a frame arrives in two pieces
    time.sleep(0.05)
    bus.stream += frame[10:] + sentence
    ts, kind, data = q.get(timeout=3)
    assert kind == gnss.NMEA and data == sentence
    gnss.unsubscribe(q)
    time.sleep(0.1)
    gnss.stop_logging()
    assert (tmp_path / "gnss.ubx").read_bytes() == frame
    index = (tmp_path / "gnss_index.csv").read_text().splitlines()
    assert index[0].startswith("timestamp_ns") and index[1].endswith(",2,21")


def test_gnss_raw_output_is_switched_on_at_open(bus):
    gnss.open_gnss_port()
    assert bus.valset == {
        0x30210001: 1000, 0x30210002: 1,       # 1 Hz
        0x209102A4: 1, 0x20910231: 1,          # RXM-RAWX, RXM-SFRBX
        0x20910178: 1, 0x2091017D: 1,          # TIM-TM2 (IMU INT1 time marks), TIM-TP
        0x2005000C: 1,                         # PPS on the GPS time grid
        0x209100D3: 1,                         # NMEA GST: the accuracy of a position
    }
    assert gnss.get_stats()["raw_output"] is True


def test_gnss_rate_sets_measurement_period(bus, monkeypatch):
    monkeypatch.setattr(config, "GNSS_RATE_HZ", 10)
    gnss.open_gnss_port()
    assert bus.valset[0x30210001] == 100 and bus.valset[0x30210002] == 10


def test_gnss_rejected_settings_leave_nmea_working(bus):
    bus.ack = "nak"
    gnss.open_gnss_port()
    stats = gnss.get_stats()
    assert stats["raw_output"] is False and "rejected" in stats["raw_output_error"]
    q = gnss.subscribe(kinds=[gnss.NMEA])
    sentence = nmea("GNGLL,4807.038,N,01131.000,E,123519,A")
    bus.stream += sentence
    assert q.get(timeout=3)[2] == sentence
    gnss.unsubscribe(q)


def test_gnss_silent_receiver_times_out_instead_of_hanging(bus):
    bus.ack = "silent"
    port = gnss.DdcPort("gnss", 0x42, 0.001)
    start = time.time()
    with pytest.raises(RuntimeError, match="did not acknowledge"):
        gnss.configure_receiver(port, 1, timeout_s=0.3)
    assert time.time() - start < 1.5
    port.close()


def test_gnss_index_has_the_monotonic_arrival_time(bus, tmp_path):
    gnss.open_gnss_port()
    gnss.start_logging(str(tmp_path))
    before = time.monotonic_ns()
    bus.stream += ubx(0x02, 0x15, bytes(32))
    time.sleep(0.3)
    gnss.stop_logging()
    index = (tmp_path / "gnss_index.csv").read_text().splitlines()
    assert index[0] == "timestamp_ns,mono_ns,offset,length,ubx_class,ubx_id"
    row = index[-1].split(",")
    assert before <= int(row[1]) <= time.monotonic_ns() and row[4:] == ["2", "21"]


def test_gnss_junk_between_frames_is_skipped(bus):
    gnss.open_gnss_port()
    q = gnss.subscribe(kinds=[gnss.NMEA])
    sentence = nmea("GNGLL,4807.038,N,01131.000,E,123519,A")
    bus.stream += b"\x00\xff junk " + b"$GPXXX,bad*00\r\n" + sentence
    assert q.get(timeout=3)[2] == sentence
    gnss.unsubscribe(q)


# --- both sensors on one bus -------------------------------------------------------------------

def test_one_bus_for_both_sensors_is_shared_and_closed_after_the_last(bus, monkeypatch):
    monkeypatch.setattr(config, "IMU_I2C_BUS", "/dev/i2c-x")
    monkeypatch.setattr(config, "GNSS_I2C_BUS", "/dev/i2c-x")
    imu.open_imu_port()
    gnss.open_gnss_port()
    assert i2c.open_buses() == 1
    assert not bus.closed
    imu.close_imu_port()
    assert not bus.closed                    # the GNSS reader still needs it
    gnss.close_gnss_port()
    assert bus.closed


def test_separate_buses_are_opened_and_closed_on_their_own(monkeypatch):
    opened = {}

    def factory(path):
        opened[path] = FakeBus()
        return opened[path]
    monkeypatch.setattr(i2c, "SMBus", factory)
    monkeypatch.setattr(config, "IMU_I2C_BUS", "/dev/imu-bus")
    monkeypatch.setattr(config, "GNSS_I2C_BUS", "/dev/gnss-bus")
    imu.open_imu_port()
    gnss.open_gnss_port()
    assert i2c.open_buses() == 2 and set(opened) == {"/dev/imu-bus", "/dev/gnss-bus"}
    imu.close_imu_port()
    assert opened["/dev/imu-bus"].closed and not opened["/dev/gnss-bus"].closed
    gnss.close_gnss_port()
    assert opened["/dev/gnss-bus"].closed and i2c.open_buses() == 0


def test_missing_bus_device_is_reported_clearly(monkeypatch):
    def factory(path):
        raise FileNotFoundError(2, "No such file or directory", path)
    monkeypatch.setattr(i2c, "SMBus", factory)
    with pytest.raises(RuntimeError, match="cannot open the IMU I2C bus"):
        imu.Lis3dh("/dev/nope", 0x18, 100)
    with pytest.raises(RuntimeError, match="cannot open the GNSS I2C bus"):
        gnss.DdcPort("/dev/nope", 0x42, 0.01)
    assert i2c.open_buses() == 0


def test_sensors_read_at_the_same_time_without_mixing_transfers(bus):
    imu.open_imu_port()
    gnss.open_gnss_port()
    q = gnss.subscribe(kinds=[gnss.NMEA])
    sentences = [nmea(f"GNGLL,{i:04d}.000,N,01131.000,E,123519,A") for i in range(50)]
    for s in sentences:
        bus.stream += s
    trickle(bus, 5, size=8, gap=0.03)
    got = [q.get(timeout=3)[2] for _ in sentences]
    assert got == sentences
    assert wait_for(lambda: imu.get_stats()["samples"] >= 24)
    gnss.unsubscribe(q)
