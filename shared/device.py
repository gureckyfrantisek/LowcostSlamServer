# Who this board is. Both the API and the GATT process need it, so it lives here.
import hashlib
import os
import socket


def device_id() -> str:
    """Four hex characters that stay the same on this board: the app tells several boards apart by them"""
    override = os.getenv("DEVICE_ID")
    if override:
        return override
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                text = f.read().strip()
            if text:
                return text[:4].upper()
        except OSError:
            continue
    return hashlib.sha1(socket.gethostname().encode()).hexdigest()[:4].upper()


def device_name() -> str:
    """The Bluetooth name. Short on purpose: flags (3 bytes), the 128-bit service UUID (18) and the name
    (2 + 8) fill the 31 bytes of a BLE advertisement"""
    return os.getenv("DEVICE_NAME") or f"SLAM-{device_id()[:3]}"
