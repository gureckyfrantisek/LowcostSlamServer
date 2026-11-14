# GNSS receiver -> NMEA -> position hub -> WebSocket / HTTP, on the simulated bus
import calendar
import time

import pytest
from fastapi.testclient import TestClient

from app.core import camera, config, gnss, imu, position
from tests.fakes import nmea

RMC = nmea("GNRMC,115840.00,A,5005.89580,N,01422.01245,E,0.032,,141126,,,A,V")
GGA = nmea("GNGGA,115840.00,5005.89580,N,01422.01245,E,1,12,0.56,304.6,M,44.4,M,,")
NO_FIX = nmea("GNGGA,115841.00,,,,,0,00,99.99,,,,,,")


def wait_for(condition, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def feed(bus):
    hub = position.PositionHub()
    gnss.open_gnss_port()
    f = position.GnssFeed(hub)
    f.start()
    yield f, hub
    f.stop()
    gnss.close_gnss_port()


def test_positions_from_the_receiver_reach_the_hub(feed, bus):
    f, hub = feed
    bus.stream += RMC + GGA
    assert wait_for(lambda: hub.latest is not None)
    assert hub.latest.latitude == pytest.approx(50.0982633, abs=1e-6)
    assert hub.latest.timestamp_ns == calendar.timegm((2026, 11, 14, 11, 58, 40)) * 10**9
    assert f.positions == 1 and not f.no_fix


def test_losing_the_fix_clears_the_latest_position_once(feed, bus):
    f, hub = feed
    bus.stream += RMC + GGA
    assert wait_for(lambda: hub.latest is not None)
    bus.stream += NO_FIX + NO_FIX
    assert wait_for(lambda: hub.latest is None and f.no_fix)


def test_a_garbled_sentence_does_not_stop_the_feed(feed, bus):
    f, hub = feed
    bus.stream += nmea("GNGGA,115840.00,5005.89580,N,01422.01245,E,1,12,-3.0,304.6,M,44.4,M,,") + RMC + GGA
    assert wait_for(lambda: hub.latest is not None)    # the bad one was skipped, the next epoch worked


def test_the_feed_survives_a_gnss_reconnect(feed, bus):
    f, hub = feed
    bus.stream += RMC + GGA
    assert wait_for(lambda: f.positions == 1)
    gnss.close_gnss_device()                           # what the supervisor does when the receiver is lost
    gnss.open_gnss_port()
    bus.stream += RMC + GGA
    assert wait_for(lambda: f.positions == 2)


def test_slow_websocket_clients_get_the_newest_position():
    import asyncio
    hub = position.PositionHub()

    async def go():
        hub.bind_loop(asyncio.get_running_loop())
        q = hub.subscribe()
        for i in range(40):
            hub.publish(position.Position(latitude=1.0, longitude=2.0, timestamp_ns=i))
        await asyncio.sleep(0.05)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items
    items = asyncio.run(go())
    assert len(items) == 16 and items[-1].timestamp_ns == 39     # the oldest were dropped


@pytest.fixture
def client(bus, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(camera, "open_camera", lambda: None)
    monkeypatch.setattr(camera, "close_camera", lambda: True)
    monkeypatch.setattr(camera, "verify_connection", lambda: True)
    position.hub.latest = None
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_websocket_streams_positions_and_no_fix(client, bus):
    with client.websocket_connect("/gnss/position/stream") as ws:
        bus.stream += RMC + GGA
        first = ws.receive_json()
        bus.stream += NO_FIX
        second = ws.receive_json()
    assert first["position"]["satellites"] == 12 and first["position"]["fix_type"] == "gnss"
    assert second == {"position": None}


def test_http_position_follows_the_receiver(client, bus):
    assert client.get("/gnss/position").status_code == 503
    bus.stream += RMC + GGA
    assert wait_for(lambda: client.get("/gnss/position").status_code == 200)
    assert client.get("/gnss/position").json()["position"]["altitude_m"] == 304.6
    bus.stream += NO_FIX
    assert wait_for(lambda: client.get("/gnss/position").status_code == 503)


def test_a_new_websocket_client_starts_with_the_latest_position(client, bus):
    bus.stream += RMC + GGA
    assert wait_for(lambda: client.get("/gnss/position").status_code == 200)
    with client.websocket_connect("/gnss/position/stream") as ws:
        assert ws.receive_json()["position"]["latitude"] == pytest.approx(50.0982633, abs=1e-6)


def test_info_says_who_the_device_is(client):
    info = client.get("/info").json()
    assert info["id"] == config.DEVICE_ID and info["name"] == config.DEVICE_NAME
    assert info["imu"]["hz"] == config.IMU_I2C_HZ and info["gnss"]["hz"] == config.GNSS_RATE_HZ
    assert {"measurement", "position"} <= set(info["capabilities"])
