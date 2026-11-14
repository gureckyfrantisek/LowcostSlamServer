# Real subscription to the FastAPI position stream (WebSocket). No polling.
# The socket is only held open while a BLE client is connected, and it
# reconnects with backoff when the API is down or restarts.
import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets
from pydantic import ValidationError

from gatt.clients import ClientTracker
from gatt.protocol import messages
from shared.models import Position

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 10.0


class PositionStream:
    def __init__(
        self,
        url: str,
        tracker: ClientTracker,
        on_position: Callable[[dict], Awaitable[None]],
        on_error: Callable[[str, str], Awaitable[None]],
        on_no_fix: Callable[[], Awaitable[None]] | None = None,
    ):
        self._url = url
        self._tracker = tracker
        self._on_position = on_position
        self._on_error = on_error
        self._on_no_fix = on_no_fix

    async def run(self):
        backoff = BACKOFF_START
        reported = False

        while True:
            await self._tracker.has_clients.wait()

            session = asyncio.create_task(self._session())
            idle = asyncio.create_task(self._tracker.no_clients.wait())
            await asyncio.wait({session, idle}, return_when=asyncio.FIRST_COMPLETED)

            if idle.done():
                # Last client left, close the stream until someone connects again
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
                backoff, reported = BACKOFF_START, False
                continue

            idle.cancel()
            received_any = session.result()
            if received_any:
                backoff, reported = BACKOFF_START, False

            if not reported:
                reported = True
                await self._on_error(messages.API_UNAVAILABLE, "position stream is not available")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session(self) -> bool:
        """One connection. Returns True if at least one position arrived before it ended."""
        received_any = False

        try:
            async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as socket:
                log.info("Position stream connected")
                async for raw in socket:
                    if self._is_no_fix(raw):
                        received_any = True
                        if self._on_no_fix:
                            await self._on_no_fix()
                        continue
                    position = self._parse(raw)
                    if position is None:
                        continue
                    received_any = True
                    await self._on_position(position)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Position stream ended: %s", e)

        return received_any

    @staticmethod
    def _is_no_fix(raw) -> bool:
        """{"position": null}: the receiver lost its fix"""
        try:
            body = json.loads(raw)
        except ValueError:
            return False
        return isinstance(body, dict) and "position" in body and body["position"] is None

    @staticmethod
    def _parse(raw) -> dict | None:
        try:
            body = json.loads(raw)
            # Validate against the shared model so garbage never reaches the phone
            return Position.model_validate(body["position"]).model_dump(mode="json", exclude_none=True)
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            log.warning("Dropping invalid position message: %s", e)
            return None
