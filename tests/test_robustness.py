# What happens when the world misbehaves: nothing connected at the start, devices pulled out and plugged
# in again, a disk that fails, a bug in a reader, requests at the same time. All on simulated hardware.
import concurrent.futures
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core import camera, common, events, gnss, imu, supervisors
from tests.fakes import nmea, ubx


class FakeCamera:
    """The camera: plug it in and out with .present"""

    def __init__(self):
        self.present = True
        self.opened = False
        self.recording = False
        self.files = []
        self.open_calls = 0

    def open_camera(self):
        self.open_calls += 1
        if not self.present:
            raise RuntimeError("no camera found")
        self.opened = True

    def close_camera(self):
        self.opened = False
        return True

    def verify_connection(self):
        return self.present and self.opened

    def start_recording(self):
        if not self.verify_connection():
            return False
        self.recording = True
        self.files.append(f"clip_{len(self.files)}.insv")
        return 1_700_000_000_000_000_000

    def stop_recording(self):
        if not self.verify_connection():
            return False
        self.recording = False
        return True

    def get_camera_files_list(self):
        return list(self.files) if self.verify_connection() else 1


class Feeder(threading.Thread):
    """The IMU keeps making samples at about 400 Hz while it is plugged in"""

    def __init__(self, bus):
        super().__init__(daemon=True)
        self.bus, self.stop_event, self.paused = bus, threading.Event(), False
        self.n = 0

    def run(self):
        while not self.stop_event.is_set():
            if not self.paused and 0x18 not in self.bus.absent:
                self.bus.produce([(self.n + i, 0, 1000) for i in range(8)])
                self.n += 8
            time.sleep(0.02)


class Server:
    def __init__(self, client, bus, cam):
        self.client, self.bus, self.cam = client, bus, cam

    def status(self):
        return self.client.get("/status").json()

    def wait(self, condition, timeout=6.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.02)
        pytest.fail(f"timed out waiting for {what}; status: {json.dumps(self.status())[:600]}")

    def wait_ready(self, what="all sensors ok", **kw):
        self.wait(lambda: self.client.get("/status").status_code == 200, what=what, **kw)

    def start(self, name="m1"):
        return self.client.post("/start", params={"project_name": name})

    def stop(self, name="m1"):
        return self.client.post("/stop", params={"project_name": name})


@pytest.fixture
def make_server(bus, tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data")
    monkeypatch.setattr(supervisors, "SCALE", 0.05)
    cam = FakeCamera()
    for name in ("open_camera", "close_camera", "verify_connection", "start_recording", "stop_recording",
                 "get_camera_files_list"):
        monkeypatch.setattr(camera, name, getattr(cam, name))
    monkeypatch.setattr(camera, "get_media_time", lambda: 1234)
    from app.main import app
    stack = []

    def make(absent=(), camera_present=True):
        bus.absent |= set(absent)
        cam.present = camera_present
        client = TestClient(app)
        client.__enter__()
        stack.append(client)
        return Server(client, bus, cam)

    yield make
    for client in stack:
        client.__exit__(None, None, None)


# --- nothing is connected at the start ---------------------------------------------------------

def test_server_starts_with_no_device_connected_and_says_what_is_missing(make_server):
    server = make_server(absent={0x18, 0x42}, camera_present=False)
    status = server.client.get("/status")
    assert status.status_code == 500 and status.json()["status"] == "not ready"
    details = status.json()["details"]
    assert all(d["state"] == "missing" for d in details.values())
    assert "no camera found" in details["camera"]["detail"]
    assert "does not answer" in details["imu"]["detail"] and "does not answer" in details["gnss"]["detail"]


def test_start_is_refused_with_the_names_of_the_missing_devices(make_server, tmp_path):
    server = make_server(absent={0x42}, camera_present=False)
    r = server.start()
    assert r.status_code == 503 and set(r.json()["missing"]) == {"camera", "gnss"}
    assert "camera" in r.json()["status"] and "gnss" in r.json()["status"]
    assert os.listdir(tmp_path / "data") == [] and not imu.is_logging()


def test_devices_plugged_in_later_are_picked_up_without_a_restart(make_server):
    server = make_server(absent={0x18, 0x42}, camera_present=False)
    assert server.client.get("/status").status_code == 500
    server.bus.absent.clear()
    server.cam.present = True
    server.wait_ready(what="the devices to be picked up")
    assert server.start().status_code == 200
    server.stop()


def test_only_the_missing_device_is_reported_missing(make_server):
    server = make_server(absent={0x42})
    server.wait(lambda: server.status()["details"]["imu"]["state"] == "ok", what="the IMU")
    details = server.status()["details"]
    assert details["imu"]["state"] == "ok" and details["camera"]["state"] == "ok"
    assert details["gnss"]["state"] == "missing"


# --- devices pulled out and plugged in again ---------------------------------------------------

def test_camera_pulled_out_and_plugged_in_again(make_server):
    server = make_server()
    server.wait_ready()
    server.cam.present = False
    server.wait(lambda: server.status()["details"]["camera"]["state"] == "missing", what="the camera to be lost")
    server.wait(lambda: any(e["source"] == "camera" and e["kind"] == "lost" for e in server.status()["events"]),
                what="the camera to be reported lost")
    server.cam.present = True
    server.wait_ready(what="the camera to come back")
    server.wait(lambda: any(e["source"] == "camera" and e["kind"] == "back" for e in server.status()["events"]),
                what="the camera to be reported back")


def test_imu_pulled_out_during_a_recording_is_a_gap_and_the_recording_goes_on(make_server, tmp_path):
    server = make_server()
    server.wait_ready()
    feeder = Feeder(server.bus)
    feeder.start()
    try:
        assert server.start().status_code == 200
        server.wait(lambda: imu.get_stats().get("samples", 0) > 100, what="samples")
        server.bus.absent.add(0x18)                 # pulled out
        server.bus.fifo.clear()
        server.wait(lambda: server.status()["details"]["imu"]["state"] == "missing", what="the IMU to be lost")
        status = server.status()
        assert status["recording"]["active"] and any("imu" in p for p in status["recording"]["problems"])
        before = imu.get_stats()["samples"]
        server.bus.absent.discard(0x18)             # plugged in again: the sensor lost its settings
        server.bus.registers.clear()
        server.wait(lambda: server.status()["details"]["imu"]["state"] == "ok", what="the IMU to come back")
        assert server.bus.registers[0x2E] == 0x88 and server.bus.registers[0x22] == 0x04   # configured again
        server.wait(lambda: imu.get_stats()["samples"] > before + 100, what="samples after the gap")
        stopped = server.stop().json()
    finally:
        feeder.stop_event.set()
    assert any("imu was lost" in w for w in stopped["warnings"])
    project = tmp_path / "data" / "m1"
    rows = (project / "imu.csv").read_text().splitlines()[1:]
    monos = [int(r.split(",")[1]) for r in rows]
    assert monos == sorted(monos)
    jumps = [b - a for a, b in zip(monos, monos[1:])]
    gap_at = jumps.index(max(jumps))
    assert max(jumps) > 0.2e9                       # the gap is visible as a jump of the time
    assert gap_at > 50 and len(rows) - gap_at > 50  # data before it and after it
    events_csv = (project / "events.csv").read_text()
    assert "imu,lost" in events_csv and "imu,back" in events_csv
    meta = json.loads((project / "meta.json").read_text())
    assert any(e["kind"] == "lost" for e in meta["events"]) and any("gap" in p for p in meta["problems"])


def test_imu_that_lost_its_power_is_noticed_by_its_silence(make_server):
    server = make_server()
    server.wait_ready()
    feeder = Feeder(server.bus)
    feeder.start()
    try:
        server.wait(lambda: imu.get_stats().get("samples", 0) > 50, what="samples")
        feeder.paused = True                        # no error on the bus, the sensor just says nothing
        server.bus.registers.clear()
        server.wait(lambda: server.bus.registers.get(0x2E) == 0x88, timeout=8, what="the sensor to be set up again")
        feeder.paused = False
        before = imu.get_stats()["samples"]
        server.wait(lambda: imu.get_stats()["samples"] > before + 50, what="samples again")
        assert any(e["source"] == "imu" and e["kind"] == "lost" for e in events.recent(20))
    finally:
        feeder.stop_event.set()


def test_gnss_receiver_pulled_out_and_back_is_configured_again(make_server):
    server = make_server()
    server.wait_ready()
    server.bus.absent.add(0x42)
    server.wait(lambda: any(e["source"] == "gnss" and e["kind"] == "lost" for e in events.recent(20)),
                what="the receiver to be reported lost")
    server.bus.valset = None                        # it forgot its settings
    server.bus.absent.discard(0x42)
    server.wait(lambda: server.status()["details"]["gnss"]["state"] == "ok", what="the receiver to come back")
    assert server.bus.valset is not None            # the raw output was switched on again
    q = gnss.subscribe(kinds=[gnss.NMEA])
    sentence = nmea("GNGLL,4807.038,N,01131.000,E,123519,A")
    server.bus.stream += sentence
    assert q.get(timeout=3)[2] == sentence
    gnss.unsubscribe(q)


def test_silent_gnss_receiver_is_reopened(make_server, monkeypatch):
    monkeypatch.setattr(gnss, "SILENT_S", 0.4)
    server = make_server()
    server.bus.valset = None
    server.wait(lambda: server.bus.valset is not None, what="a configuration after the silence")
    assert any(e["source"] == "gnss" and e["kind"] == "lost" for e in events.recent(20))


def test_receiver_that_forgot_its_settings_is_configured_again(make_server, monkeypatch):
    """A power dip: the bus answers, NMEA goes on, but the raw output is off. RAWX stops, so it is reopened"""
    monkeypatch.setattr(gnss, "RAWX_SILENT_S", 0.5)
    server = make_server()
    server.wait_ready()
    server.bus.valset = None
    stop = threading.Event()

    def nmea_only():                                 # the receiver still talks NMEA
        while not stop.is_set():
            server.bus.stream += nmea("GNGLL,4807.038,N,01131.000,E,123519,A")
            time.sleep(0.05)
    t = threading.Thread(target=nmea_only, daemon=True)
    t.start()
    try:
        server.wait(lambda: server.bus.valset is not None, what="the raw output to be switched on again")
    finally:
        stop.set()


# --- the reader itself breaks --------------------------------------------------------------------

def test_a_reader_that_crashes_is_replaced(make_server, monkeypatch):
    server = make_server()
    server.wait_ready()
    feeder = Feeder(server.bus)
    feeder.start()
    try:
        server.wait(lambda: imu.get_stats().get("samples", 0) > 20, what="samples")
        real_step, calls = imu.Reader.step, []

        def broken(self):
            if not calls:
                calls.append(1)
                raise RuntimeError("bug in the reader")
            return real_step(self)
        monkeypatch.setattr(imu.Reader, "step", broken)
        server.wait(lambda: calls and imu.get_stats()["reader_error"] is None
                    and server.status()["details"]["imu"]["state"] == "ok"
                    and imu._reader_thread is not None and imu._reader_thread.is_alive(), what="the reader to be replaced")
        before = imu.get_stats()["samples"]
        server.wait(lambda: imu.get_stats()["samples"] > before + 30, what="samples from the new reader")
    finally:
        feeder.stop_event.set()
    assert any(e["source"] == "imu" and e["kind"] == "lost" for e in events.recent(20))


# --- the storage fails -----------------------------------------------------------------------------

def test_storage_that_fails_during_a_recording_is_reported_and_the_stop_still_works(make_server, monkeypatch, tmp_path):
    monkeypatch.setattr(imu, "FSYNC_S", 0.05)
    monkeypatch.setattr(gnss, "FSYNC_S", 0.05)
    server = make_server()
    server.wait_ready()
    feeder = Feeder(server.bus)
    feeder.start()
    try:
        assert server.start().status_code == 200
        server.wait(lambda: imu.get_stats().get("samples", 0) > 50, what="samples")

        real_fsync = os.fsync

        def pulled(fd):
            raise OSError(5, "Input/output error")        # the stick was pulled out
        monkeypatch.setattr(os, "fsync", pulled)
        server.wait(lambda: imu.get_stats()["log_error"] is not None, what="the write error")
        status = server.status()
        assert any("imu data is no longer written" in p for p in status["recording"]["problems"])
        assert any(e["kind"] == "storage_error" for e in status["events"])
        monkeypatch.setattr(os, "fsync", real_fsync)       # the stick works again for the cleanup
        stopped = server.stop()
    finally:
        feeder.stop_event.set()
    assert stopped.status_code == 200 and any("no longer written" in w for w in stopped.json()["warnings"])
    assert not imu.is_logging() and not common.get_recording_state()["active"]
    # and the next measurement works
    server.wait_ready()
    assert server.start("next").status_code == 200       # the next measurement works
    assert imu.get_stats()["log_error"] is None
    server.stop("next")


def test_stop_when_the_project_folder_is_gone_does_not_crash(make_server, tmp_path):
    import shutil
    server = make_server()
    server.wait_ready()
    assert server.start().status_code == 200
    shutil.rmtree(tmp_path / "data" / "m1")                 # the stick was pulled out
    stopped = server.stop()
    assert stopped.status_code == 200 and stopped.json()["warnings"]
    assert not common.get_recording_state()["active"]
    assert not (tmp_path / "data" / "m1").exists()          # no stray folder is created


def test_stop_with_the_camera_lost_still_stops_everything(make_server):
    server = make_server()
    server.wait_ready()
    assert server.start().status_code == 200
    server.cam.present = False
    stopped = server.stop()
    assert stopped.status_code == 200
    assert any("camera" in w for w in stopped.json()["warnings"])
    assert not imu.is_logging() and gnss._log_thread is None


def test_a_start_that_fails_in_the_middle_is_rolled_back_with_a_reason(make_server, monkeypatch, tmp_path):
    server = make_server()
    server.wait_ready()

    real_start = gnss.start_logging

    def boom(path):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(gnss, "start_logging", boom)
    r = server.start()
    assert r.status_code == 500 and "No space left" in r.json()["reason"]
    assert os.listdir(tmp_path / "data") == [] and not imu.is_logging()
    monkeypatch.setattr(gnss, "start_logging", real_start)
    assert server.start("again").status_code == 200        # nothing was left half started
    server.stop("again")


# --- requests that do not wait for each other -----------------------------------------------------

def test_two_starts_at_the_same_time_start_one_measurement(make_server):
    server = make_server()
    server.wait_ready()
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda i: server.start("race"), range(6)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1 and codes.count(500) == 5
    assert all(r.json()["status"] == "already measuring" for r in results if r.status_code == 500)
    assert server.stop("race").status_code == 200


def test_stop_without_a_measurement_and_a_second_stop_are_harmless(make_server):
    server = make_server()
    server.wait_ready()
    assert server.stop().status_code == 500 and server.stop().json()["status"] == "couldn't stop"
    assert server.start().status_code == 200
    assert server.stop().status_code == 200 and server.stop().status_code == 500


def test_shutting_the_server_down_during_a_measurement_closes_it_properly(make_server, tmp_path):
    server = make_server()
    server.wait_ready()
    assert server.start("shutdown").status_code == 200
    server.bus.produce([(1, 2, 3)] * 8)
    time.sleep(0.2)
    server.client.__exit__(None, None, None)                 # the service is stopped
    project = tmp_path / "data" / "shutdown"
    meta = json.loads((project / "meta.json").read_text())
    assert "camera_files" in meta and (project / "imu.csv").exists() and (project / "imu_info.json").exists()
    assert not common.get_recording_state()["active"]
    server.client.__enter__()                                # so the fixture can close it again


def test_status_never_blocks_while_a_device_is_being_opened(make_server, monkeypatch):
    server = make_server(camera_present=False)
    start = time.time()
    for _ in range(20):
        server.client.get("/status")
    assert time.time() - start < 3
