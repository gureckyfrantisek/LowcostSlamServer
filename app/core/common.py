# Shared functions for all the sensors
import os
from app.core.camera import download_all, delete_all

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
    cam_status = download_all(project_path)

    if cleanup:
        cam_del_status = delete_all()
    
    return True

def get_project_path(project_name):
    base_path = "/media/pi"
    base_path = "/home/franta/Downloads"
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