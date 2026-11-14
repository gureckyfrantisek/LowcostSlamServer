# Entry point:  python -m gatt.main
import asyncio
import logging
import signal

from gatt import adapter
from gatt import config as gatt_config
from gatt.api_client.client import ApiClient, ApiError
from gatt.clients import ClientTracker
from gatt.position_stream import PositionStream
from gatt.protocol import messages, uuids
from gatt.protocol.commands import COMMANDS
from gatt.server import GattServer
from gatt.services.device import DeviceService
from gatt.services.position import PositionService
from gatt.status_poller import StatusPoller

log = logging.getLogger("gatt")

BLE_RETRY_S = 5
PROTOCOL_VERSION = 1
INFO_RETRY_S = 5      # until the API has answered once
INFO_REFRESH_S = 300  # the settings of the device hardly ever change


async def bring_up(cfg, server: GattServer, stop: asyncio.Event):
    """Sets up the adapter (open pairing) and starts advertising. Bluetooth may not be up yet at boot, so it
    keeps trying instead of exiting, and a stop request ends the trying at once.
    Returns the D-Bus connection of the pairing agent (None without one), or False if stopped first."""
    bus = None
    while not stop.is_set():
        problems = []
        if cfg.open_pairing and bus is None:
            # Any phone may connect and pair without a PIN
            bus, problems = await adapter.open_up(cfg.device_name)
            for problem in problems:
                log.warning("Bluetooth setup: %s", problem)
        try:
            await server.start()
            return bus
        except Exception as e:
            log.error("Cannot start the GATT server (%s), retrying in %d s", e, BLE_RETRY_S)
        if problems and bus is not None:
            bus.disconnect()    # the setup did not work (BlueZ not there yet): do it all again next time
            bus = None
        try:
            await asyncio.wait_for(stop.wait(), BLE_RETRY_S)
        except asyncio.TimeoutError:
            pass
    if bus is not None:
        bus.disconnect()
    return False


def info_payload(cfg, api_info: dict | None) -> bytes:
    """What the Info characteristic says: enough for the app to list the device it found and to check that
    it speaks the same protocol. `api` is what the API says about itself, missing until it answered"""
    info = {"protocol": PROTOCOL_VERSION, "name": cfg.device_name, "id": cfg.device_id,
            "service": cfg.service_uuid, "api_reachable": api_info is not None}
    if api_info is not None:
        info["api"] = api_info
    return messages.dumps(info)


async def publish_info_forever(cfg, api: ApiClient, server: GattServer):
    """The Info characteristic is readable from the start (without the API part), then filled in"""
    await server.publish(uuids.INFO_CHAR_UUID, info_payload(cfg, None))
    while True:
        try:
            result = await api.call(COMMANDS["device.info"], {})
            await server.publish(uuids.INFO_CHAR_UUID, info_payload(cfg, result.data))
            await asyncio.sleep(INFO_REFRESH_S)
        except ApiError:
            await asyncio.sleep(INFO_RETRY_S)
        except Exception:
            log.exception("info publishing failed")
            await asyncio.sleep(INFO_RETRY_S)


async def main():
    cfg = gatt_config.load()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api = ApiClient(cfg.api_base_url, cfg.api_timeout)
    tracker = ClientTracker()
    server = GattServer(cfg, tracker)

    device = DeviceService(api, server)
    position = PositionService(server)

    server.on_command_write = device.on_command_write
    tracker.add_listener(lambda count: device.reset() if count == 0 else None)

    async def on_status(status: dict):
        await device.publish_status(status)
        sensors = status.get("sensors")
        if isinstance(sensors, dict):
            await position.on_gnss_state(sensors.get("gnss") == "ok")

    poller = StatusPoller(api, tracker, cfg.status_poll_interval, on_status)
    stream = PositionStream(cfg.position_stream_url, tracker, position.on_position, position.on_stream_error,
                            position.on_no_fix)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    await tracker.start()
    bus = await bring_up(cfg, server, stop)
    if bus is False:
        await tracker.stop()
        await api.aclose()
        return
    await position.start()

    # Independent background tasks; none can take the BLE server down
    tasks = [asyncio.create_task(poller.run()), asyncio.create_task(stream.run()),
             asyncio.create_task(publish_info_forever(cfg, api, server))]
    log.info("API: %s, status every %.1f s, position stream: %s",
             cfg.api_base_url, cfg.status_poll_interval, cfg.position_stream_url)

    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.stop()
        await tracker.stop()
        if bus is not None:
            bus.disconnect()
        await api.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
