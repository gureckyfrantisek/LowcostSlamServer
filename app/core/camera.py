from sdk import camerasdk
import time
import os

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
    if not verify_connection():
        return False
    
    _camera.start_recording()
    return True

def stop_recording():
    if not verify_connection():
        return False
    
    _camera.stop_recording()
    return True

def get_camera_files_list():
    if not verify_connection():
        return False
    
    return _camera.get_camera_files_list()

def download_file(file, local_file, progress_callback):
    if not verify_connection():
        return False
    
    _camera.download_file(file, local_file, progress_callback)
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

def get_media_time():
    if not verify_connection():
        return False
    
    return _camera.get_media_time()

# Helpers
def progress(current, total):
    """Callback function for the download progress"""
    percent = (current / total) * 100
    print(f"Downloading file: {round(percent, 3)} %")
