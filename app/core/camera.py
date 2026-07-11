from sdk import camerasdk
import time
import os
import re
from datetime import datetime

# The global camera object lives here
_camera = None

def open_camera():
    # Global camera connection lives while the server lives
    global _camera
    
    for _ in range(5):
        # This might need to be changed to match the sdk
        discovery = camerasdk.DeviceDiscovery()
        devices = discovery.get_available_devices()

        if len(devices) == 0:
            print("No cameras found, retrying in 5 seconds")
            time.sleep(5)
            continue

        _camera = camerasdk.Camera(devices[0].info)
        _camera.open()

        # Break out of the loop if we found the camera
        if _camera:
            break
    
    if not _camera:
        raise(RuntimeError("No camera found, shutting down."))

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

    if not verify_connection():
        return False
    
    _camera.close()
    _camera = None
    return True

def start_recording():
    """
    Starts recording and returns the actual t0 in nanoseconds,
    parsed from the camera's own NORMAL_CAPTURE log line.
    """
    if not verify_connection():
        return False

    # Redirect stderr to capture SDK logs
    LOG_FILE = "sdk_capture.log"
    log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    old_stderr = os.dup(2)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    _camera.start_recording()

    # Poll the log file until NORMAL_CAPTURE appears (or timeout)
    pattern = re.compile(r'\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\].*NORMAL_CAPTURE')
    deadline = time.time() + 3.0  # 3s timeout
    t0_ns = None

    while time.time() < deadline:
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

    # Restore stderr
    os.dup2(old_stderr, 2)
    os.close(old_stderr)
    os.remove(LOG_FILE)

    if not t0_ns:
        raise RuntimeError("Timed out waiting for NORMAL_CAPTURE confirmation")

    return t0_ns

def stop_recording():
    if not verify_connection():
        return False
    
    _camera.stop_recording()
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