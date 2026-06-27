# Shared functions for all the sensors
import os
import time
import json
import shutil
from dataclasses import dataclass
from typing import Optional
from app.core import camera, config, gnss, imu

# Only a shortcut to if we're recording or not
_measurement = False

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


def start_measurement(project_name):
    """Starts video capture and GNSS and IMU logging
    Returns:
        Measurement: The measurement object
    """
    global _measurement

    # Return if already recording
    if _measurement:
        return 1

    _measurement = True

    # Name the measurement so that the files are consistent too
    local_path = get_local_path(project_name)

    # Get files before for the diff later
    files_old = camera.get_camera_files_list()

    # Start IMU and GNSS immediately
    imu_start_ns = imu.start_logging(local_path)
    gnss_start_ns = gnss.start_logging(local_path)

    # Start recording
    if not camera.start_recording():
        return 2

    # Get time anchor
    t0_ns = now_ns()
    media_time = camera.get_media_time()

    # Files recorded
    files_new = list(set(camera.get_camera_files_list()) - set(files_old))

    # Write metadata file for postprocessing
    metadata = {
        "name": project_name,
        "t0_ns": t0_ns,
        "camera_media_time_at_t0_ms": media_time,
        "gnss_start_offset_ns": imu_start_ns - t0_ns,
        "imu_start_offset_ns": gnss_start_ns - t0_ns,
        "camera_files": files_new
    }

    meta_path = os.path.join(local_path, "meta.json")

    with open(meta_path, "w") as file:
        json.dump(metadata, file, indent=2)

    return True

def stop_measurement():
    global _measurement
    if not _measurement:
        return False

    camera.stop_recording()
    gnss.stop_logging()
    imu.stop_logging()

    _measurement = None
    return True

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

    # Copy local data to USB
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
        cam_del_status = camera.delete_all()
        shutil.rmtree(local_path, ignore_errors=True)

        # Cleanup failed
        if not cam_del_status:
            return 5
    
    return True

def get_projects():
    base_path = config.LOCAL_DATA_PATH
    try:
        projects = os.listdir(base_path)
    except Exception as e:
        print(f"Directory list failed: {e}")
        return False

    return projects

def get_project_files(project_name):
    projects = get_projects()

    if type(projects) == bool:
        return 1

    if project_name not in projects:
        return 2

    base_path = config.LOCAL_DATA_PATH

    local_path = os.path.join(base_path, project_name)

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

    base_path = config.LOCAL_DATA_PATH

    local_path = os.path.join(base_path, project_name)

    try:
        shutil.rmtree(local_path)
    except Exception as e:
        print(f"Directory delete failed: {e}")
        return 3

    return


def get_local_path(project_name):
    base_path = config.LOCAL_DATA_PATH
    
    local_path = os.path.join(base_path, project_name)

    if not os.path.exists(local_path):
        os.makedirs(local_path)
    
    return local_path

def get_usb_path(project_name):
    base_path = config.BASE_USB_PATH
    devices = os.listdir(base_path)
    print(f"Found devices: {devices}")
    
    if not devices:
        return False
    
    usb_path = os.path.join(base_path, devices[0])

    project_path = os.path.join(usb_path, project_name)

    if not os.path.exists(project_path):
        os.makedirs(project_path)
    
    return project_path