from sdk import camerasdk
import logging
import time
import os
import re
from datetime import datetime
from app.core import config, events

log = logging.getLogger(__name__)

# The global camera object lives here
_camera = None

def open_camera():
    """One attempt to find and open the camera. Raises RuntimeError if there is none: the supervisor
    (app/core/supervisor.py) tries again, so the server starts without a camera and picks it up when
    it is plugged in"""
    global _camera

    discovery = camerasdk.DeviceDiscovery()
    devices = discovery.get_available_devices()

    if len(devices) == 0:
        raise RuntimeError("no camera found")

    camera = camerasdk.Camera(devices[0].info)
    camera.open()
    _camera = camera
    _opened(camera, devices[0])

def verify_connection():
    try:
        is_connected = _camera.is_connected()
        return is_connected
    except:
        return False

def get_camera():
    """Returns the camera connected"""
    if not verify_connection():
        raise RuntimeError("Camera not connected")
    return _camera

def close_camera():
    """Closes (e.g.) disconnects the current camera"""
    # When we write to globals we must state it
    global _camera

    camera, _camera = _camera, None
    _closed()
    if camera is None:
        return False

    try:
        camera.close()
    except Exception as e:  # a camera that was pulled out cannot be closed politely
        print(f"Closing the camera failed: {e}")
    return True

def start_recording():
    """
    Starts recording and returns the actual t0 in nanoseconds,
    parsed from the camera's own NORMAL_CAPTURE log line.
    """
    if not verify_connection():
        return False

    # Video mode, quality and clock; raises with the reason when the camera refuses
    prepare_recording()

    # Redirect stderr to capture SDK logs
    LOG_FILE = "sdk_capture.log"
    log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    old_stderr = os.dup(2)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    t0_ns = None
    try:
        started = _camera.start_recording()

        # Poll the log file until NORMAL_CAPTURE appears (or timeout)
        pattern = re.compile(r'\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\].*NORMAL_CAPTURE')
        deadline = time.time() + 3.0  # 3s timeout

        while started is not False and time.time() < deadline:
            with open(LOG_FILE) as f:
                for line in f:
                    m = pattern.search(line)
                    if m:
                        now = datetime.now()
                        parsed = datetime.strptime(
                            f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S.%f"
                        )
                        t0_ns = int(parsed.timestamp() * 1_000_000_000)
                        break
            if t0_ns:
                break
            time.sleep(0.001)
    finally:
        # Restore stderr, whatever happened
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        os.remove(LOG_FILE)

    if started is False:
        raise RuntimeError("the camera refused to start recording (is it in a photo mode, or is the card full?)")

    if not t0_ns:
        raise RuntimeError("Timed out waiting for NORMAL_CAPTURE confirmation")

    _info_changed(recording=True, stopped=None)
    return t0_ns

def stop_recording():
    if not verify_connection():
        return False

    expect_stop()  # the SDK reports this stop too: it is not "the camera stopped by itself"
    _camera.stop_recording()
    _info_changed(recording=False)
    return True

def get_camera_files_list():
    if not verify_connection():
        return 1
    
    return _camera.get_camera_files_list()

def download_file(file, local_file, progress_callback):
    if not verify_connection():
        return False
    
    _camera.download_file(file, local_file, progress_callback)
    return True

def download_files(project_path, files):
    if not verify_connection():
        return False

    for file in files:
        file_name = os.path.basename(file)
        local_file = os.path.join(project_path, file_name)

        if not download_file(file, local_file, progress):
            return False

    return True

def download_all(project_path):
    if not verify_connection():
        return False
    
    files = get_camera_files_list()

    for file in files:
        file_name = os.path.basename(file)
        local_file = os.path.join(project_path, file_name)

        if not download_file(file, local_file, progress):
            return False
    
    return True

def delete_file(file):
    if not verify_connection():
        return False
    
    _camera.delete_file(file)
    return True

def delete_all():
    if not verify_connection():
        return False
    
    files = get_camera_files_list()

    for file in files:
        if not delete_file(file):
            return False
    
    return True

def get_battery_status():
    if not verify_connection():
        return False

    return _camera.get_battery_status()

def get_storage_state():
    if not verify_connection():
        return False

    return _camera.get_storage_state()

def get_media_time():
    if not verify_connection():
        return False
    
    return _camera.get_media_time()

def shutdown_camera():
    if not verify_connection():
        return False

    return _camera.shutdown_camera()

# --- video mode, quality and the state of the camera ----------------------------------------------------
# These use functions of the camerasdk module that older builds of the wrapper do not have: without them
# the server still records (the camera must then be in a video mode already) and says so in the status.

# The quality names of the settings, and what they are in the SDK (VideoResolution, the primary names)
QUALITIES = {
    "4K24": "RES_3840_1920P24", "4K25": "RES_3840_1920P25", "4K30": "RES_3840_1920P30",
    "4K50": "RES_3840_1920P50", "4K60": "RES_3840_1920P60", "4K100": "RES_3840_1920P100",
    "5.7K24": "RES_2880_2880P24", "5.7K25": "RES_2880_2880P25", "5.7K30": "RES_2880_2880P30",
    "5.7K50": "RES_2880_2880P50", "5.7K60": "RES_2880_2880P60",
    "6K24": "RES_3008_3008P24", "6K25": "RES_3008_3008P25", "6K30": "RES_3008_3008P30",
    "8K24": "RES_3840_3840P24", "8K25": "RES_3840_3840P25", "8K30": "RES_3840_3840P30",
    "11K30": "RES_5632_5632P30",
}
CARD_STATES = {0: "ok", 1: "no_card", 2: "full", 3: "invalid_format", 4: "write_protected", 5: "error"}

_device: dict = {}
_settings: dict = {"mode": None, "quality": None, "applied": False, "error": None}
_info: dict = {}
_info_time = 0.0
_flags: dict = {"stopped": None, "battery_low": None, "storage_full": False, "temperature_high": False,
                "capturing": None}
_expected_stop_until = 0.0
_clock_synced = False
_module_warned = False


def quality_label(key: str) -> str:
    """5.7K30 -> 5.7K/30"""
    for i, ch in enumerate(key):
        if ch == "K":
            return f"{key[:i + 1]}/{key[i + 1:]}"
    return key


def resolve_quality(setting: str):
    """The SDK resolution for a quality setting like 5.7K30. Raises ValueError for one that does not exist"""
    key = setting.strip().upper().replace("/", "").replace(" ", "")
    if key in QUALITIES:
        return key, getattr(camerasdk.VideoResolution, QUALITIES[key])
    raise ValueError(f"unknown camera quality '{setting}', use one of {', '.join(QUALITIES)}")


def prepare_recording():
    """Puts the camera into the video mode and sets the quality (CAMERA_SET_MODE, CAMERA_QUALITY), and
    syncs its clock when the GNSS confirms the system clock. Raises RuntimeError with the reason when the
    camera refuses: a camera left in a photo mode would otherwise just time out"""
    global _module_warned
    camera = _camera
    if not config.CAMERA_SET_MODE:
        return
    if not hasattr(camera, "set_video_sub_mode"):
        if not _module_warned:
            _module_warned = True
            events.record("camera", "warning", "the camerasdk module is too old to set the video mode: "
                                               "rebuild the wrapper; the camera must be in a video mode itself")
        return
    if not camera.set_video_sub_mode(camerasdk.SubVideoMode.VIDEO_NORMAL):
        _settings.update(applied=False, error="the camera refused the video mode")
        raise RuntimeError("the camera refused to switch to the video mode")
    _settings.update(mode="video_normal", applied=True, error=None)
    if config.CAMERA_QUALITY:
        try:
            key, resolution = resolve_quality(config.CAMERA_QUALITY)
        except ValueError as e:
            raise RuntimeError(str(e))
        bitrate = int(config.CAMERA_BITRATE_MBPS * 1024 * 1024)
        if not camera.set_video_capture_params(resolution, bitrate):
            _settings.update(quality=None, error=f"the camera refused the quality {key}")
            raise RuntimeError(f"the camera refused the quality {key} (does this camera model have it?)")
        _settings.update(quality=key)
    sync_clock()


def clock_trusted() -> bool:
    """The system clock agrees with the GNSS time (the camera clock must not be set from an unverified clock)"""
    from app.core import position
    feed = position.feed
    if feed.last_position is None or time.monotonic() - feed.last_monotonic > 5:
        return False
    return abs(time.time_ns() - feed.last_position.timestamp_ns) < 2_500_000_000


def sync_clock() -> bool:
    """Sets the camera clock (it stamps its files) when the system clock is verified. Once per camera session"""
    global _clock_synced
    if _clock_synced or not config.CAMERA_SYNC_CLOCK or not hasattr(_camera, "sync_local_time_to_camera"):
        return False
    if not clock_trusted():
        return False
    now = int(time.time())
    try:
        _camera.sync_local_time_to_camera(now, time.localtime(now).tm_gmtoff)
    except Exception as e:
        events.record("camera", "warning", f"could not sync the camera clock: {e}")
        return False
    _clock_synced = True
    return True


def expect_stop(seconds: float = 3.0):
    global _expected_stop_until
    _expected_stop_until = time.monotonic() + seconds


def _info_changed(**values):
    """Updates the cached state at once, so a status right after a start or stop is true"""
    global _info
    if "stopped" in values:
        _flags["stopped"] = values.pop("stopped")
    _info = {**_info, **values}


def _capture_stopped(url, err_code):
    """The SDK says a recording ended. Our own stop is expected; any other is the camera stopping by itself"""
    if time.monotonic() < _expected_stop_until:
        return
    try:
        reason = camerasdk.CaptureStoppedErrorCode(err_code).name
    except Exception:
        reason = f"code {err_code}"
    _flags["stopped"] = {"reason": reason, "ts_ns": time.time_ns()}
    events.record("camera", "capture_stopped", f"the camera stopped recording: {reason}")


def _register_notifications(camera):
    def set_flag(name, value, text=None):
        def callback(*args):
            _flags[name] = value if value is not None else (args[0] if args else True)
            if text:
                events.record("camera", "warning", text)
        return callback

    for setter, callback in (
            ("set_capture_stopped_notification", _capture_stopped),
            ("set_battery_low_notification", set_flag("battery_low", None, "the camera battery is low")),
            ("set_storage_full_notification", set_flag("storage_full", True, "the camera card is full")),
            ("set_temperature_high_notification", set_flag("temperature_high", True, "the camera is too hot")),
            ("set_capture_state_notification", set_flag("capturing", None))):
        try:
            getattr(camera, setter)(callback)
        except Exception as e:  # an older module, or a camera that cannot report it
            log.info("no %s: %s", setter, e)


def _opened(camera, descriptor):
    """A camera was opened: remember who it is, listen for what it reports, read its state"""
    global _clock_synced, _info, _info_time, _expected_stop_until
    _clock_synced = False
    _expected_stop_until = 0.0
    _flags.update(stopped=None, battery_low=None, storage_full=False, temperature_high=False, capturing=None)
    _settings.update(mode=None, quality=None, applied=False, error=None)
    _device.clear()
    try:
        _device.update(name=descriptor.camera_name, type=str(descriptor.camera_type).split(".")[-1],
                       serial=descriptor.serial_number, firmware=descriptor.fw_version)
    except Exception:
        pass
    _register_notifications(camera)
    _info, _info_time = {}, 0.0
    refresh_info(force=True)
    if _info.get("recording"):
        events.record("camera", "warning", "the camera is already recording (started outside this server)")


def _closed():
    global _info, _info_time
    _info, _info_time = {}, 0.0


def _call(name, *args):
    """A camera call that never raises: None when the camera does not answer or does not have it"""
    try:
        function = getattr(_camera, name, None)
        return function(*args) if function else None
    except Exception:
        return None


def refresh_info(force: bool = False):
    """Reads battery, card, lens and recording state from the camera, at most every CAMERA_INFO_REFRESH_S.
    Called by the supervisor, so a status never waits for the camera"""
    global _info, _info_time
    if _camera is None or (not force and time.monotonic() - _info_time < config.CAMERA_INFO_REFRESH_S):
        return
    info = {}
    battery = _call("get_battery_status")
    if battery:
        scale = battery.get("battery_scale") or 0
        info["battery"] = {"level": battery.get("battery_level"), "scale": scale,
                           "percent": round(100 * battery["battery_level"] / scale) if scale else None}
    storage = _call("get_storage_state")
    if storage:
        total, free = storage.get("total_space") or 0, storage.get("free_space") or 0
        info["card"] = {"state": CARD_STATES.get(storage.get("state"), "error"),
                        "free_mb": round(free / 1e6), "total_mb": round(total / 1e6),
                        "free_percent": round(100 * free / total) if total else None}
    recording = _call("capture_current_status")
    if recording is not None:
        info["recording"] = bool(recording)
    lens = _call("get_camera_lens_type")
    if lens is not None:
        info["lens"] = lens.name
    encode = _call("get_video_encode_type")
    if encode is not None:
        info["encode"] = encode.name.lower()
    # what the camera did not answer is dropped, not kept: a battery level from a minute ago is not "the" level
    _info = info
    _info_time = time.monotonic()


def healthy() -> bool:
    """For the supervisor: the camera is connected; also keeps the cached state fresh"""
    if not verify_connection():
        return False
    refresh_info()
    return True


def get_info() -> dict:
    """What /status says about the camera: who, in what mode and quality, battery, card, recording, and
    what it reported by itself. Cached, it never waits for the camera"""
    if _camera is None or not verify_connection():
        return {"connected": False}
    info = {"connected": True, **_device, **_info}
    lens = _info.get("lens", "")
    info["video"] = "360" if lens.startswith("Pano") else ("wide" if lens else None)
    info["mode"] = _settings["mode"]
    info["quality"] = quality_label(_settings["quality"]) if _settings["quality"] else None
    warnings = []
    if _settings["error"]:
        warnings.append(f"camera: {_settings['error']}")
    stopped = _flags["stopped"]
    if stopped:
        info["stopped"] = stopped
        warnings.append(f"the camera stopped recording by itself: {stopped['reason']}")
    battery = _info.get("battery")
    if _flags["battery_low"] is not None:
        warnings.append("the camera battery is low")
    elif battery and battery.get("percent") is not None and battery["percent"] <= 15:
        warnings.append(f"the camera battery is at {battery['percent']} %")
    card = _info.get("card")
    if _flags["storage_full"] or (card and card["state"] != "ok"):
        warnings.append("the camera card is full" if _flags["storage_full"] or card["state"] == "full"
                        else f"camera card: {card['state'].replace('_', ' ')}")
    elif card and card["free_percent"] is not None and card["free_percent"] <= 10:
        warnings.append(f"the camera card has only {card['free_percent']} % free")
    if _flags["temperature_high"]:
        warnings.append("the camera is too hot")
    info["warnings"] = warnings
    return info


# Helpers
def progress(current, total):
    """Callback function for the download progress"""
    percent = (current / total) * 100
    print(f"Downloading file: {round(percent, 3)} %")

def run_delay_test(count: int = 10):
    # Redirect stderr to file
    LOG_FILE = "sdk_stderr.log"
    log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    old_stderr = os.dup(2)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    t0_ns_list = []
    for _ in range(count):
        _camera.start_recording()
        t0_ns_list.append(time.time_ns())
        time.sleep(0.5)
        _camera.stop_recording()
        time.sleep(0.5)

    # Restore stderr
    os.dup2(old_stderr, 2)
    os.close(old_stderr)

    # Parse log
    with open(LOG_FILE) as f:
        lines = f.readlines()

    # Remove file after reading
    os.remove(LOG_FILE)

    pattern = re.compile(r'\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\].*NORMAL_CAPTURE')
    now = datetime.now()
    cam_times = []
    in_recording = False
    not_capture = re.compile(r'NOT_CAPTURE')
    for line in lines:
        if not_capture.search(line):
            in_recording = False
            continue
        m = pattern.search(line)
        if m and not in_recording:
            in_recording = True
            print(f"Matched: {line.strip()}")
            parsed = datetime.strptime(f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S.%f")
            cam_times.append(int(parsed.timestamp() * 1_000_000_000))

    deltas = [t0 - tc for t0, tc in zip(t0_ns_list, cam_times)]
    return {
        "count": len(deltas),
        "average_ms": sum(deltas) / len(deltas) / 1_000_000 if deltas else None,
        "deltas_ms": [d / 1_000_000 for d in deltas]
    }