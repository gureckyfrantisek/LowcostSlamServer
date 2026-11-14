# Device status is polled over HTTP (no subscription): every STATUS_POLL_INTERVAL seconds
# while at least one BLE client is connected.
import asyncio
import logging
import time
from typing import Awaitable, Callable

from gatt.api_client.client import ApiClient, ApiError
from gatt.clients import ClientTracker
from gatt.protocol.commands import COMMANDS

log = logging.getLogger(__name__)


MAX_EVENTS = 3


def compact_status(data: dict) -> dict:
    """What /status says, small enough to read in one go over BLE: why a device is missing, the state of
    the measurement and the disk, what to warn about, and the last few events"""
    out = {"status": data.get("status"), "sensors": data.get("sensors")}
    camera = data.get("camera")
    if isinstance(camera, dict) and camera.get("connected"):
        out["camera"] = {k: camera[k] for k in ("name", "type", "video", "mode", "quality", "encode", "recording",
                                                 "battery", "card", "stopped") if camera.get(k) is not None}
    details = data.get("details")
    if isinstance(details, dict):
        why = {name: d.get("detail") for name, d in details.items()
               if isinstance(d, dict) and d.get("state") != "ok" and d.get("detail")}
        if why:
            out["detail"] = why
    recording = data.get("recording")
    if isinstance(recording, dict):
        out["recording"] = recording
    disk = data.get("storage")
    if isinstance(disk, dict):
        out["disk"] = {k: disk[k] for k in ("location", "usb_present", "free_mb", "hours_left", "level") if k in disk}
    warnings = data.get("warnings")
    if warnings:
        out["warnings"] = warnings
    events = data.get("events")
    if isinstance(events, list) and events:
        out["events"] = [{"ts_ns": e.get("ts_ns"), "source": e.get("source"), "kind": e.get("kind"),
                          "detail": e.get("detail")} for e in events[-MAX_EVENTS:] if isinstance(e, dict)]
    return out


class StatusPoller:
    def __init__(
        self,
        api: ApiClient,
        tracker: ClientTracker,
        interval: float,
        on_status: Callable[[dict], Awaitable[None]],
    ):
        self._api = api
        self._tracker = tracker
        self._interval = interval
        self._on_status = on_status

    async def run(self):
        while True:
            await self._tracker.has_clients.wait()

            try:
                await self._on_status(await self.poll_once())
            except Exception:
                # Whatever happens, the next round still runs
                log.exception("status round failed")

            await asyncio.sleep(self._interval)

    async def poll_once(self) -> dict:
        """One read: /status, which carries the camera's battery, card and mode too"""
        try:
            status = await self._api.call(COMMANDS["device.status"], {}, timeout=self._interval)
        except BaseException as e:
            status = e

        report: dict = {"timestamp": round(time.time(), 3), "api_reachable": True}

        if isinstance(status, ApiError):
            report["api_reachable"] = status.code not in ("api_unavailable", "api_timeout")
            report["errors"] = {"status": {"code": status.code, "message": status.message}}
        elif isinstance(status, BaseException):
            if isinstance(status, asyncio.CancelledError):
                raise status
            log.error("status poll crashed: %r", status)
            report["errors"] = {"status": {"code": "internal_error", "message": "status poll failed"}}
        else:
            report.update(compact_status(status.data))

        return report
