# Where the recordings go: a USB stick when one is mounted, else the local disk, and how much room is left
import logging
import os
import shutil

from app.core import config

log = logging.getLogger(__name__)

APP_FOLDER = "LowcostSlam"  # on a stick, so the stick stays usable for other things

# What one hour of recording takes, measured on the Radxa (docs/timing.md): imu.csv is 44 bytes per
# sample (48 here, the timestamps get longer digits), gnss.ubx plus gnss_index.csv 1.6 KB per second at 1 Hz.
# The video stays on the camera card and is not counted.
IMU_BYTES_PER_SAMPLE = 48
GNSS_BYTES_PER_S_PER_HZ = 1600

MOUNTS_FILE = "/proc/mounts"
SYSFS = "/sys"

WARN_HOURS = 3.0  # warn when less than this many hours of recording fit
FULL_HOURS = 1 / 6  # refuse to start with less than ten minutes of room


def bytes_per_hour() -> int:
    """What one hour of IMU and GNSS logging takes at the configured rates"""
    return round(3600 * (config.IMU_I2C_HZ * IMU_BYTES_PER_SAMPLE + config.GNSS_RATE_HZ * GNSS_BYTES_PER_S_PER_HZ))


def parse_mounts(text: str) -> list[tuple[str, str]]:
    """(device, mount point) of every line of /proc/mounts. Spaces in a path are written as \\040"""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1].replace("\\040", " ")))
    return out


def is_usb_device(device: str, sysfs: str | None = None) -> bool:
    """A block device (/dev/sda1) that hangs off a USB port"""
    if not device.startswith("/dev/"):
        return False
    path = os.path.realpath(os.path.join(sysfs or SYSFS, "class", "block", os.path.basename(device)))
    return "/usb" in path


def usb_mounts(mounts_file: str | None = None, sysfs: str | None = None) -> list[str]:
    """Mount points of the USB sticks: file systems on USB block devices, and anything mounted under
    BASE_USB_PATH (where the udev rule of deploy/ mounts them)"""
    try:
        with open(mounts_file or MOUNTS_FILE) as f:
            mounts = parse_mounts(f.read())
    except OSError:
        return []
    base = os.path.abspath(config.BASE_USB_PATH)
    found = []
    for device, point in mounts:
        under_base = os.path.dirname(os.path.abspath(point)) == base
        if (under_base or is_usb_device(device, sysfs)) and point not in found:
            found.append(point)
    return found


def _writable(path: str) -> bool:
    """The folder can be created and written to (a read-only or unplugged stick fails here)"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as f:
            f.write("x")
            f.flush()
            os.fsync(f.fileno())
        os.remove(probe)
        return True
    except OSError:
        return False


def free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def level(free: int) -> str:
    """ok, low (less than WARN_HOURS of recording fit) or full (less than FULL_HOURS)"""
    hours = free / bytes_per_hour()
    return "full" if hours < FULL_HOURS else "low" if hours < WARN_HOURS else "ok"


def usb_roots() -> list[str]:
    """The recording folders on the mounted sticks that can be written to"""
    return [os.path.join(m, APP_FOLDER) for m in usb_mounts() if _writable(os.path.join(m, APP_FOLDER))]


def local_root() -> str:
    return config.LOCAL_DATA_PATH


def data_root() -> str:
    """The folder a new recording goes to: the stick with the most room that has at least the
    minimum, else the local disk"""
    sticks = [r for r in usb_roots() if level(free_bytes(r)) != "full"]
    if sticks:
        return max(sticks, key=free_bytes)
    return local_root()


def roots() -> list[str]:
    """Every folder that can hold projects, the sticks first"""
    return usb_roots() + [local_root()]


def find_project_path(name: str) -> str | None:
    """The folder of an existing project, wherever it is"""
    for root in roots():
        path = os.path.join(root, name)
        if os.path.isdir(path):
            return path
    return None


def valid_project_name(name: str) -> bool:
    """A single folder name: no path separators, no dots-only names, no hidden folder"""
    return (bool(name) and len(name) <= 100 and name not in (".", "..") and not name.startswith(".")
            and not any(c in name for c in '/\\\0') and name == name.strip())


def status() -> dict:
    """Where recordings go, how much room is left and whether that is a problem"""
    root = data_root()
    free = free_bytes(root if os.path.isdir(root) else os.path.dirname(root) or "/")
    hour = bytes_per_hour()
    lvl = level(free)
    sticks = usb_mounts()
    out = {
        "location": "usb" if root in usb_roots() else "local",
        "path": root,
        "usb_present": bool(sticks),
        "free_mb": round(free / 1e6),
        "hour_mb": round(hour / 1e6, 1),
        "hours_left": round(free / hour, 1),
        "level": lvl,
    }
    if lvl != "ok":
        out["message"] = (f"only {out['hours_left']} h of recording fit on the {out['location']} storage"
                          f" ({out['free_mb']} MB free, one hour takes {out['hour_mb']} MB)")
        if out["location"] == "local" and sticks:
            out["message"] += "; the USB stick is full or read-only"
    return out
