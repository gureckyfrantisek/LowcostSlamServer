# The HTTP API with simulated sensors and a stand-in camera
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core import camera, config, gnss, imu
from tests.fakes import nmea, trickle


@pytest.fixture
def client(bus, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path))
    state = {"files": []}
    monkeypatch.setattr(camera, "open_camera", lambda: None)
    monkeypatch.setattr(camera, "close_camera", lambda: True)
    monkeypatch.setattr(camera, "verify_connection", lambda: True)
    monkeypatch.setattr(camera, "start_recording", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(camera, "get_media_time", lambda: 1234)
    monkeypatch.setattr(camera, "stop_recording", lambda: True)
    monkeypatch.setattr(camera, "get_camera_files_list", lambda: state["files"])
    from app.main import app
    with TestClient(app) as c:
        c.state = state
        yield c


def test_status_is_ready_with_all_sensors(client):
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["sensors"] == {"camera": "ok", "gnss": "ok", "imu": "ok"}


def test_measurement_needs_no_calibration_and_stores_raw_data(client, bus, tmp_path):
    assert client.post("/start", params={"project_name": "p1"}).status_code == 200
    bus.produce([(5, 6, 1000)] * 5)
    time.sleep(0.15)
    bus.stream += nmea("GNGLL,4807.038,N,01131.000,E,123519,A")
    time.sleep(0.3)
    assert client.post("/stop", params={"project_name": "p1"}).status_code == 200
    project = tmp_path / "p1"
    rows = (project / "imu.csv").read_text().splitlines()
    assert rows[0] == "ts,mono_ns,aX,aY,aZ" and rows[1].endswith(",5,6,1000")
    assert "camera_files" in json.loads((project / "meta.json").read_text())


def test_second_start_is_refused(client):
    assert client.post("/start", params={"project_name": "p2"}).status_code == 200
    assert client.post("/start", params={"project_name": "p2"}).json()["status"] == "already measuring"
    client.post("/stop", params={"project_name": "p2"})


def test_failed_camera_start_leaves_no_files(client, monkeypatch, tmp_path):
    monkeypatch.setattr(camera, "start_recording", lambda: False)
    r = client.post("/start", params={"project_name": "p3"})
    assert r.status_code == 500 and r.json()["status"] == "failed to start camera"
    assert not (tmp_path / "p3").exists()
    assert not imu.is_logging() and gnss._log_thread is None


def test_imu_websocket_streams_samples(client, bus):
    with client.websocket_connect("/ws/imu") as ws:
        trickle(bus, 3, size=8)
        message = ws.receive_text()
    ts, mono, x, y, z = message.split(",")
    assert (x, y, z) == ("0", "0", "0") and int(ts) > 0 and int(mono) > 0


# --- storage: the stick, the warnings, the project names ---------------------------------------

@pytest.fixture
def stick(client, tmp_path, monkeypatch):
    """A mounted USB stick with plenty of room; returns its recording folder"""
    from app.core import storage
    mount = tmp_path.parent / (tmp_path.name + "_stick")    # outside the local data folder
    mount.mkdir()
    monkeypatch.setattr(storage, "usb_mounts", lambda *a: [str(mount)])
    monkeypatch.setattr(storage, "free_bytes", lambda path: 100 * storage.bytes_per_hour())
    return mount / "LowcostSlam"


def test_recording_goes_to_the_stick_when_there_is_one(client, stick, bus, tmp_path):
    assert client.post("/start", params={"project_name": "onstick"}).status_code == 200
    bus.produce([(1, 2, 3)] * 8)
    time.sleep(0.2)
    assert client.post("/stop", params={"project_name": "onstick"}).status_code == 200
    assert (stick / "onstick" / "imu.csv").exists() and (stick / "onstick" / "meta.json").exists()
    assert not (tmp_path / "onstick").exists()
    assert client.get("/projects").json()["projects"] == ["onstick"]


def test_projects_of_the_stick_and_the_local_disk_are_listed_together(client, stick, tmp_path):
    (stick / "a").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    assert sorted(client.get("/projects").json()["projects"]) == ["a", "b"]
    assert client.get("/projects/a").status_code == 200 and client.get("/projects/b").status_code == 200


def test_downloading_a_project_that_is_on_the_stick_copies_nothing_and_cleanup_keeps_it(client, stick, monkeypatch):
    from app.core import camera
    monkeypatch.setattr(camera, "download_files", lambda path, files: True)
    monkeypatch.setattr(camera, "delete_file", lambda f: True)
    (stick / "keep").mkdir(parents=True)
    (stick / "keep" / "imu.csv").write_text("data")
    (stick / "keep" / "meta.json").write_text('{"camera_files": []}')
    r = client.post("/download", params={"project_name": "keep", "cleanup": True})
    assert r.status_code == 200 and r.json()["status"] == "downloaded"
    assert (stick / "keep" / "imu.csv").read_text() == "data"      # the only copy is still there


def test_downloading_a_local_project_copies_it_to_the_stick_and_cleanup_removes_the_local_one(client, stick, tmp_path, monkeypatch):
    from app.core import camera
    monkeypatch.setattr(camera, "download_files", lambda path, files: True)
    monkeypatch.setattr(camera, "delete_file", lambda f: True)
    # recorded before the stick was plugged in: the project is local only
    from app.core import storage
    real = storage.usb_mounts
    monkeypatch.setattr(storage, "usb_mounts", lambda *a: [])
    assert client.post("/start", params={"project_name": "old"}).status_code == 200
    client.post("/stop", params={"project_name": "old"})
    monkeypatch.setattr(storage, "usb_mounts", lambda *a: [str(stick.parent)])
    assert (tmp_path / "old" / "imu.csv").exists()
    r = client.post("/download", params={"project_name": "old", "cleanup": True})
    assert r.status_code == 200
    assert (stick / "old" / "imu.csv").exists() and not (tmp_path / "old").exists()


def test_download_without_a_stick_says_so(client, tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "meta.json").write_text("{}")
    r = client.post("/download", params={"project_name": "x"})
    assert r.status_code == 500 and r.json()["status"] == "USB unavailable"


def test_status_and_start_warn_when_less_than_three_hours_fit(client, monkeypatch):
    from app.core import storage
    monkeypatch.setattr(storage, "free_bytes", lambda path: round(2 * storage.bytes_per_hour()))
    status = client.get("/status").json()
    assert status["storage"]["level"] == "low" and "2.0 h of recording" in status["warnings"][0]
    started = client.post("/start", params={"project_name": "low"})
    assert started.status_code == 200 and started.json()["warnings"]    # a warning, not a refusal
    client.post("/stop", params={"project_name": "low"})


def test_start_is_refused_when_not_even_ten_minutes_fit(client, monkeypatch, tmp_path):
    from app.core import storage
    monkeypatch.setattr(storage, "free_bytes", lambda path: round(0.05 * storage.bytes_per_hour()))
    r = client.post("/start", params={"project_name": "nospace"})
    assert r.status_code == 507 and r.json()["status"] == "storage full"
    assert not (tmp_path / "nospace").exists() and not imu.is_logging()


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", ".hidden", ""])
def test_start_refuses_names_that_are_not_one_folder(client, name, tmp_path):
    r = client.post("/start", params={"project_name": name})
    assert r.status_code in (400, 422)
    assert list(tmp_path.iterdir()) == []


def test_status_without_a_warning_has_an_empty_warning_list(client):
    status = client.get("/status").json()
    assert status["warnings"] == [] and status["storage"]["level"] == "ok"
