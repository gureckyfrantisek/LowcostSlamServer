# Splits a message over several BLE packets and puts it back together.
#
# Every packet is: 1 header byte + up to chunk_size payload bytes.
# Header bits: 0x01 = first packet of a message, 0x02 = last packet.
# So a message that fits in one packet has header 0x03, a middle packet 0x00.
from typing import Optional

FLAG_FIRST = 0x01
FLAG_LAST = 0x02

# Hard cap so a broken client cannot make us buffer forever
MAX_MESSAGE_SIZE = 64 * 1024


class FramingError(Exception):
    pass


def encode(payload: bytes, chunk_size: int) -> list[bytes]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    if not payload:
        return [bytes([FLAG_FIRST | FLAG_LAST])]

    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
    frames = []

    for index, chunk in enumerate(chunks):
        flags = 0
        if index == 0:
            flags |= FLAG_FIRST
        if index == len(chunks) - 1:
            flags |= FLAG_LAST
        frames.append(bytes([flags]) + chunk)

    return frames


class Reassembler:
    def __init__(self, max_size: int = MAX_MESSAGE_SIZE):
        self._max_size = max_size
        self._buffer: Optional[bytearray] = None

    def reset(self):
        self._buffer = None

    def feed(self, frame: bytes) -> Optional[bytes]:
        """Returns the whole message once its last packet arrives, else None."""
        if not frame:
            raise FramingError("empty packet")

        flags = frame[0]
        if flags & ~(FLAG_FIRST | FLAG_LAST):
            raise FramingError(f"unknown header flags 0x{flags:02x}")

        if flags & FLAG_FIRST:
            self._buffer = bytearray()
        elif self._buffer is None:
            raise FramingError("continuation packet without a first packet")

        self._buffer += frame[1:]

        if len(self._buffer) > self._max_size:
            self._buffer = None
            raise FramingError("message too large")

        if flags & FLAG_LAST:
            message = bytes(self._buffer)
            self._buffer = None
            return message

        return None
