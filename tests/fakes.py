# A simulated I2C bus with a LIS3DH and a ZED-F9P on it, so the sensor code runs without hardware
import ctypes
import struct
import threading

from smbus2 import i2c_msg


class FakeBus:
    """Stands in for smbus2.SMBus: i2c_rdwr, write_byte_data, close"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lis_address = 0x18
        self.gnss_address = 0x42
        self.who_am_i = 0x33
        self.registers = {}            # LIS3DH registers written by the driver
        self.samples = []              # LIS3DH output, (x, y, z) 12 bit counts, popped when read
        self.stream = bytearray()      # ZED-F9P output waiting to be read
        self.fail = 0                  # the next N transfers raise OSError (NACK)
        self.absent = set()            # addresses that never answer
        self.closed = False
        self.transfers = 0

    # --- what the sensors do -------------------------------------------------------------------
    def _lis_read(self, register, length):
        reg = register & 0x7F
        if reg == 0x0F:
            return bytes([self.who_am_i])
        if reg == 0x27:  # status + OUT_X_L..OUT_Z_H
            if self.samples:
                x, y, z = self.samples.pop(0)
                return bytes([0x08]) + struct.pack("<hhh", x << 4, y << 4, z << 4)[:length - 1]
            return bytes([0x00]) + bytes(6)[:length - 1]
        return bytes(length)

    def _gnss_read_register(self, register, length):
        if register == 0xFD:
            return struct.pack(">H", len(self.stream))[:length]
        return b"\xff" * length

    def _gnss_read_stream(self, length):
        data = bytes(self.stream[:length])
        del self.stream[:length]
        return data + b"\xff" * (length - len(data))  # a read past the end returns filler

    # --- SMBus interface -----------------------------------------------------------------------
    def i2c_rdwr(self, *msgs):
        with self.lock:
            self.transfers += 1
            if self.fail:
                self.fail -= 1
                raise OSError(121, "Remote I/O error")
            address = msgs[0].addr
            if address in self.absent or address not in (self.lis_address, self.gnss_address):
                raise OSError(121, "Remote I/O error")
            if len(msgs) == 2:
                register, read = msgs[0].buf[0][0], msgs[1]
                length = read.len
                data = (self._lis_read(register, length) if address == self.lis_address
                        else self._gnss_read_register(register, length))
            else:
                read = msgs[0]
                length = read.len
                data = self._gnss_read_stream(length)
            ctypes.memmove(read.buf, data, length)

    def write_byte_data(self, address, register, value):
        with self.lock:
            self.transfers += 1
            if self.fail:
                self.fail -= 1
                raise OSError(121, "Remote I/O error")
            if address in self.absent:
                raise OSError(121, "Remote I/O error")
            self.registers[register] = value

    def close(self):
        self.closed = True


def nmea(body: str) -> bytes:
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"${body}*{calc:02X}\r\n".encode()


def ubx(cls: int, ident: int, payload: bytes = b"") -> bytes:
    body = bytes([cls, ident]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])
