import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gatt.api_client.client import ApiClient
from gatt.clients import ClientTracker
from gatt.protocol import uuids
from gatt.protocol.framing import encode
from gatt.services.device import DeviceService
from gatt.services.position import PositionService
from gatt.status_poller import StatusPoller


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def publish(self, char_uuid, payload):
        self.sent.append((char_uuid, json.loads(payload)))


def api_with(handler):
    return ApiClient("http://api.test", 5, transport=httpx.MockTransport(handler))


def raising(exc):
    def handler(request):
        raise exc
    return handler


async def send(device, notifier, request: dict):
    """Writes a command like a phone would (20 byte packets) and waits for the response."""
    for frame in encode(json.dumps(request).encode(), 20):
        device.on_command_write(frame)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if notifier.sent:
            return notifier.sent.pop()[1]
    raise AssertionError("no response")


def test_command_success_maps_to_endpoint():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"status": "started"})

    async def go():
        notifier = FakeNotifier()
        device = DeviceService(api_with(handler), notifier)
        return await send(device, notifier, {"id": 1, "cmd": "measurement.start", "params": {"project_name": "p1"}})

    response = asyncio.run(go())
    assert seen == [("POST", "/start", {"project_name": "p1"})]
    assert response == {"id": 1, "ok": True, "http_status": 200, "data": {"status": "started"}}


@pytest.mark.parametrize("handler,code", [
    (raising(httpx.ConnectError("refused")), "api_unavailable"),
    (raising(httpx.ReadTimeout("slow")), "api_timeout"),
    (lambda r: httpx.Response(400, json={"status": "invalid project"}), "client_error"),
    (lambda r: httpx.Response(422, json={"detail": [{"msg": "x"}]}), "invalid_params"),
    (lambda r: httpx.Response(500, json={"status": "couldn't start"}), "server_error"),
    (lambda r: httpx.Response(500, text="Internal Server Error"), "server_error"),
])
def test_http_failures_become_error_responses(handler, code):
    async def go():
        notifier = FakeNotifier()
        device = DeviceService(api_with(handler), notifier)
        return await send(device, notifier, {"id": "a", "cmd": "camera.start_recording"})

    response = asyncio.run(go())
    assert response["ok"] is False and response["id"] == "a"
    assert response["error"]["code"] == code


def test_device_unavailable_when_status_says_sensor_missing():
    async def go():
        notifier = FakeNotifier()
        device = DeviceService(api_with(lambda r: httpx.Response(500, json={"status": "couldn't start"})), notifier)
        await device.publish_status({"sensors": {"camera": "not connected", "gnss": "ok", "imu": "ok"}})
        notifier.sent.clear()
        return await send(device, notifier, {"id": 1, "cmd": "camera.start_recording"})

    assert asyncio.run(go())["error"]["code"] == "device_unavailable"


def test_malformed_and_unknown_commands():
    async def go():
        notifier = FakeNotifier()
        device = DeviceService(api_with(lambda r: httpx.Response(200, json={})), notifier)
        unknown = await send(device, notifier, {"id": 2, "cmd": "http.get"})
        device.on_command_write(encode(b"not json", 20)[0])
        await asyncio.sleep(0.05)
        return unknown, notifier.sent.pop()[1]

    unknown, malformed = asyncio.run(go())
    assert unknown["error"]["code"] == "unknown_command" and unknown["id"] == 2
    assert malformed["error"]["code"] == "malformed_request" and malformed["id"] is None


def test_status_poll_is_one_call_with_the_camera_block():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(500, json={
            "status": "not ready", "sensors": {"camera": "ok", "gnss": "not connected", "imu": "ok"},
            "camera": {"connected": True, "name": "X4", "video": "360", "quality": "5.7K/30", "recording": False,
                       "battery": {"level": 80, "scale": 100, "percent": 80},
                       "card": {"state": "ok", "free_mb": 9000, "total_mb": 64000, "free_percent": 14},
                       "warnings": ["a long sentence the app gets from the warnings list instead"]}})

    async def go():
        return await StatusPoller(api_with(handler), ClientTracker(), 5, None).poll_once()

    report = asyncio.run(go())
    assert paths == ["/status"]                                   # no separate battery and storage calls
    assert report["status"] == "not ready" and report["sensors"]["gnss"] == "not connected"
    assert report["camera"] == {"name": "X4", "video": "360", "quality": "5.7K/30", "recording": False,
                                "battery": {"level": 80, "scale": 100, "percent": 80},
                                "card": {"state": "ok", "free_mb": 9000, "total_mb": 64000, "free_percent": 14}}
    assert report["api_reachable"] is True and "errors" not in report


def test_status_poll_with_api_down():
    async def go():
        poller = StatusPoller(api_with(raising(httpx.ConnectError("refused"))), ClientTracker(), 5, None)
        return await poller.poll_once()

    report = asyncio.run(go())
    assert report["api_reachable"] is False
    assert report["errors"]["status"]["code"] == "api_unavailable"


def test_position_service_states():
    async def go():
        notifier = FakeNotifier()
        service = PositionService(notifier)
        await service.start()
        await service.on_gnss_state(False)
        await service.on_gnss_state(False)       # no duplicate
        await service.on_position({"latitude": 1.0, "longitude": 2.0, "timestamp_ns": 3})
        await service.on_gnss_state(False)       # ignored once we have data
        return notifier.sent

    sent = asyncio.run(go())
    assert [m["error"]["code"] for _, m in sent[:2]] == ["no_position_fix", "gnss_unavailable"]
    assert sent[2][1] == {"latitude": 1.0, "longitude": 2.0, "timestamp_ns": 3}
    assert len(sent) == 3
    assert all(c == uuids.POSITION_CHAR_UUID for c, _ in sent)


def test_fastapi_position_stream_pushes_updates():
    from app.core.position import hub
    from app.routers import gnss
    from shared.models import Position

    app = FastAPI()
    app.include_router(gnss.router, prefix="/gnss")
    hub.latest = None

    with TestClient(app) as client:
        assert client.get("/gnss/position").status_code == 503

        # The app runs on the TestClient's own event loop
        client.portal.call(lambda: hub.bind_loop(asyncio.get_running_loop()))

        with client.websocket_connect("/gnss/position/stream") as ws:
            hub.publish(Position(latitude=50.0, longitude=14.0, timestamp_ns=1, satellites=9))
            message = ws.receive_json()

        assert client.get("/gnss/position").json()["position"]["latitude"] == 50.0

    assert message["position"]["latitude"] == 50.0
    assert message["position"]["satellites"] == 9
    assert message["position"]["altitude_m"] is None
