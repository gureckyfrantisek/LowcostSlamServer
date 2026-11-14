# The GATT server with a fake bless (no Bluetooth needed): what is advertised, what the characteristics are,
# how notifications are framed, the info characteristic, the open pairing agent and the advertisement size.
import asyncio
import json

import httpx
import pytest

from gatt import adapter, config as gatt_config, main as gatt_main, server as gatt_server
from gatt.api_client.client import ApiClient
from gatt.clients import ClientTracker
from gatt.protocol import messages, uuids
from gatt.protocol.framing import Reassembler
from gatt.status_poller import compact_status


class FakeCharacteristic:
    def __init__(self, uuid):
        self.uuid, self.value = uuid, bytearray()


class FakeBless:
    instances = []

    def __init__(self, name=None, loop=None):
        self.name, self.services, self.chars, self.updates = name, [], {}, []
        self.started = self.stopped = False
        self.read_request_func = self.write_request_func = None
        FakeBless.instances.append(self)

    async def add_new_service(self, uuid):
        self.services.append(uuid)

    async def add_new_characteristic(self, service_uuid, char_uuid, properties, value, permissions):
        self.chars[char_uuid] = (service_uuid, properties, permissions, FakeCharacteristic(char_uuid))

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def get_characteristic(self, uuid):
        return self.chars[uuid][3]

    def update_value(self, service_uuid, char_uuid):
        self.updates.append((service_uuid, char_uuid, bytes(self.chars[char_uuid][3].value)))


@pytest.fixture
def fake_bless(monkeypatch):
    FakeBless.instances.clear()
    monkeypatch.setattr(gatt_server, "BlessServer", FakeBless)
    return FakeBless


def config(**kw):
    return gatt_config.load().__class__(**{**gatt_config.load().__dict__, **kw})


def make_server(cfg=None, clients=True):
    tracker = ClientTracker()
    if clients:
        tracker.has_clients.set()
        tracker.no_clients.clear()
    return gatt_server.GattServer(cfg or gatt_config.load(), tracker)


def test_one_service_with_five_characteristics(fake_bless):
    async def go():
        server = make_server()
        await server.start()
        return fake_bless.instances[0]
    bless = asyncio.run(go())
    cfg = gatt_config.load()
    assert bless.started and bless.name == cfg.device_name
    assert bless.services == [uuids.DEVICE_SERVICE_UUID]       # one service: the advertisement has room for one UUID
    assert set(bless.chars) == {uuids.COMMAND_CHAR_UUID, uuids.RESPONSE_CHAR_UUID, uuids.STATUS_CHAR_UUID,
                                uuids.POSITION_CHAR_UUID, uuids.INFO_CHAR_UUID}
    assert all(service == uuids.DEVICE_SERVICE_UUID for service, *_ in bless.chars.values())


def test_characteristic_properties_and_no_pairing_needed(fake_bless):
    P = gatt_server._P
    A = gatt_server._A
    props = {u: p for u, (p, _) in gatt_server.CHARACTERISTICS.items()}
    assert props[uuids.COMMAND_CHAR_UUID] == P.write | P.write_without_response
    for readable in (uuids.RESPONSE_CHAR_UUID, uuids.STATUS_CHAR_UUID, uuids.POSITION_CHAR_UUID):
        assert props[readable] == P.read | P.notify
    assert props[uuids.INFO_CHAR_UUID] == P.read                # nothing to subscribe to: read it once
    perms = {u: a for u, (_, a) in gatt_server.CHARACTERISTICS.items()}
    assert set(perms.values()) <= {A.readable, A.writeable}      # no *_encryption_required: any phone, no pairing


def test_uuids_are_valid_and_distinct():
    import uuid
    all_uuids = [uuids.DEVICE_SERVICE_UUID, uuids.COMMAND_CHAR_UUID, uuids.RESPONSE_CHAR_UUID, uuids.STATUS_CHAR_UUID,
                 uuids.POSITION_CHAR_UUID, uuids.INFO_CHAR_UUID]
    assert len({str(uuid.UUID(u)) for u in all_uuids}) == len(all_uuids)
    assert all(u == u.lower() for u in all_uuids)


def test_advertisement_fits_31_bytes_for_any_device_id(monkeypatch):
    """flags (3) + one complete 128-bit UUID (2 + 16) + the complete local name (2 + n)"""
    from shared import device
    for device_id in ("0000", "FFFF", "3F2A", "ABCD"):
        monkeypatch.setenv("DEVICE_ID", device_id)
        monkeypatch.delenv("DEVICE_NAME", raising=False)
        name = device.device_name()
        assert 3 + 18 + 2 + len(name.encode()) <= 31, name


def test_notification_is_framed_and_read_returns_the_whole_message(fake_bless):
    payload = json.dumps({"status": "ready", "x": "y" * 300}).encode()

    async def go():
        server = make_server(config(chunk_size=100))
        await server.start()
        await server.publish(uuids.STATUS_CHAR_UUID, payload)
        bless = fake_bless.instances[0]
        read = server._on_read(bless.get_characteristic(uuids.STATUS_CHAR_UUID))
        return bless.updates, read
    updates, read = asyncio.run(go())
    assert bytes(read) == payload                                # READ: the complete latest value, no framing
    assert [u[1] for u in updates] == [uuids.STATUS_CHAR_UUID] * 4
    reassembler, message = Reassembler(), None
    for _, _, frame in updates:
        assert len(frame) <= 101
        message = reassembler.feed(frame) or message
    assert message == payload                                    # NOTIFY: framed packets that put it back together


def test_nothing_is_notified_without_a_client_but_the_value_is_readable(fake_bless):
    async def go():
        server = make_server(clients=False)
        await server.start()
        await server.publish(uuids.POSITION_CHAR_UUID, b'{"a":1}')
        bless = fake_bless.instances[0]
        return bless.updates, server._on_read(bless.get_characteristic(uuids.POSITION_CHAR_UUID))
    updates, read = asyncio.run(go())
    assert updates == [] and bytes(read) == b'{"a":1}'


def test_info_is_only_readable_never_notified(fake_bless):
    async def go():
        server = make_server()
        await server.start()
        await server.publish(uuids.INFO_CHAR_UUID, b'{"protocol":1}')
        bless = fake_bless.instances[0]
        return bless.updates, server._on_read(bless.get_characteristic(uuids.INFO_CHAR_UUID))
    updates, read = asyncio.run(go())
    assert updates == [] and bytes(read) == b'{"protocol":1}'


def test_commands_written_by_the_phone_reach_the_handler(fake_bless):
    async def go():
        server = make_server()
        got = []
        server.on_command_write = got.append
        await server.start()
        bless = fake_bless.instances[0]
        server._on_write(bless.get_characteristic(uuids.COMMAND_CHAR_UUID), bytearray(b"\x03{}"))
        server._on_write(bless.get_characteristic(uuids.STATUS_CHAR_UUID), bytearray(b"ignored"))
        return got
    assert asyncio.run(go()) == [b"\x03{}"]


def test_stop_stops_the_server_and_a_second_stop_is_harmless(fake_bless):
    async def go():
        server = make_server()
        await server.start()
        await server.stop()
        await server.stop()
        return fake_bless.instances[0].stopped
    assert asyncio.run(go())


# --- the info characteristic -----------------------------------------------------------------

def test_info_payload_without_and_with_the_api():
    cfg = gatt_config.load()
    bare = json.loads(gatt_main.info_payload(cfg, None))
    assert bare == {"protocol": 1, "name": cfg.device_name, "id": cfg.device_id, "service": cfg.service_uuid,
                    "api_reachable": False}
    full = json.loads(gatt_main.info_payload(cfg, {"imu": {"hz": 100}}))
    assert full["api_reachable"] is True and full["api"] == {"imu": {"hz": 100}}
    assert len(gatt_main.info_payload(cfg, {"imu": {"hz": 100}, "gnss": {"hz": 1}, "capabilities": ["a"] * 6})) < 500


def test_info_is_readable_at_once_and_filled_in_when_the_api_answers(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            raise httpx.ConnectError("not up yet")
        return httpx.Response(200, json={"id": "ABCD", "imu": {"hz": 100}})

    class Stub:
        def __init__(self):
            self.published = []

        async def publish(self, char, payload):
            self.published.append((char, json.loads(payload)))

    async def go():
        monkeypatch.setattr(gatt_main, "INFO_RETRY_S", 0.01)
        stub = Stub()
        api = ApiClient("http://api.test", 5, transport=httpx.MockTransport(handler))
        task = asyncio.create_task(gatt_main.publish_info_forever(gatt_config.load(), api, stub))
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await api.aclose()
        return stub.published
    published = asyncio.run(go())
    assert all(char == uuids.INFO_CHAR_UUID for char, _ in published)
    assert published[0][1]["api_reachable"] is False             # readable before the API is up
    assert published[-1][1]["api_reachable"] is True and published[-1][1]["api"]["id"] == "ABCD"
    assert calls == ["/info"] * 3


# --- the richer status ---------------------------------------------------------------------------

def test_status_is_compacted_for_one_read():
    data = {
        "status": "not ready", "sensors": {"camera": "not connected", "gnss": "ok", "imu": "ok"},
        "details": {"camera": {"state": "missing", "detail": "no camera found", "since": 1.0},
                    "gnss": {"state": "ok", "detail": None, "since": 1.0}},
        "recording": {"active": True, "project": "p", "problems": ["camera is not connected, its data has a gap"]},
        "storage": {"location": "usb", "path": "/media/usb/sda1/LowcostSlam", "usb_present": True, "free_mb": 9000,
                    "hour_mb": 23.0, "hours_left": 391.3, "level": "ok"},
        "warnings": ["camera is not connected, its data has a gap"],
        "events": [{"ts_ns": i, "source": "camera", "kind": "lost", "detail": "x"} for i in range(10)],
    }
    out = compact_status(data)
    assert out["detail"] == {"camera": "no camera found"}             # only what is wrong
    assert out["disk"] == {"location": "usb", "usb_present": True, "free_mb": 9000, "hours_left": 391.3, "level": "ok"}
    assert len(out["events"]) == 3 and out["events"][-1]["ts_ns"] == 9
    assert out["recording"]["active"] and out["warnings"]
    assert len(json.dumps(out)) < 900


def test_status_from_an_older_api_still_works():
    assert compact_status({"status": "ready", "sensors": {"camera": "ok"}}) == {"status": "ready", "sensors": {"camera": "ok"}}


# --- open pairing -------------------------------------------------------------------------------------

def test_the_agent_says_yes_to_everything():
    agent = adapter.OpenAgent()

    def call(name, *args):
        # dbus_next calls the original function of a method, the decorated one drops the return value
        return getattr(adapter.OpenAgent, name).__dict__["__DBUS_METHOD"].fn(agent, *args)

    call("RequestConfirmation", "/org/bluez/hci0/dev_AA", 123456)
    call("RequestAuthorization", "/org/bluez/hci0/dev_BB")
    call("AuthorizeService", "/org/bluez/hci0/dev_CC", "0000180f-0000-1000-8000-00805f9b34fb")
    assert call("RequestPinCode", "/org/bluez/hci0/dev_DD") == "0000"
    assert call("RequestPasskey", "/org/bluez/hci0/dev_EE") == 0
    assert len(agent.accepted) == 5
    assert adapter.CAPABILITY == "NoInputNoOutput"                     # no screen, no keyboard: Just Works


def test_adapter_is_set_up_to_stay_visible_and_pairable():
    from dbus_next import MessageType

    class Reply:
        message_type, body = MessageType.METHOD_RETURN, []

    class Bus:
        def __init__(self, fail=()):
            self.calls, self.fail = [], fail

        async def call(self, message):
            self.calls.append((message.member, message.body[1], message.body[2].value))
            if message.body[1] in self.fail:
                err = Reply()
                err.message_type, err.body = MessageType.ERROR, ["org.bluez.Error.Failed"]
                return err
            return Reply()

    async def go(fail=()):
        bus = Bus(fail)
        return await adapter.prepare_adapter(bus, name="SLAM-3F2"), bus.calls
    problems, calls = asyncio.run(go())
    assert problems == []
    assert {(p, v) for _, p, v in calls} == {("Powered", True), ("Pairable", True), ("PairableTimeout", 0),
                                              ("Discoverable", True), ("DiscoverableTimeout", 0), ("Alias", "SLAM-3F2")}
    problems, _ = asyncio.run(go(fail={"Discoverable"}))
    assert problems == ["Discoverable: org.bluez.Error.Failed"]         # one failure does not stop the rest


def test_without_dbus_the_setup_reports_instead_of_raising(monkeypatch):
    async def boom(self):
        raise OSError("no bus")
    monkeypatch.setattr(adapter.MessageBus, "connect", boom)
    bus, problems = asyncio.run(adapter.open_up("SLAM-3F2"))
    assert bus is None and "no D-Bus" in problems[0]


# --- start up and shut down ----------------------------------------------------------------------------

def test_stop_request_ends_the_waiting_for_bluetooth(monkeypatch):
    """BlueZ not up: the server keeps retrying, but SIGTERM must end it (systemctl stop must not hang)"""
    calls = []

    class Down:
        async def start(self):
            calls.append("start")
            raise RuntimeError("The name org.bluez was not provided by any .service files")

    async def no_bluez(name):
        calls.append("setup")
        return None, ["no D-Bus"]

    async def go():
        monkeypatch.setattr(gatt_main.adapter, "open_up", no_bluez)
        monkeypatch.setattr(gatt_main, "BLE_RETRY_S", 30)
        stop = asyncio.Event()
        task = asyncio.create_task(gatt_main.bring_up(gatt_config.load(), Down(), stop))
        await asyncio.sleep(0.1)
        assert not task.done()                       # waiting for Bluetooth
        stop.set()
        return await asyncio.wait_for(task, 1)       # and it ends at once
    assert asyncio.run(go()) is False and calls[:2] == ["setup", "start"]


def test_bluetooth_coming_up_late_is_picked_up_and_set_up_again(monkeypatch):
    attempts = []

    class Late:
        async def start(self):
            attempts.append("start")
            if attempts.count("start") < 2:
                raise RuntimeError("org.bluez not there")

    class Bus:
        disconnected = 0

        def disconnect(self):
            Bus.disconnected += 1

    async def open_up(name):
        attempts.append("setup")
        return Bus(), (["agent: not there"] if attempts.count("setup") == 1 else [])

    async def go():
        monkeypatch.setattr(gatt_main.adapter, "open_up", open_up)
        monkeypatch.setattr(gatt_main, "BLE_RETRY_S", 0.01)
        return await gatt_main.bring_up(gatt_config.load(), Late(), asyncio.Event())
    bus = asyncio.run(go())
    assert isinstance(bus, Bus) and attempts == ["setup", "start", "setup", "start"]
    assert Bus.disconnected == 1                       # the first, half set up connection was dropped


def test_open_pairing_can_be_switched_off(monkeypatch):
    async def go():
        monkeypatch.setattr(gatt_main.adapter, "open_up", lambda name: (_ for _ in ()).throw(AssertionError("no setup")))

        class Ok:
            async def start(self):
                pass
        cfg = gatt_config.load()
        cfg = cfg.__class__(**{**cfg.__dict__, "open_pairing": False})
        return await gatt_main.bring_up(cfg, Ok(), asyncio.Event())
    assert asyncio.run(go()) is None
