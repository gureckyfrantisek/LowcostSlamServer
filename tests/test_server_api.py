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
