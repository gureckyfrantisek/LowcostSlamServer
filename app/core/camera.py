from sdk import camerasdk
import time

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

def get_camera():
    """Returns the camera connected"""
    if _camera is None:
        raise RuntimeError("Camera not connected")
    return _camera

def close_camera():
    """Closes (e.g.) disconnects the current camera"""
    # When we write to globals we must state it
    global _camera

    if _camera:
        _camera.close()
        _camera = None

def start_recording():
    if _camera:
        _camera.start_recording()

def stop_recording():
    if _camera:
        _camera.stop_recording()