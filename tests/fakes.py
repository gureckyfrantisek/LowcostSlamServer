# A simulated I2C bus with a LIS3DH and a ZED-F9P on it, so the sensor code runs without hardware
import ctypes
import struct
import threading

from smbus2 import i2c_msg


class FakeEdges:
    """Stands in for imu.GpioEdges: edges are pushed by the test, the fd wakes the reader"""

    def __init__(self):
        import os
        self.fd, self._wake = os.pipe()
        self._edges = []
        self.closed = False

    def push(self, line, ns, seqno):
        import os
        from app.core.imu import Edge
        self._edges.append(Edge(line, ns, seqno))
        os.write(self._wake, b"x")

    def read(self):
        import os
        os.read(self.fd, 4096)
        edges, self._edges = self._edges, []
        return edges

    def close(self):
        import os
        self.closed = True
        os.close(self.fd)
        os.close(self._wake)


class FakeBus:
    """Stands in for smbus2.SMBus: i2c_rdwr, write_byte_data, close"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lis_address = 0x18
        self.gnss_address = 0x42
        self.who_am_i = 0x33
        self.registers = {}            # LIS3DH registers written by the driver
        self.fifo = []                 # LIS3DH FIFO, (x, y, z) 12 bit counts, oldest first
        self.overrun = False           # the FIFO was full when a sample arrived
        self.stream = bytearray()      # ZED-F9P output waiting to be read
        self.fail = 0                  # the next N transfers raise OSError (NACK)
        self.absent = set()            # addresses that never answer
        self.closed = False
        self.transfers = 0
        self.valset = None             # settings the receiver was given: {key: value}
        self.ack = "ack"               # how the receiver answers a configuration: "ack", "nak" or "silent"

    # --- what the sensors do -------------------------------------------------------------------
    def produce(self, samples):
        """The sensor makes samples; a full FIFO drops the oldest and flags the overrun"""
        with self.lock:
            for sample in samples:
                if len(self.fifo) >= 32:
                    del self.fifo[0]
                    self.overrun = True
                self.fifo.append(tuple(sample))

    def _lis_read(self, register, length):
        reg = register & 0x7F
        if reg == 0x0F:
            return bytes([self.who_am_i])
        if reg == 0x2F:  # FIFO_SRC
            n = len(self.fifo)
            return bytes([(0x40 if self.overrun and n else 0) | (0x20 if n == 0 else 0) | (n & 0x1F)])
        if reg == 0x28:  # OUT_X_L ... : pops one sample per 6 bytes
            out = b""
            for _ in range(length // 6):
                x, y, z = self.fifo.pop(0) if self.fifo else (0, 0, 0)
                out += struct.pack("<hhh", x << 4, y << 4, z << 4)
            if not self.fifo:
                self.overrun = False
            return out
        return bytes(length)

    def _gnss_read_register(self, register, length):
        if register == 0xFD:
            return struct.pack(">H", len(self.stream))[:length]
        return b"\xff" * length

    def _gnss_write(self, data):
        """A UBX command to the receiver. Only CFG-VALSET is understood"""
        assert data[:2] == bytes([0xB5, 0x62]), "not a UBX frame"
        cls, ident = data[2], data[3]
        length = struct.unpack("<H", data[4:6])[0]
        payload = data[6:6 + length]
        if (cls, ident) == (0x06, 0x8A):
            values, i = {}, 4
            while i < len(payload):
                key = struct.unpack_from("<I", payload, i)[0]
                size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}[key >> 28 & 7]
                values[key] = int.from_bytes(payload[i + 4:i + 4 + size], "little")
                i += 4 + size
            self.valset = values
            if self.ack != "silent":
                self.stream += ubx(0x05, 0x01 if self.ack == "ack" else 0x00, bytes([cls, ident]))

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
            elif msgs[0].flags & 1:
                read = msgs[0]
                length = read.len
                data = self._gnss_read_stream(length)
            else:
                self._gnss_write(bytes(msgs[0]))
                return
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


def trickle(bus, batches, size=8, gap=0.06, first=0):
    """The sensor keeps making samples: `batches` batches of `size` samples, `gap` seconds apart"""
    import time
    for b in range(batches):
        bus.produce([(first + b * size + i, 0, 0) for i in range(size)])
        time.sleep(gap)
