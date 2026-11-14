# BLE transport: owns the bless GATT server and turns characteristic updates into BLE packets.
import asyncio
import logging
from typing import Callable, Optional

from bless import (
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

from gatt.clients import ClientTracker
from gatt.config import Config
from gatt.protocol import uuids
from gatt.protocol.framing import encode

log = logging.getLogger(__name__)

# Pause between packets of one message so the stack keeps them in order
PACKET_GAP_S = 0.01

_P = GATTCharacteristicProperties
_A = GATTAttributePermissions

# characteristic -> (properties, permissions). Nothing needs encryption or pairing: any phone can use it.
CHARACTERISTICS = {
    uuids.COMMAND_CHAR_UUID: (_P.write | _P.write_without_response, _A.writeable),
    uuids.RESPONSE_CHAR_UUID: (_P.read | _P.notify, _A.readable),
    uuids.STATUS_CHAR_UUID: (_P.read | _P.notify, _A.readable),
    uuids.POSITION_CHAR_UUID: (_P.read | _P.notify, _A.readable),
    uuids.INFO_CHAR_UUID: (_P.read, _A.readable),
}


class GattServer:
    def __init__(self, config: Config, tracker: ClientTracker):
        self._config = config
        self._tracker = tracker
        self._server: Optional[BlessServer] = None
        self._read_cache: dict[str, bytes] = {}
        self._locks = {char: asyncio.Lock() for char in CHARACTERISTICS}
        self.on_command_write: Callable[[bytes], None] = lambda data: None

    async def start(self):
        cfg = self._config
        server = BlessServer(name=cfg.device_name, loop=asyncio.get_running_loop())
        server.read_request_func = self._on_read
        server.write_request_func = self._on_write

        await server.add_new_service(cfg.service_uuid)
        for char_uuid, (properties, permissions) in CHARACTERISTICS.items():
            await server.add_new_characteristic(cfg.service_uuid, char_uuid, properties, None, permissions)

        await server.start()
        self._server = server
        log.info("GATT server advertising as '%s' (service %s)", cfg.device_name, cfg.service_uuid)

    async def stop(self):
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def publish(self, char_uuid: str, payload: bytes):
        """Stores payload as the characteristic's readable value and notifies subscribers.

        READ returns the whole payload; NOTIFY delivers it as framed packets.
        """
        self._read_cache[char_uuid] = payload

        if self._server is None or char_uuid == uuids.INFO_CHAR_UUID or not self._tracker.has_clients.is_set():
            return

        async with self._locks[char_uuid]:
            characteristic = self._server.get_characteristic(char_uuid)
            for frame in encode(payload, self._config.chunk_size):
                characteristic.value = bytearray(frame)
                self._server.update_value(self._config.service_uuid, char_uuid)
                await asyncio.sleep(PACKET_GAP_S)

    # bless callbacks run on the event loop thread
    def _on_read(self, characteristic, **kwargs):
        uuid = str(characteristic.uuid).lower()
        return bytearray(self._read_cache.get(uuid, b""))

    def _on_write(self, characteristic, value, **kwargs):
        if str(characteristic.uuid).lower() == uuids.COMMAND_CHAR_UUID:
            self.on_command_write(bytes(value))
