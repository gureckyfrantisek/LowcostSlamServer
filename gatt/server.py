# BLE transport: owns the bless GATT server and turns characteristic updates into BLE packets.
import asyncio
import logging
from typing import Callable, Optional

from bless import (
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
    GATTDescriptorProperties,
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

# What a phone shows next to each characteristic (the Characteristic User Description, 0x2901): a generic
# BLE explorer lists custom UUIDs as "Unknown Characteristic", with this the app can tell them apart.
USER_DESCRIPTION_UUID = "00002901-0000-1000-8000-00805f9b34fb"
DESCRIPTIONS = {
    uuids.COMMAND_CHAR_UUID: "Command (write)",
    uuids.RESPONSE_CHAR_UUID: "Response (notify)",
    uuids.STATUS_CHAR_UUID: "Status (notify)",
    uuids.POSITION_CHAR_UUID: "Position (notify)",
    uuids.INFO_CHAR_UUID: "Info (read)",
}

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
        """Starts advertising. The names of the characteristics are a nicety: if BlueZ refuses them the
        server starts without"""
        try:
            await self._start(with_descriptions=True)
        except Exception as e:
            log.warning("Cannot start with the characteristic names (%s), trying without", e)
            await self._start(with_descriptions=False)

    async def _start(self, with_descriptions: bool):
        cfg = self._config
        server = BlessServer(name=cfg.device_name, loop=asyncio.get_running_loop())
        server.read_request_func = self._on_read
        server.write_request_func = self._on_write

        await server.add_new_service(cfg.service_uuid)
        for char_uuid, (properties, permissions) in CHARACTERISTICS.items():
            await server.add_new_characteristic(cfg.service_uuid, char_uuid, properties, None, permissions)
            if with_descriptions:
                await server.add_new_descriptor(cfg.service_uuid, char_uuid, USER_DESCRIPTION_UUID,
                                                GATTDescriptorProperties.read,
                                                bytearray(DESCRIPTIONS[char_uuid].encode()),
                                                GATTAttributePermissions.readable)

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
        data = self._read_cache.get(uuid, b"")
        # Reads are rare (the phone asks), so every one is logged: it shows what was answered
        log.info("READ %s: %d bytes %s", DESCRIPTIONS.get(uuid, uuid), len(data), data[:60])
        return bytearray(data)

    def _on_write(self, characteristic, value, **kwargs):
        if str(characteristic.uuid).lower() == uuids.COMMAND_CHAR_UUID:
            self.on_command_write(bytes(value))
