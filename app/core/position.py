# In-process position stream: the GNSS feed publishes, WebSocket clients consume
import asyncio
import logging
import queue
import threading
import time
from typing import Optional

from pydantic import ValidationError

from app.core import gnss
from app.core.nmea import NO_FIX, NmeaPosition
from shared.models import Position

log = logging.getLogger(__name__)


class PositionHub:
    """Fan-out of position updates to async subscribers.

    publish() is thread-safe so the threaded GNSS feed can call it directly.
    Each subscriber gets a small queue; when it is full the oldest position is
    dropped, so a slow client always sees the freshest data. `None` in the
    queue means "the receiver lost its fix".
    """

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: set[asyncio.Queue] = set()
        self.latest: Optional[Position] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def publish(self, position: Optional[Position]):
        """A new position, or None when the fix is lost"""
        self.latest = position

        loop = self._loop
        if loop is None or loop.is_closed():
            return

        loop.call_soon_threadsafe(self._fan_out, position)

    def _fan_out(self, position: Optional[Position]):
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(position)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=16)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)


hub = PositionHub()


class GnssFeed:
    """Reads the NMEA frames of the GNSS receiver, turns them into positions and publishes them to
    the hub. The GNSS reader keeps its subscribers across a reconnect, so this runs for the whole
    life of the server"""

    def __init__(self, hub: PositionHub = hub):
        self._hub = hub
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.positions = 0
        self.no_fix = True   # the last thing said: nothing yet counts as no fix
        self.last_monotonic = 0.0   # when the last position arrived
        self.last_position = None
        self._listeners = []

    def add_listener(self, callback):
        """callback(Position) runs in the feed thread for every position (keep it quick)"""
        self._listeners.append(callback)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="position-feed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        parser = NmeaPosition()
        q = gnss.subscribe(kinds=[gnss.NMEA], maxsize=200)
        try:
            while not self._stop.is_set():
                try:
                    _, _, sentence = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    result = parser.feed(sentence)
                except (ValidationError, ValueError, IndexError) as e:  # a sentence the receiver garbled
                    log.warning("ignored an NMEA sentence (%s): %r", e, sentence[:60])
                    continue
                if result is NO_FIX:
                    if not self.no_fix:
                        self.no_fix = True
                        self._hub.publish(None)
                elif result is not None:
                    self.no_fix = False
                    self.last_monotonic, self.last_position = time.monotonic(), result
                    self.positions += 1
                    self._hub.publish(result)
                    for listener in self._listeners:
                        try:
                            listener(result)
                        except Exception:
                            log.exception("position listener failed")
        finally:
            gnss.unsubscribe(q)


feed = GnssFeed()


async def run_simulator(rate_hz: float = 1.0):
    """Development only: publishes a slowly drifting FAKE position.

    Enabled with POSITION_SIMULATOR=1. fix_type is 'simulated' so consumers
    can tell it from real data.
    """
    lat0, lon0 = 50.0755, 14.4378
    step = 0

    while True:
        step += 1
        hub.publish(Position(
            latitude=lat0 + 0.00001 * step,
            longitude=lon0 + 0.00001 * step,
            altitude_m=200.0,
            timestamp_ns=time.time_ns(),
            horizontal_accuracy_m=1.5,
            fix_type="simulated",
            satellites=12,
        ))
        await asyncio.sleep(1 / rate_hz)
