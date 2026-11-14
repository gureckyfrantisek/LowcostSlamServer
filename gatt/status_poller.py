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
        """One aggregate read: /status plus the camera's battery and storage."""
        timeout = self._interval
        status, battery, storage = await asyncio.gather(
            self._api.call(COMMANDS["device.status"], {}, timeout=timeout),
            self._api.call(COMMANDS["camera.battery"], {}, timeout=timeout),
            self._api.call(COMMANDS["camera.storage"], {}, timeout=timeout),
            return_exceptions=True,
        )

        report: dict = {"timestamp": round(time.time(), 3), "api_reachable": True}
        errors: dict = {}

        if isinstance(status, ApiError):
            report["api_reachable"] = status.code not in ("api_unavailable", "api_timeout")
            errors["status"] = {"code": status.code, "message": status.message}
        elif isinstance(status, BaseException):
            log.error("status poll crashed: %r", status)
            errors["status"] = {"code": "internal_error", "message": "status poll failed"}
        else:
            report.update(compact_status(status.data))

        for key, result in (("battery", battery), ("storage", storage)):
            if isinstance(result, ApiError):
                errors[key] = {"code": result.code, "message": result.message}
            elif isinstance(result, BaseException):
                log.error("%s poll crashed: %r", key, result)
                errors[key] = {"code": "internal_error", "message": f"{key} poll failed"}
            else:
                report[key] = result.data.get(key)

        if errors:
            report["errors"] = errors

        return report
