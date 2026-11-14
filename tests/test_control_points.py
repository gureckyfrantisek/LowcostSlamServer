# Control points: start and finish timestamps, the average position in between, the file, the API
import math
import time

import pytest
from fastapi.testclient import TestClient

from app.core import camera, config, position
from app.core.control_points import ControlPointError, ControlPoints, average
from shared.models import Position
from shared.names import valid_point_id
from tests.fakes import nmea


def pos(lat, lon, alt=300.0, acc=None, fix="gnss"):
    return Position(latitude=lat, longitude=lon, timestamp_ns=1, altitude_m=alt, horizontal_accuracy_m=acc, fix_type=fix)


def read_rows(path):
    lines = (path / "control_points.csv").read_text().splitlines()
    return lines[0], [line.split(",") for line in lines[1:]]


# --- the average -------------------------------------------------------------------------------------

def test_average_of_positions():
    result = average([pos(50.0, 14.0, 300, 2.0), pos(50.0001, 14.0001, 302, 4.0), pos(50.0002, 14.0002, 301)])
    assert result["latitude"] == pytest.approx(50.0001) and result["longitude"] == pytest.approx(14.0001)
    assert result["altitude_m"] == pytest.approx(301.0) and result["positions"] == 3
    assert result["accuracy_m"] == pytest.approx(3.0)            # only the positions that have one
    assert result["fix_type"] == "gnss"
    assert 5 < result["std_m"] < 20                              # about 14 m between the ends of the line


def test_steady_positions_have_no_spread():
    result = average([pos(50.0, 14.0)] * 5)
    assert result["std_m"] == pytest.approx(0.0, abs=1e-6)


def test_average_without_positions_has_no_coordinates():
    result = average([])
    assert result["positions"] == 0 and result["latitude"] is None and result["std_m"] is None


def test_most_common_fix_type_wins_and_missing_altitude_is_skipped():
    result = average([pos(1, 2, None, fix="rtk_float"), pos(1, 2, 10, fix="gnss"), pos(1, 2, 20, fix="gnss")])
    assert result["fix_type"] == "gnss" and result["altitude_m"] == pytest.approx(15.0)


# --- the state machine ---------------------------------------------------------------------------------

@pytest.fixture
def points(tmp_path):
    p = ControlPoints()
    p.begin_recording(str(tmp_path))
    yield p
    p.end_recording()


def test_measuring_a_point_stores_times_and_the_average(points, tmp_path):
    before_wall, before_mono = time.time_ns(), time.monotonic_ns()
    started = points.start("4001")
    assert started["point_id"] == "4001" and started["start_ns"] >= before_wall
    for lat in (50.0, 50.00002):
        points.on_position(pos(lat, 14.0))
    time.sleep(0.01)
    done = points.finish()
    assert done["end_ns"] > done["start_ns"] and done["end_mono_ns"] > done["start_mono_ns"] >= before_mono
    assert done["latitude"] == pytest.approx(50.00001) and done["positions"] == 2
    header, rows = read_rows(tmp_path)
    assert header.startswith("point_id,start_ns,end_ns,start_mono_ns,end_mono_ns,latitude,longitude")
    row = dict(zip(header.split(","), rows[0]))
    assert row["point_id"] == "4001" and int(row["start_ns"]) == done["start_ns"] and int(row["end_mono_ns"]) == done["end_mono_ns"]
    assert float(row["latitude"]) == pytest.approx(50.00001) and row["positions"] == "2" and row["fix_type"] == "gnss"


def test_positions_outside_the_measurement_are_not_counted(points):
    points.on_position(pos(10.0, 10.0))               # before
    points.start("A1")
    points.on_position(pos(50.0, 14.0))
    done = points.finish()
    points.on_position(pos(10.0, 10.0))               # after
    assert done["positions"] == 1 and done["latitude"] == 50.0


def test_a_point_without_positions_is_stored_without_coordinates(points, tmp_path):
    points.start("A1")
    done = points.finish()
    assert done["positions"] == 0 and done["latitude"] is None
    _, rows = read_rows(tmp_path)
    assert rows[0][5:8] == ["", "", ""] and rows[0][8] == "0"       # the times are there, the coordinates are not


def test_errors_say_what_is_wrong(points):
    with pytest.raises(ControlPointError) as e:
        points.finish()
    assert e.value.code == "not_started"
    with pytest.raises(ControlPointError) as e:
        points.cancel()
    assert e.value.code == "not_started"
    for bad in ("", " ", "a b", "a,b", "../x", "x" * 33, "-a", "é", None, 4001):
        with pytest.raises(ControlPointError) as e:
            points.start(bad)
        assert e.value.code == "invalid_id", bad
    points.start("A1")
    with pytest.raises(ControlPointError) as e:
        points.start("A2")
    assert e.value.code == "already_open" and "A1" in e.value.message
    points.finish()
    with pytest.raises(ControlPointError) as e:
        points.start("A1")
    assert e.value.code == "duplicate_id"


def test_cancelled_point_can_be_measured_again_and_leaves_no_row(points, tmp_path):
    points.start("A1")
    points.on_position(pos(50.0, 14.0))
    points.cancel()
    points.start("A1")                                  # not a duplicate: nothing was stored
    points.finish()
    assert len(read_rows(tmp_path)[1]) == 1


def test_not_recording_is_refused():
    p = ControlPoints()
    with pytest.raises(ControlPointError) as e:
        p.start("A1")
    assert e.value.code == "not_recording"


def test_end_of_the_recording_finishes_an_open_point(tmp_path):
    p = ControlPoints()
    p.begin_recording(str(tmp_path))
    p.start("A1")
    p.on_position(pos(50.0, 14.0))
    problems = p.end_recording()
    assert any("A1" in x and "finished by the stop" in x for x in problems)
    assert len(read_rows(tmp_path)[1]) == 1 and p.state()["open"] is None
    with pytest.raises(ControlPointError):
        p.start("A2")                                     # the recording is over


def test_state_shows_the_open_point(points):
    assert points.state() == {"count": 0, "open": None}
    points.start("A1")
    points.on_position(pos(50.0, 14.0))
    state = points.state()
    assert state["open"]["point_id"] == "A1" and state["open"]["positions"] == 1 and state["open"]["elapsed_s"] >= 0
    points.finish()
    assert points.state() == {"count": 1, "open": None}


def test_a_new_recording_starts_with_no_points(tmp_path):
    p = ControlPoints()
    p.begin_recording(str(tmp_path))
    p.start("A1")
    p.finish()
    other = tmp_path / "second"
    other.mkdir()
    p.begin_recording(str(other))
    assert p.state() == {"count": 0, "open": None}
    p.start("A1")                                          # the id is free again in the new measurement
    p.end_recording()


def test_a_failing_disk_is_reported_not_raised(tmp_path, monkeypatch):
    import os
    p = ControlPoints()
    p.begin_recording(str(tmp_path))
    p.start("A1")

    def broken(fd):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(os, "fsync", broken)
    done = p.finish()                                      # the answer still comes: the app must not lose the point
    assert done["point_id"] == "A1" and p.write_error
    assert any("could not be written" in x for x in p.end_recording())


def test_unwritable_folder_is_reported(tmp_path):
    p = ControlPoints()
    p.begin_recording(str(tmp_path / "missing"))
    assert p.write_error
    p.start("A1")
    assert p.finish()["point_id"] == "A1"
    assert any("could not be written" in x for x in p.end_recording())


@pytest.mark.parametrize("text, ok", [("4001", True), ("A-1", True), ("pt_2.1", True), ("0", True), ("x" * 32, True),
                                      ("", False), ("x" * 33, False), ("a b", False), ("a,b", False), (".a", False),
                                      ("a\n", False), ("é", False)])
def test_point_ids(text, ok):
    assert valid_point_id(text) is ok


# --- the API ---------------------------------------------------------------------------------------------

RMC = nmea("GNRMC,115840.00,A,5005.89580,N,01422.01245,E,0.032,,141126,,,A,V")


def gga(second, lat="5005.89580", alt="304.6"):
    return nmea(f"GNGGA,1158{second:02d}.00,{lat},N,01422.01245,E,1,12,0.56,{alt},M,44.4,M,,")


@pytest.fixture
def client(bus, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(camera, "open_camera", lambda: None)
    monkeypatch.setattr(camera, "close_camera", lambda: True)
    monkeypatch.setattr(camera, "verify_connection", lambda: True)
    monkeypatch.setattr(camera, "start_recording", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(camera, "get_media_time", lambda: 1234)
    monkeypatch.setattr(camera, "stop_recording", lambda: True)
    monkeypatch.setattr(camera, "get_camera_files_list", lambda: [])
    from app.main import app
    with TestClient(app) as c:
        c.tmp = tmp_path / "data"
        yield c


def wait_positions(n, timeout=4):
    end = time.time() + timeout
    while time.time() < end and position.feed.positions < n:
        time.sleep(0.02)
    assert position.feed.positions >= n


def test_measuring_a_control_point_through_the_api(client, bus):
    position.feed.positions = 0
    assert client.post("/start", params={"project_name": "cp"}).status_code == 200
    started = client.post("/control-point/start", params={"point_id": "4001"})
    assert started.status_code == 200 and started.json()["point"]["point_id"] == "4001"
    status = client.get("/status").json()["recording"]
    assert status["control_points"]["open"]["point_id"] == "4001" and status["control_points"]["count"] == 0
    bus.stream += RMC + gga(40, "5005.89580") + gga(41, "5005.89700")
    wait_positions(1)
    time.sleep(0.4)
    done = client.post("/control-point/finish")
    assert done.status_code == 200 and done.json()["warnings"] == []
    point = done.json()["point"]
    assert point["positions"] >= 1 and point["latitude"] == pytest.approx(50 + 5.8958 / 60, abs=0.001)
    assert point["end_ns"] > point["start_ns"]
    assert client.get("/status").json()["recording"]["control_points"] == {"count": 1, "open": None}
    assert [p["point_id"] for p in client.get("/control-point").json()["points"]] == ["4001"]
    stopped = client.post("/stop", params={"project_name": "cp"}).json()
    assert stopped["warnings"] == []
    import json
    meta = json.loads((client.tmp / "cp" / "meta.json").read_text())
    assert meta["control_points"] == 1
    assert len(read_rows(client.tmp / "cp")[1]) == 1


def test_control_point_errors_over_http(client):
    assert client.post("/control-point/start", params={"point_id": "1"}).json()["code"] == "not_recording"
    assert client.post("/start", params={"project_name": "cp2"}).status_code == 200
    assert client.post("/control-point/finish").status_code == 409
    bad = client.post("/control-point/start", params={"point_id": "a b"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_id"
    assert client.post("/control-point/start", params={"point_id": "1"}).status_code == 200
    assert client.post("/control-point/start", params={"point_id": "2"}).json()["code"] == "already_open"
    finished = client.post("/control-point/finish")
    assert finished.json()["warnings"] == ["no GNSS position arrived while the point was measured"]
    assert client.post("/control-point/start", params={"point_id": "1"}).json()["code"] == "duplicate_id"
    assert client.post("/control-point/cancel").json()["code"] == "not_started"
    client.post("/stop", params={"project_name": "cp2"})


def test_stopping_with_a_point_open_finishes_it_and_warns(client):
    client.post("/start", params={"project_name": "cp3"})
    client.post("/control-point/start", params={"point_id": "9"})
    stopped = client.post("/stop", params={"project_name": "cp3"})
    assert stopped.status_code == 200
    assert any("9" in w and "finished by the stop" in w for w in stopped.json()["warnings"])
    assert len(read_rows(client.tmp / "cp3")[1]) == 1


def test_status_has_the_start_time_and_the_elapsed_time(client):
    idle = client.get("/status").json()["recording"]
    assert idle["active"] is False and "started_ns" not in idle and "elapsed_s" not in idle
    before = time.time_ns()
    client.post("/start", params={"project_name": "cp4"})
    time.sleep(0.6)
    recording = client.get("/status").json()["recording"]
    assert recording["active"] and recording["project"] == "cp4"
    assert before <= recording["started_ns"] <= time.time_ns()
    assert 0.5 <= recording["elapsed_s"] < 3
    client.post("/stop", params={"project_name": "cp4"})
    assert "elapsed_s" not in client.get("/status").json()["recording"]


# --- over BLE ----------------------------------------------------------------------------------------------

def test_ble_commands_for_control_points():
    from gatt.protocol.commands import COMMANDS, build_request
    from gatt.protocol.messages import ProtocolError
    assert build_request(COMMANDS["control_point.start"], {"point_id": "4001"}) == ("/control-point/start", {"point_id": "4001"})
    assert build_request(COMMANDS["control_point.finish"], {}) == ("/control-point/finish", {})
    assert COMMANDS["control_point.finish"].method == "POST" and COMMANDS["control_point.list"].method == "GET"
    for bad in ({}, {"point_id": "a b"}, {"point_id": 4001}, {"point_id": ""}, {"point_id": "x" * 33}):
        with pytest.raises(ProtocolError):
            build_request(COMMANDS["control_point.start"], bad)


def test_the_recording_state_fits_the_compact_status():
    import json
    from gatt.status_poller import compact_status
    out = compact_status({"status": "ready", "sensors": {}, "recording": {
        "active": True, "project": "p", "problems": [], "started_ns": 1, "elapsed_s": 12.3,
        "control_points": {"count": 2, "open": {"point_id": "4003", "elapsed_s": 4.1, "positions": 4}}}})
    assert out["recording"]["control_points"]["open"]["point_id"] == "4003"
    assert len(json.dumps(out)) < 400
