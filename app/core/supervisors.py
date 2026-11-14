# The three devices of the server and what keeps each of them connected
from app.core import camera, gnss, imu
from app.core.supervisor import Supervisor

_supervisors: dict[str, Supervisor] = {}
SCALE = 1.0  # every wait is multiplied by this (the tests make it small)


def start_all(wait_s: float = 5.0):
    """Starts keeping every device connected. Returns as soon as each has had its first attempt (at most
    `wait_s` for all), so a status right after the start is true. The server starts without any device."""
    stop_all()
    # The lambdas look the functions up when called, not now
    _supervisors["camera"] = Supervisor("camera", lambda: camera.open_camera(), lambda: camera.close_camera(),
                                        lambda: camera.healthy(), check_s=2.0 * SCALE, retry_s=1.0 * SCALE,
                                        max_retry_s=5.0 * SCALE)
    _supervisors["gnss"] = Supervisor("gnss", lambda: gnss.open_gnss_port(), lambda: gnss.close_gnss_device(),
                                      lambda: gnss.verify_connection(), check_s=1.0 * SCALE, retry_s=1.0 * SCALE,
                                      max_retry_s=5.0 * SCALE)
    _supervisors["imu"] = Supervisor("imu", lambda: imu.open_imu_port(), lambda: imu.close_imu_device(),
                                     lambda: imu.verify_connection(), check_s=0.5 * SCALE, retry_s=1.0 * SCALE,
                                     max_retry_s=5.0 * SCALE)
    for supervisor in _supervisors.values():
        supervisor.start()
    for supervisor in _supervisors.values():
        supervisor.wait_first_attempt(wait_s)


def stop_all():
    """Stops the supervisors, then closes the devices (a recording, if one still runs, is stopped too)"""
    for supervisor in _supervisors.values():
        supervisor.stop()
    _supervisors.clear()
    imu.close_imu_port()
    gnss.close_gnss_port()
    camera.close_camera()


def get(name: str) -> Supervisor | None:
    return _supervisors.get(name)
