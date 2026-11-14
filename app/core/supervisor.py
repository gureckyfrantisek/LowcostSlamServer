# Keeps a device connected: opens it in the background, and closes and reopens it when it stops
# answering. The server starts without any device, and a device that is plugged in later, or pulled out
# and plugged in again, comes back by itself.
import logging
import threading
import time
from typing import Callable

from app.core import events

log = logging.getLogger(__name__)


class Supervisor:
    """Runs open_fn until it works, then checks healthy_fn every `check_s`; when that fails close_fn
    is called and the device is opened again. open_fn raises when the device is not there."""

    def __init__(self, name: str, open_fn: Callable, close_fn: Callable, healthy_fn: Callable[[], bool],
                 check_s: float = 1.0, retry_s: float = 1.0, max_retry_s: float = 5.0):
        self.name = name
        self._open, self._close, self._healthy = open_fn, close_fn, healthy_fn
        self._check_s, self._retry_s, self._max_retry_s = check_s, retry_s, max_retry_s
        self._stop = threading.Event()
        self._first = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "starting"   # starting, ok, missing (not there or lost, trying again)
        self.error: str | None = None
        self.since = time.time()
        self.opened = 0           # how many times it was opened
        self._was_ok = False

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"supervisor-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
            self._thread = None
        try:
            self._close()
        except Exception:
            log.exception("closing %s failed", self.name)
        self._set("stopped")

    def wait_first_attempt(self, timeout: float) -> bool:
        """Waits until the first open attempt is over (so a status right after the start is true)"""
        return self._first.wait(timeout)

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def _set(self, state: str, error: str | None = None):
        if state != self.state:
            self.since = time.time()
        self.state, self.error = state, error

    def _run(self):
        delay = self._retry_s
        while not self._stop.is_set():
            if self.state != "ok":
                try:
                    self._open()
                except Exception as e:  # not there, busy, wrong chip, SDK error: all mean "try again"
                    if self.state != "missing" or self.error != str(e):
                        log.warning("%s not available: %s", self.name, e)
                    self._set("missing", str(e) or type(e).__name__)
                    self._first.set()
                    self._stop.wait(delay)
                    delay = min(delay * 2, self._max_retry_s)
                    continue
                self.opened += 1
                delay = self._retry_s
                if self._was_ok or self.opened > 1:
                    events.record(self.name, "back", "reconnected")
                self._was_ok = True
                self._set("ok")
                self._first.set()
            else:
                try:
                    healthy = self._healthy()
                except Exception:
                    healthy = False
                if not healthy:
                    events.record(self.name, "lost", self.error or "stopped answering")
                    try:
                        self._close()
                    except Exception:
                        log.exception("closing %s failed", self.name)
                    self._set("missing", "lost: stopped answering")
                    continue  # reopen right away
                self._stop.wait(self._check_s)
