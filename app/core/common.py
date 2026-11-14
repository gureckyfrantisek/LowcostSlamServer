# Shared functions for all the sensors
import os
import time
import json
import shutil
import threading
from dataclasses import dataclass
from typing import Optional
from app.core import camera, config, events, gnss, imu, storage, supervisors
from app.core.control_points import control_points

# Only a shortcut to if we're recording or not
_measurement = False
_measurement_name = None
_measurement_start_ns = 0
_measurement_start_mono_ns = 0
_measurement_path = None  # folder of the running measurement (the stick may be gone when it stops)
_start_error = None  # why the last start failed
# Start and stop come from separate request threads: one at a time
_measurement_lock = threading.RLock()

def now_ns() -> int:
    return time.time_ns()

def get_status():
    """Checks the status of all sensors

    Returns:
        Int: a binary sum of the results

        4 for camera

        2 for GNSS

        1 for IMU
    """
    result = 0

    if not camera.verify_connection():
        result += 4

    if not gnss.verify_connection():
        result += 2

    if not imu.verify_connection():
        result += 1

    return result


def device_info() -> dict:
    """Identity and settings of this device, for the app to show and to check compatibility"""
    return {
        "name": config.DEVICE_NAME,
        "id": config.DEVICE_ID,
        "api_version": 1,
        "imu": {"hz": config.IMU_I2C_HZ, "range_g": config.IMU_RANGE_G},
        "gnss": {"hz": config.GNSS_RATE_HZ},
        "storage": {"local_path": config.LOCAL_DATA_PATH},
        "capabilities": ["measurement", "projects", "camera", "position", "usb_storage"],
    }

def get_sensor_states() -> dict:
    """What each device is doing: ok, or missing with the reason, and since when"""
    out = {}
    for name, ok in (("camera", camera.verify_connection()), ("gnss", gnss.verify_connection()),
                     ("imu", imu.verify_connection())):
        sup = supervisors.get(name)
        out[name] = {
            "state": "ok" if ok else "missing",
            "detail": None if ok else (sup.error if sup and sup.error else "not connected"),
            "since": sup.since if sup else None,
        }
    return out

def get_start_error() -> str | None:
    return _start_error

def get_recording_state() -> dict:
    """Whether a measurement runs and whether something went wrong in it"""
    problems = []
    if _measurement:
        for name, error in (("imu", imu.get_stats().get("log_error")), ("gnss", gnss.get_stats().get("log_error"))):
            if error:
                problems.append(f"{name} data is no longer written: {error}")
        for name, state in get_sensor_states().items():
            if state["state"] != "ok":
                problems.append(f"{name} is not connected, its data has a gap")
        info = camera.get_info()
        stopped = info.get("stopped")
        if stopped and stopped["ts_ns"] >= _measurement_start_ns:
            problems.append(f"the camera stopped recording by itself: {stopped['reason']}")
        elif info.get("connected") and info.get("recording") is False and \
                time.monotonic_ns() - _measurement_start_mono_ns > 10 * 1_000_000_000:
            problems.append("the camera is not recording")
    state = {"active": bool(_measurement), "project": _measurement_name if _measurement else None,
             "problems": problems}
    if _measurement:
        # elapsed is counted here, so a phone that reconnects shows the right time without the clocks agreeing
        state["started_ns"] = _measurement_start_ns
        state["elapsed_s"] = round((time.monotonic_ns() - _measurement_start_mono_ns) / 1e9, 1)
        state["control_points"] = control_points.state()
        if control_points.write_error:
            problems.append(f"control points are no longer written: {control_points.write_error}")
    return state

def start_measurement(project_name):
    """Starts video capture and GNSS and IMU logging
    Returns:
        Measurement: The measurement object
    """
    with _measurement_lock:
        return _start_measurement(project_name)

def _start_measurement(project_name):
    global _measurement, _measurement_name, _measurement_start_ns, _measurement_start_mono_ns, _measurement_path, _start_error

    # Return if already recording
    if _measurement:
        return 1

    if not storage.valid_project_name(project_name):
        return 4

    # Refuse before anything starts, so no sensor is left running
    if any(state["state"] != "ok" for state in get_sensor_states().values()):
        return 6

    # Refuse before anything starts when there is no room for even a short recording
    existing = storage.find_project_path(project_name)
    if not existing and storage.level(storage.free_bytes(storage.data_root())) == "full":
        return 5

    # Remember what existed before, so a failed start can be undone cleanly
    project_path = existing or os.path.join(storage.data_root(), project_name)
    local_files_before = set(os.listdir(project_path)) if os.path.isdir(project_path) else None
    camera_files_before = camera.get_camera_files_list()

    try:
        # Name the measurement so that the files are consistent too
        local_path = get_local_path(project_name)

        events.start_recording(local_path)
        control_points.begin_recording(local_path)

        # Start IMU and GNSS immediately
        imu_start_ns = imu.start_logging(local_path)
        gnss_start_ns = gnss.start_logging(local_path)

        # Start recording
        t0_ns = camera.start_recording()
        if not t0_ns:
            abort_start(project_path, local_files_before, camera_files_before)
            return 2

        # Get time anchor
        media_time = camera.get_media_time()

        # Write metadata file for postprocessing
        metadata = {
            "name": project_name,
            "t0_ns": t0_ns,
            "camera_media_time_at_t0_ms": media_time,
            "gnss_start_offset_ns": gnss_start_ns - t0_ns,
            "imu_start_offset_ns": imu_start_ns - t0_ns
        }

        meta_path = os.path.join(local_path, "meta.json")

        with open(meta_path, "w") as file:
            json.dump(metadata, file, indent=2)
    except Exception as e:
        abort_start(project_path, local_files_before, camera_files_before)
        _start_error = f"{type(e).__name__}: {e}"
        events.record("server", "error", f"start failed: {_start_error}")
        return 7

    # Only mark as measuring once everything succeeded
    _measurement = True
    _measurement_name = project_name
    _measurement_start_ns = time.time_ns()
    _measurement_start_mono_ns = time.monotonic_ns()
    _measurement_path = local_path
    return True

def abort_start(project_path, local_files_before, camera_files_before):
    """Undoes a failed start_measurement: stops IMU/GNSS logging and the
    recording, then deletes the local and camera files created by the attempt.

    local_files_before: files in the project folder before the attempt, or None
    if the folder did not exist (the whole folder is then removed)."""

    def attempt(step, *args):
        # One failing step must not stop the rest of the cleanup
        try:
            step(*args)
        except Exception as e:
            print(f"Start cleanup step {step.__name__} failed: {e}")

    attempt(imu.stop_logging)
    attempt(gnss.stop_logging)
    attempt(camera.stop_recording)
    attempt(control_points.end_recording)
    attempt(events.stop_recording)

    # Camera files created by the attempt
    camera_files_now = camera.get_camera_files_list()
    if isinstance(camera_files_before, (list, tuple, set)) and isinstance(camera_files_now, (list, tuple, set)):
        for file in set(camera_files_now) - set(camera_files_before):
            attempt(camera.delete_file, file)

    # Local files created by the attempt
    if local_files_before is None:
        shutil.rmtree(project_path, ignore_errors=True)
    elif os.path.isdir(project_path):
        for name in set(os.listdir(project_path)) - local_files_before:
            path = os.path.join(project_path, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                attempt(os.remove, path)

def stop_measurement(project_name):
    """Stops everything and closes the recording. Returns False if nothing was recording, else the list of
    problems met on the way (empty when all is well): a lost sensor or a full disk does not stop the stop"""
    with _measurement_lock:
        return _stop_measurement(project_name)

def _stop_measurement(project_name):
    global _measurement, _measurement_name
    if not _measurement:
        return False

    problems = list(get_recording_state()["problems"])

    def attempt(what, step, *args):
        # One failing step must not leave the rest of the sensors running
        try:
            return step(*args)
        except Exception as e:
            problems.append(f"{what}: {e}")
            events.record("server", "error", f"{what}: {e}")

    # Files before ending
    files_old = attempt("camera file list", camera.get_camera_files_list)

    problems += control_points.end_recording()     # a point still being measured is finished by the stop
    points = control_points.points()

    attempt("stop camera", camera.stop_recording)
    attempt("stop GNSS", gnss.stop_logging)
    attempt("stop IMU", imu.stop_logging)

    # Files recorded
    files_after = attempt("camera file list", camera.get_camera_files_list)
    if isinstance(files_old, (list, tuple, set)) and isinstance(files_after, (list, tuple, set)):
        files_new = list(set(files_after) - set(files_old))
    else:
        files_new = []
        problems.append("camera files could not be listed, meta.json has none")

    # What happened to the sensors during the measurement: a lost sensor is a gap in its data
    during = [e for e in events.recent(50) if e["ts_ns"] >= _measurement_start_ns]
    for event in during:
        if event["kind"] == "lost":
            problems.append(f"{event['source']} was lost during the measurement, its data has a gap")
    problems = list(dict.fromkeys(problems))   # the same sentence once

    def write_meta():
        meta_path = os.path.join(_measurement_path, "meta.json")
        with open(meta_path, "r") as f:
            metadata = json.load(f)
        metadata["camera_files"] = files_new
        metadata["control_points"] = len(points)
        metadata["problems"] = problems
        metadata["events"] = during
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

    attempt("meta.json", write_meta)
    attempt("events", events.stop_recording)

    _measurement = False
    _measurement_name = None
    return problems

def download_project_data(project_name, cleanup=False):
    """Downloads data into the USB drive
    Parameters:
        project_name (string): Project name

    Returns:
        bool: Success status
    """

    # Check if project exists
    projects = get_projects()

    if type(projects) == bool or project_name not in projects:
        return 1
    
    # Get the paths
    local_path = get_local_path(project_name)
    usb_path = get_usb_path(project_name)

    # USB not connected
    if not usb_path:
        return 2

    # The project may have been recorded on the stick already: then there is nothing to copy, and
    # nothing to clean up afterwards (the stick holds the only copy)
    on_usb = os.path.realpath(local_path) == os.path.realpath(usb_path)

    # Copy local data to USB
    if not on_usb:
        try:
            shutil.copytree(local_path, usb_path, dirs_exist_ok=True)
        except Exception as e:
            print(f"Copy failed: {e}")
            return 3

    # Get camera file names from meta.json
    meta_path = os.path.join(local_path, "meta.json")
    with open(meta_path, "r") as file:
        metadata = json.load(file)
    camera_files = metadata.get("camera_files", [])

    # Download camera data
    cam_status = camera.download_files(usb_path, camera_files)

    # Camera download failed
    if not cam_status:
        return 4
    
    if cleanup:
        # Delete camera files for this project (nothing to delete counts as success)
        cam_del_status = True
        for file in camera_files:
            cam_del_status = camera.delete_file(file)
        
        # Delete the local files after transfer, never the copy on the stick
        if not on_usb:
            shutil.rmtree(local_path, ignore_errors=True)

        # Cleanup failed
        if not cam_del_status:
            return 5
    
    return True

def get_projects():
    """The projects on the stick and on the local disk, False if none of the folders can be listed"""
    projects, listed = [], False
    for root in storage.roots():
        try:
            names = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
        except Exception as e:
            print(f"Directory list failed: {e}")
            continue
        listed = True
        projects += [n for n in names if n not in projects]

    return projects if listed else False

def get_project_files(project_name):
    projects = get_projects()

    if type(projects) == bool:
        return 1

    if project_name not in projects:
        return 2

    local_path = storage.find_project_path(project_name)

    try:
        project_files = os.listdir(local_path)
    except Exception as e:
        print(f"Directory list failed: {e}")
        return 3

    return project_files

def delete_project_files(project_name):
    projects = get_projects()

    if type(projects) == bool:
        return 1

    if project_name not in projects:
        return 2

    local_path = storage.find_project_path(project_name)

    try:
        shutil.rmtree(local_path)
    except Exception as e:
        print(f"Directory delete failed: {e}")
        return 3

    return True


def get_local_path(project_name):
    """The folder of the project: where it is, or a new one on the stick (else the local disk)"""
    local_path = storage.find_project_path(project_name)

    if not local_path:
        local_path = os.path.join(storage.data_root(), project_name)
        os.makedirs(local_path)

    return local_path

def get_usb_path(project_name):
    """The project folder on the stick with the most room, False if no stick is mounted"""
    roots = storage.usb_roots()

    if not roots:
        return False

    project_path = os.path.join(max(roots, key=storage.free_bytes), project_name)

    if not os.path.exists(project_path):
        os.makedirs(project_path)

    return project_path
