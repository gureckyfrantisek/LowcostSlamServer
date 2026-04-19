# Shared functions for all the sensors
import os
import time
import json
from dataclasses import dataclass
from typing import Optional
from app.core import camera, config, gnss, imu

@dataclass
class Measurement:
    name: str
    t0_ns: int
    camera_media_time_at_t0_ms: Optional[int] = None
    gnss_start_offset_ns: Optional[int] = None
    imu_start_offset_ns: Optional[int] = None

_measurement: Optional[Measurement] = None

def get_measurement() -> Optional[Measurement]:
    return _measurement

def now_ns() -> int:
    return time.time_ns()


def start_measurement(project_name):
    """Starts video capture and GNSS and IMU logging
    Returns:
        Measurement: The measurement object
    """
    global _measurement

    # Return if already recording
    if _measurement:
        return False

    # Name the measurement so that the files are consistent too
    project_path = get_project_path(project_name)

    if not project_path:
        # No USB drive inserted, cannot create project folder
        return False

    # Start IMU and GNSS immediately
    imu_start_ns = imu.start_logging(project_path)
    # gnss_start_ns = gnss.start_logging(project_path)

    # Start recording
    camera.start_recording()

    # Get time anchor
    t0_ns = now_ns()
    media_time = camera.get_media_time()

    _measurement = Measurement(project_name, t0_ns)
    _measurement.camera_media_time_at_t0_ms = media_time

    # Save the IMU and GNSS offsets
    _measurement.imu_start_offset_ns = imu_start_ns - t0_ns
    # _measurement.gnss_start_offset_ns = gnss_offset - t0_ns

    return _measurement

def stop_measurement():
    global _measurement
    if not _measurement:
        return False

    camera.stop_recording()
    # gnss.stop_logging()
    imu.stop_logging()

    project_path = get_project_path(_measurement.name)

    # Write metadata file for postprocessing
    metadata = {
        "name": _measurement.name,
        "t0_ns": _measurement.t0_ns,
        "camera_media_time_at_t0_ms": _measurement.camera_media_time_at_t0_ms,
        "gnss_start_offset_ns": _measurement.gnss_start_offset_ns,
        "imu_start_offset_ns": _measurement.imu_start_offset_ns,
    }

    meta_path = os.path.join(project_path, "meta.json")

    with open(meta_path, "w") as file:
        json.dump(metadata, file, indent=2)

    _measurement = None
    return True

def download_all_data(project_name, cleanup=False):
    """Downloads data into the USB drive
    Parameters:
        project_name (string): Project name

    Returns:
        bool: Success status
    """
    # Get the path
    project_path = get_project_path(project_name)

    # Download camera data
    cam_status = camera.download_all(project_path)

    if cleanup:
        cam_del_status = camera.delete_all()
    
    return True

def get_project_path(project_name):
    base_path = config.BASE_PATH
    devices = os.listdir(base_path)
    print(f"Found devices: {devices}")
    
    if not devices:
        return False
    
    usb_path = os.path.join(base_path, devices[0])
    usb_path = os.path.join(base_path, '00usbtest')
    project_path = os.path.join(usb_path, project_name)

    if not os.path.exists(project_path):
        os.makedirs(project_path)
    
    return project_path