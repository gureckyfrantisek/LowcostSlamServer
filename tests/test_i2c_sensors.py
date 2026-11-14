# IMU and GNSS over a simulated I2C bus (no hardware needed)
import time

import pytest

from app.core import config, gnss, i2c, imu
from tests.fakes import FakeBus, nmea, ubx


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

def test_imu_configures_the_sensor_and_reads_samples(bus):
    bus.samples = [(10, -20, 1000)]
    sensor = imu.Lis3dh("imu", 0x18, 100)
    assert bus.registers[0x20] == 0x57       # 100 Hz, normal mode, XYZ on
    assert bus.registers[0x23] == 0x88       # block data update, +-2 g, high resolution
    ts, line = sensor.read_line()
    assert line == b"10,-20,1000" and ts > 0
    assert sensor.read_line() is None        # nothing new yet
    sensor.close()
    assert bus.registers[0x20] == 0x00       # powered down


def test_imu_wrong_chip_is_reported_and_releases_the_bus(bus):
    bus.who_am_i = 0x44
    with pytest.raises(RuntimeError, match="wrong WHO_AM_I"):
        imu.Lis3dh("imu", 0x18, 100)


def test_imu_missing_sensor_is_reported_and_releases_the_bus(bus):
    bus.absent.add(0x18)
    with pytest.raises(RuntimeError, match="does not answer"):
        imu.Lis3dh("imu", 0x18, 100)


def test_imu_bad_rate_is_rejected_before_touching_the_bus(bus):
    with pytest.raises(ValueError):
        imu.Lis3dh("imu", 0x18, 123)
    assert bus.transfers == 0


def test_imu_transfer_errors_are_counted_and_retried(bus):
    sensor = imu.Lis3dh("imu", 0x18, 100)
    bus.samples = [(1, 2, 3)]
    bus.fail = 2
    assert sensor.read_line() is None and sensor.read_line() is None
    assert sensor.errors == 2
    assert sensor.read_line()[1] == b"1,2,3"
    sensor.close()


def test_imu_logs_raw_values_without_calibration(bus, tmp_path):
    imu.open_imu_port()
    start = imu.start_logging(str(tmp_path))
    assert start > 0
    bus.samples = [(i, -i, 1000 + i) for i in range(20)]
    assert wait_for(lambda: not bus.samples)
    time.sleep(0.1)
    imu.stop_logging()
    lines = (tmp_path / "imu.csv").read_text().splitlines()
    assert lines[0] == "ts,aX,aY,aZ"
    rows = [line.split(",") for line in lines[1:]]
    assert [r[1:] for r in rows] == [[str(i), str(-i), str(1000 + i)] for i in range(20)]
    stamps = [int(r[0]) for r in rows]
    assert stamps == sorted(stamps)


def test_imu_log_needs_an_open_port(tmp_path):
    with pytest.raises(RuntimeError, match="not open"):
        imu.start_logging(str(tmp_path))


def test_imu_subscriber_that_is_too_slow_loses_lines_not_the_reader(bus):
    imu.open_imu_port()
    q = imu.subscribe(maxsize=2)
    bus.samples = [(i, 0, 0) for i in range(30)]
    assert wait_for(lambda: not bus.samples)
    assert q.qsize() == 2 and imu._dropped_lines > 0
    imu.unsubscribe(q)


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
    bus.samples = [(i, i, i) for i in range(200)]
    for s in sentences:
        bus.stream += s
    got = [q.get(timeout=3)[2] for _ in sentences]
    assert got == sentences
    assert wait_for(lambda: not bus.samples)
    gnss.unsubscribe(q)
