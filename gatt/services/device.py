# Device service: Command (write) -> FastAPI -> Response (notify), plus Status (notify)
import asyncio
import logging
from typing import Optional, Protocol

from gatt.api_client.client import ApiClient, ApiError
from gatt.protocol import messages, uuids
from gatt.protocol.commands import COMMANDS
from gatt.protocol.framing import FramingError, Reassembler, MAX_MESSAGE_SIZE
from gatt.protocol.messages import ProtocolError

log = logging.getLogger(__name__)

# Commands waiting for the API (they run one at a time, the camera SDK is not re-entrant)
MAX_PENDING = 16


class Notifier(Protocol):
    async def publish(self, char_uuid: str, payload: bytes) -> None: ...


class DeviceService:
    def __init__(self, api: ApiClient, notifier: Notifier):
        self._api = api
        self._notifier = notifier
        self._reassembler = Reassembler()
        self._api_lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()
        self._sensors: dict = {}

    def reset(self):
        """Drops a half-received command, e.g. when the client disconnected mid-write."""
        self._reassembler.reset()

    def on_command_write(self, data: bytes):
        """Called by the BLE layer for every packet written to the Command characteristic."""
        try:
            raw = self._reassembler.feed(data)
        except FramingError as e:
            self._spawn(self._reply(messages.error_response(None, messages.MALFORMED_REQUEST, str(e))))
            return

        if raw is None:
            return

        if len(self._pending) >= MAX_PENDING:
            self._spawn(self._reply(messages.error_response(
                messages.peek_id(raw), messages.BUSY, "too many commands in progress")))
            return

        self._spawn(self._handle(raw))

    async def publish_status(self, status: dict):
        sensors = status.get("sensors")
        if isinstance(sensors, dict):
            self._sensors = sensors

        await self._notifier.publish(uuids.STATUS_CHAR_UUID, messages.dumps(status))

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _reply(self, payload: bytes):
        if len(payload) > MAX_MESSAGE_SIZE:
            # Cannot happen for the small error envelopes, but never send a runaway message
            payload = messages.error_response(None, messages.RESPONSE_TOO_LARGE, "response too large")
        await self._notifier.publish(uuids.RESPONSE_CHAR_UUID, payload)

    async def _handle(self, raw: bytes):
        request_id = messages.peek_id(raw)

        try:
            request = messages.parse_request(raw)
            request_id = request.id

            spec = COMMANDS.get(request.cmd)
            if spec is None:
                raise ProtocolError(messages.UNKNOWN_COMMAND, f"unknown command '{request.cmd}'")

            async with self._api_lock:
                result = await self._api.call(spec, request.params)

            payload = messages.ok_response(request_id, result.http_status, result.data)
            if len(payload) > MAX_MESSAGE_SIZE:
                payload = messages.error_response(
                    request_id, messages.RESPONSE_TOO_LARGE, "response does not fit in a BLE message")

        except ProtocolError as e:
            payload = messages.error_response(request_id, e.code, e.message)

        except ApiError as e:
            code = e.code
            if code == messages.SERVER_ERROR and self._missing_sensor(spec):
                code = messages.DEVICE_UNAVAILABLE
            payload = messages.error_response(request_id, code, e.message, e.http_status)

        except Exception:
            log.exception("command failed")
            payload = messages.error_response(request_id, messages.INTERNAL_ERROR, "internal error")

        await self._reply(payload)

    def _missing_sensor(self, spec) -> Optional[str]:
        """Sensor this command needs that the last status poll reported as not connected."""
        for sensor in spec.sensors:
            if self._sensors.get(sensor) not in (None, "ok"):
                return sensor
        return None
