# The camera: video mode and quality before recording, the cached state in /status, what the camera reports
# by itself, the clock. A fake SDK camera stands in for the real one (it behaves like it, down to the line
# the SDK writes to stderr when a recording starts).
import os
import time
import types

import pytest
from fastapi.testclient import TestClient

from app.core import camera, common, config, events, position
from sdk import camerasdk
from shared.models import Position


class FakeSdkCamera:
    def __init__(self):
        self.calls = []
        self.mode_ok = True
        self.params_ok = True
        self.start_ok = True
        self.recording = False
        self.battery = {"power_type": 1, "battery_level": 80, "battery_scale": 100}
        self.storage = {"state": 0, "free_space": 20_000_000_000, "total_space": 64_000_000_000}
        self.lens = types.SimpleNamespace(name="Pano577")
        self.encode = types.SimpleNamespace(name="H265")
        self.callbacks = {}
        self.files = []
        self.queries = 0

    def is_connected(self):
        return True

    def set_video_sub_mode(self, mode):
        self.calls.append(("mode", mode))
        return self.mode_ok

    def set_video_capture_params(self, resolution, bitrate=0, mode=None):
        self.calls.append(("params", resolution, bitrate))
        return self.params_ok

    def start_recording(self):
        self.calls.append(("start",))
        if not self.start_ok:
            return False
        os.write(2, b"[11-14 13:00:00.123] I camera: NORMAL_CAPTURE started\n")   # what the SDK logs
        self.recording = True
        self.files.append("VID_1.insv")
        return True

    def stop_recording(self):
        self.calls.append(("stop",))
        self.recording = False

    def capture_current_status(self):
        self.queries += 1
        return self.recording

    def get_battery_status(self):
        return self.battery

    def get_storage_state(self):
        return self.storage

    def get_camera_lens_type(self):
        return self.lens

    def get_video_encode_type(self):
        return self.encode

    def get_camera_files_list(self):
        return list(self.files)

    def get_media_time(self):
        return 1234

    def sync_local_time_to_camera(self, utc, offset):
        self.calls.append(("sync", utc, offset))

    def __getattr__(self, name):
        if name.startswith("set_") and name.endswith("_notification"):
            return lambda callback: self.callbacks.__setitem__(name, callback)
        raise AttributeError(name)


DESCRIPTOR = types.SimpleNamespace(camera_name="Insta360 X4", camera_type="CameraType.Insta360X4",
                                   serial_number="IXSE123", fw_version="v1.0")


@pytest.fixture
def cam(monkeypatch):
    """The fake camera is installed as the SDK camera behind app.core.camera"""
    fake = FakeSdkCamera()
    camera._camera = fake
    camera._opened(fake, DESCRIPTOR)
    monkeypatch.setattr(config, "CAMERA_SET_MODE", True)
    monkeypatch.setattr(config, "CAMERA_QUALITY", "5.7K30")
    monkeypatch.setattr(config, "CAMERA_BITRATE_MBPS", 0.0)
    monkeypatch.setattr(config, "CAMERA_SYNC_CLOCK", True)
    monkeypatch.setattr(config, "CAMERA_INFO_REFRESH_S", 0.0)
    camera._module_warned = False
    position.feed.last_position = None             # nothing verifies the system clock unless a test says so
    yield fake
    camera._camera = None
    camera._closed()


# --- video mode and quality before recording -----------------------------------------------------------------

def test_recording_starts_in_the_video_mode_with_the_quality(cam):
    assert camera.start_recording() > 0
    names = [c[0] for c in cam.calls]
    assert names.index("mode") < names.index("params") < names.index("start")
    assert cam.calls[0] == ("mode", camerasdk.SubVideoMode.VIDEO_NORMAL)
    assert cam.calls[1] == ("params", camerasdk.VideoResolution.RES_2880_2880P30, 0)
    info = camera.get_info()
    assert info["mode"] == "video_normal" and info["quality"] == "5.7K/30"


@pytest.mark.parametrize("setting, resolution", [("8K30", "RES_3840_3840P30"), ("5.7K/30", "RES_2880_2880P30"),
                                                 (" 5.7k30 ", "RES_2880_2880P30"), ("4K30", "RES_3840_1920P30")])
def test_quality_settings(cam, monkeypatch, setting, resolution):
    monkeypatch.setattr(config, "CAMERA_QUALITY", setting)
    camera.start_recording()
    assert cam.calls[1][1] == getattr(camerasdk.VideoResolution, resolution)


def test_bitrate_is_in_megabits(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_BITRATE_MBPS", 50.0)
    camera.start_recording()
    assert cam.calls[1][2] == 50 * 1024 * 1024


def test_no_quality_setting_leaves_the_camera_as_it_is(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_QUALITY", "")
    camera.start_recording()
    assert [c[0] for c in cam.calls] == ["mode", "start"] and camera.get_info()["quality"] is None


def test_mode_setting_can_be_switched_off(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_SET_MODE", False)
    camera.start_recording()
    assert [c[0] for c in cam.calls] == ["start"]


def test_an_unknown_quality_is_reported_before_anything_is_started(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_QUALITY", "9K99")
    with pytest.raises(RuntimeError, match="unknown camera quality '9K99', use one of 4K24"):
        camera.start_recording()
    assert ("start",) not in cam.calls


def test_a_camera_that_refuses_the_video_mode_says_so(cam):
    cam.mode_ok = False
    with pytest.raises(RuntimeError, match="refused to switch to the video mode"):
        camera.start_recording()
    assert ("start",) not in cam.calls and "refused" in camera.get_info()["warnings"][0]


def test_a_camera_that_refuses_the_quality_says_which(cam):
    cam.params_ok = False
    with pytest.raises(RuntimeError, match="refused the quality 5.7K30"):
        camera.start_recording()
    assert ("start",) not in cam.calls


def test_a_camera_that_refuses_to_record_is_not_waited_for(cam):
    """It used to wait 3 s and say 'timed out waiting for NORMAL_CAPTURE'"""
    cam.start_ok = False
    before = os.readlink("/proc/self/fd/2")
    start = time.time()
    with pytest.raises(RuntimeError, match="refused to start recording"):
        camera.start_recording()
    assert time.time() - start < 1.5
    assert os.readlink("/proc/self/fd/2") == before            # stderr was put back
    assert not os.path.exists("sdk_capture.log")


def test_stderr_is_restored_when_the_camera_raises(cam):
    cam.start_recording = lambda: (_ for _ in ()).throw(OSError("usb error"))
    before = os.readlink("/proc/self/fd/2")
    with pytest.raises(OSError):
        camera.start_recording()
    assert os.readlink("/proc/self/fd/2") == before and not os.path.exists("sdk_capture.log")


def test_an_old_module_without_the_new_functions_still_records_and_says_so(cam):
    class Old(FakeSdkCamera):
        def __getattribute__(self, name):
            if name in ("set_video_sub_mode", "set_video_capture_params", "sync_local_time_to_camera"):
                raise AttributeError(name)
            return super().__getattribute__(name)
    old = Old()
    camera._camera = old
    camera._opened(old, DESCRIPTOR)
    assert camera.start_recording() > 0 and camera.start_recording is not None
    assert any(e["kind"] == "warning" and "too old" in e["detail"] for e in events.recent(5))
    camera.start_recording()
    assert len([e for e in events.recent(20) if "too old" in e["detail"]]) == 1   # said once


# --- the camera clock --------------------------------------------------------------------------------------------

def feed_position(age_s=0.0, clock_error_s=0.0):
    position.feed.last_position = Position(latitude=50, longitude=14,
                                           timestamp_ns=time.time_ns() + int(clock_error_s * 1e9))
    position.feed.last_monotonic = time.monotonic() - age_s


def test_the_clock_is_synced_when_the_gnss_confirms_it(cam):
    feed_position()
    camera.start_recording()
    syncs = [c for c in cam.calls if c[0] == "sync"]
    assert len(syncs) == 1 and abs(syncs[0][1] - time.time()) < 3 and syncs[0][2] == time.localtime().tm_gmtoff
    camera.start_recording()
    assert len([c for c in cam.calls if c[0] == "sync"]) == 1       # once per camera session


@pytest.mark.parametrize("age, error", [(30, 0), (0, 10), (0, -10)])
def test_the_clock_is_not_synced_from_an_unverified_system_clock(cam, age, error):
    feed_position(age_s=age, clock_error_s=error)
    camera.start_recording()
    assert not [c for c in cam.calls if c[0] == "sync"]


def test_no_gnss_position_means_no_sync(cam):
    position.feed.last_position = None
    camera.start_recording()
    assert not [c for c in cam.calls if c[0] == "sync"]


def test_clock_sync_can_be_switched_off(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_SYNC_CLOCK", False)
    feed_position()
    camera.start_recording()
    assert not [c for c in cam.calls if c[0] == "sync"]


# --- the state of the camera -----------------------------------------------------------------------------------------

def test_info_has_who_what_and_how_full(cam):
    info = camera.get_info()
    assert info["connected"] and info["name"] == "Insta360 X4" and info["type"] == "Insta360X4"
    assert info["video"] == "360" and info["lens"] == "Pano577" and info["encode"] == "h265"
    assert info["battery"] == {"level": 80, "scale": 100, "percent": 80}
    assert info["card"] == {"state": "ok", "free_mb": 20000, "total_mb": 64000, "free_percent": 31}
    assert info["recording"] is False and info["warnings"] == []


def test_info_of_a_camera_that_is_not_there():
    camera._camera = None
    assert camera.get_info() == {"connected": False}


def test_warnings(cam):
    cam.battery = {"power_type": 1, "battery_level": 10, "battery_scale": 100}
    cam.storage = {"state": 0, "free_space": 3_000_000_000, "total_space": 64_000_000_000}
    camera.refresh_info(force=True)
    warnings = camera.get_info()["warnings"]
    assert "the camera battery is at 10 %" in warnings and "the camera card has only 5 % free" in warnings
    cam.storage = {"state": 2, "free_space": 0, "total_space": 64_000_000_000}
    cam.battery = {"power_type": 1, "battery_level": 80, "battery_scale": 100}
    camera.refresh_info(force=True)
    assert camera.get_info()["warnings"] == ["the camera card is full"]
    cam.storage = {"state": 1, "free_space": 0, "total_space": 0}
    camera.refresh_info(force=True)
    assert camera.get_info()["warnings"] == ["camera card: no card"] and camera.get_info()["card"]["free_percent"] is None


def test_the_camera_cannot_break_the_status(cam):
    cam.get_battery_status = lambda: (_ for _ in ()).throw(OSError("usb"))
    cam.get_storage_state = lambda: None
    camera.refresh_info(force=True)
    info = camera.get_info()
    assert info["connected"] and "battery" not in info and "card" not in info and info["recording"] is False


def test_the_state_is_cached_so_a_status_never_waits_for_the_camera(cam, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_INFO_REFRESH_S", 60.0)
    camera.refresh_info(force=True)
    queries = cam.queries
    for _ in range(10):
        camera.get_info()
        camera.refresh_info()
    assert cam.queries == queries                                   # nothing asked the camera again
    camera.refresh_info(force=True)
    assert cam.queries == queries + 1


def test_the_supervisor_check_keeps_the_state_fresh(cam):
    queries = cam.queries
    assert camera.healthy() is True and cam.queries == queries + 1


# --- what the camera reports by itself ---------------------------------------------------------------------------------

def test_the_camera_stopping_by_itself_is_reported(cam):
    camera.start_recording()
    cam.callbacks["set_capture_stopped_notification"]("file.insv", int(camerasdk.CaptureStoppedErrorCode.STORAGE_FULL))
    info = camera.get_info()
    assert info["stopped"]["reason"] == "STORAGE_FULL"
    assert "the camera stopped recording by itself: STORAGE_FULL" in info["warnings"]
    assert any(e["source"] == "camera" and e["kind"] == "capture_stopped" for e in events.recent(5))


def test_our_own_stop_is_not_reported_as_the_camera_stopping(cam):
    camera.start_recording()
    camera.stop_recording()
    cam.callbacks["set_capture_stopped_notification"]("file.insv", 2)     # the SDK reports every stop
    assert "stopped" not in camera.get_info() or camera.get_info()["stopped"] is None


def test_a_new_recording_clears_an_old_stop(cam):
    camera.start_recording()
    cam.callbacks["set_capture_stopped_notification"]("f", int(camerasdk.CaptureStoppedErrorCode.LOW_BATTERY))
    assert camera.get_info()["stopped"]["reason"] == "LOW_BATTERY"
    camera.stop_recording()
    camera.start_recording()
    assert camera.get_info().get("stopped") is None


def test_low_battery_full_card_and_heat_callbacks(cam):
    cam.callbacks["set_battery_low_notification"](7)
    cam.callbacks["set_storage_full_notification"]()
    cam.callbacks["set_temperature_high_notification"]()
    warnings = camera.get_info()["warnings"]
    assert "the camera battery is low" in warnings and "the camera card is full" in warnings and "the camera is too hot" in warnings


def test_a_camera_that_is_already_recording_when_opened_is_noticed():
    fake = FakeSdkCamera()
    fake.recording = True
    camera._camera = fake
    camera._opened(fake, DESCRIPTOR)
    assert camera.get_info()["recording"] is True
    assert any("already recording" in e["detail"] for e in events.recent(5))
    camera._camera = None


# --- in /status, with a measurement ------------------------------------------------------------------------------------

@pytest.fixture
def client(bus, tmp_path, monkeypatch, cam):
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(camera, "open_camera", lambda: None)             # the fake is installed already
    monkeypatch.setattr(camera, "close_camera", lambda: True)
    from app.main import app
    with TestClient(app) as c:
        c.tmp = tmp_path / "data"
        yield c


def test_status_has_the_camera_block(client):
    status = client.get("/status").json()
    assert status["status"] == "ready" or status["sensors"]["camera"] == "ok"
    block = status["camera"]
    assert block["connected"] and block["video"] == "360" and block["battery"]["percent"] == 80
    assert client.get("/camera/info").json()["name"] == "Insta360 X4"


def test_a_measurement_sets_the_mode_and_reports_the_quality(client, cam):
    assert client.post("/start", params={"project_name": "cam1"}).status_code == 200
    assert client.get("/status").json()["camera"]["quality"] == "5.7K/30"
    assert [c[0] for c in cam.calls if c[0] != "sync"][:3] == ["mode", "params", "start"]
    assert client.post("/stop", params={"project_name": "cam1"}).status_code == 200


def test_a_camera_in_photo_mode_gives_a_clear_reason_and_leaves_nothing_behind(client, cam):
    cam.mode_ok = False
    r = client.post("/start", params={"project_name": "cam2"})
    assert r.status_code == 500 and "refused to switch to the video mode" in r.json()["reason"]
    assert not (client.tmp / "cam2").exists()
    cam.mode_ok = True
    assert client.post("/start", params={"project_name": "cam2"}).status_code == 200      # the next try works
    client.post("/stop", params={"project_name": "cam2"})


def test_the_camera_stopping_during_a_measurement_is_a_problem_of_the_recording(client, cam):
    client.post("/start", params={"project_name": "cam3"})
    cam.callbacks["set_capture_stopped_notification"]("f", int(camerasdk.CaptureStoppedErrorCode.STORAGE_FULL))
    status = client.get("/status").json()
    assert any("camera stopped recording by itself: STORAGE_FULL" in p for p in status["recording"]["problems"])
    assert any("STORAGE_FULL" in w for w in status["warnings"])
    stopped = client.post("/stop", params={"project_name": "cam3"}).json()
    assert any("STORAGE_FULL" in w for w in stopped["warnings"])


def test_a_camera_that_silently_stopped_is_noticed(client, cam, monkeypatch):
    client.post("/start", params={"project_name": "cam4"})
    cam.recording = False                                              # no callback, the camera just is not recording
    camera.refresh_info(force=True)
    assert "the camera is not recording" not in client.get("/status").json()["recording"]["problems"]   # not in the first seconds
    monkeypatch.setattr(common, "_measurement_start_mono_ns", time.monotonic_ns() - 20 * 10**9)
    assert "the camera is not recording" in client.get("/status").json()["recording"]["problems"]
    client.post("/stop", params={"project_name": "cam4"})


def test_a_camera_recording_without_a_measurement_is_a_warning(client, cam):
    cam.recording = True
    camera.refresh_info(force=True)
    assert "the camera is recording but no measurement runs" in client.get("/status").json()["warnings"]


def test_the_ble_status_carries_the_camera_block(client, cam):
    from gatt.status_poller import compact_status
    out = compact_status(client.get("/status").json())
    assert out["camera"]["video"] == "360" and out["camera"]["battery"]["percent"] == 80
    assert "warnings" not in out["camera"] and "lens" not in out["camera"]
