# Position: forwards the FastAPI position stream to a NOTIFY characteristic
import logging

from gatt.protocol import messages, uuids
from gatt.services.device import Notifier

log = logging.getLogger(__name__)


class PositionService:
    def __init__(self, notifier: Notifier):
        self._notifier = notifier
        self._have_position = False
        self._gnss_ok = True
        self._last_error = None

    async def start(self):
        """Gives the characteristic a readable value before the first fix arrives."""
        await self._publish_error(messages.NO_POSITION_FIX, "no position received yet")

    async def on_position(self, position: dict):
        """position is the Position model as a dict, straight from the API stream."""
        self._have_position = True
        self._last_error = None
        await self._notifier.publish(uuids.POSITION_CHAR_UUID, messages.dumps(position))

    async def on_no_fix(self):
        """The receiver is there but lost its fix: the last position is stale and must not be shown as current"""
        self._have_position = False
        await self._publish_error(messages.NO_POSITION_FIX, "the receiver has no fix")

    async def on_stream_error(self, code: str, message: str):
        """The stream to the API is down. Reported once per outage."""
        await self._publish_error(code, message)

    async def on_gnss_state(self, gnss_ok: bool):
        """Fed from the status poll. Only matters while we have no position at all."""
        self._gnss_ok = gnss_ok
        if self._have_position:
            return

        if gnss_ok:
            await self._publish_error(messages.NO_POSITION_FIX, "no position received yet")
        else:
            await self._publish_error(messages.GNSS_UNAVAILABLE, "GNSS receiver is not connected")

    async def _publish_error(self, code: str, message: str):
        if code == self._last_error:
            return
        self._last_error = code

        payload = messages.dumps({"error": {"code": code, "message": message}})
        await self._notifier.publish(uuids.POSITION_CHAR_UUID, payload)
