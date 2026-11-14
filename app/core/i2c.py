# The I2C buses of the IMU and the GNSS receiver. They sit on separate buses of the board header
# (a shared bus makes the GNSS reads delay the accelerometer reads), but a setup with both on one
# bus works too: opening the same bus twice returns the same handle.
import threading
from smbus2 import SMBus, i2c_msg

_buses: dict[str, "Bus"] = {}
_buses_lock = threading.Lock()


class Bus:
    """One I2C device file, shared by everything that talks to devices on it"""

    def __init__(self, path):
        self.path = path
        self._smbus = SMBus(path)
        self._users = 0
        # One transfer at a time: the target address is kept in the SMBus file descriptor, so two
        # threads must not interleave. The reader threads of the sensors take turns here.
        self._lock = threading.Lock()

    def read_registers(self, address: int, register: int, length: int) -> bytes:
        """Writes the register address, then reads `length` bytes (repeated start, one transfer)"""
        write, read = i2c_msg.write(address, [register]), i2c_msg.read(address, length)
        with self._lock:
            self._smbus.i2c_rdwr(write, read)
        return bytes(read)

    def write_register(self, address: int, register: int, value: int):
        with self._lock:
            self._smbus.write_byte_data(address, register, value)

    def read_stream(self, address: int, length: int) -> bytes:
        """Plain read without a register address (the data stream of a u-blox receiver)"""
        read = i2c_msg.read(address, length)
        with self._lock:
            self._smbus.i2c_rdwr(read)
        return bytes(read)

    def close(self):
        self._smbus.close()


def acquire(path) -> Bus:
    """Opens the bus for the first user, every acquire needs a release of the same bus"""
    key = str(path)
    with _buses_lock:
        bus = _buses.get(key)
        if bus is None:
            bus = _buses[key] = Bus(path)
        bus._users += 1
        return bus


def release(bus: Bus):
    """Closes the bus when the last user is gone"""
    with _buses_lock:
        if bus._users == 0:
            return
        bus._users -= 1
        if bus._users == 0:
            _buses.pop(str(bus.path), None)
            bus.close()


def open_buses() -> int:
    """How many buses are open (for tests and status)"""
    with _buses_lock:
        return len(_buses)
