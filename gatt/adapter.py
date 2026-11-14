# Makes the Bluetooth adapter accept every phone without a PIN or a confirmation:
#  - the adapter is powered, pairable and discoverable for ever (no timeouts)
#  - a "NoInputNoOutput" agent says yes to every pairing and authorization request
# The GATT characteristics need neither pairing nor encryption, so most phones connect without any of
# this; the agent is for the phones that start a pairing anyway (Android does, for some apps).
# Everything is best effort: a failure is logged and the GATT server still runs.
import logging

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/org/lowcostslam/agent"
CAPABILITY = "NoInputNoOutput"


class OpenAgent(ServiceInterface):
    """org.bluez.Agent1: accepts everything"""

    def __init__(self):
        super().__init__("org.bluez.Agent1")
        self.accepted: list[str] = []

    @method()
    def Release(self):  # noqa: N802
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore # noqa: F821 N802
        self.accepted.append(device)
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore # noqa: F821 N802
        self.accepted.append(device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # type: ignore # noqa: F821 N802
        pass

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # type: ignore # noqa: F821 N802
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # type: ignore # noqa: F821 N802
        self.accepted.append(device)   # returning without an error is "yes"

    @method()
    def RequestAuthorization(self, device: "o"):  # type: ignore # noqa: F821 N802
        self.accepted.append(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # type: ignore # noqa: F821 N802
        self.accepted.append(device)

    @method()
    def Cancel(self):  # noqa: N802
        pass


# What the adapter should look like, and the D-Bus calls that make it so
ADAPTER_PROPERTIES = {
    "Powered": True,
    "Pairable": True,
    "PairableTimeout": 0,        # 0 = never
    "Discoverable": True,
    "DiscoverableTimeout": 0,
}


def _variant(value):
    return Variant("b" if isinstance(value, bool) else "u", value)


async def prepare_adapter(bus: MessageBus, adapter_path: str = "/org/bluez/hci0", name: str | None = None) -> list[str]:
    """Sets the adapter properties. Returns the problems met (empty when all worked)"""
    from dbus_next import Message, MessageType
    problems = []
    properties = dict(ADAPTER_PROPERTIES)
    for prop, value in properties.items():
        reply = await bus.call(Message(
            destination="org.bluez", path=adapter_path, interface="org.freedesktop.DBus.Properties",
            member="Set", signature="ssv", body=["org.bluez.Adapter1", prop, _variant(value)]))
        if reply.message_type == MessageType.ERROR:
            problems.append(f"{prop}: {reply.body[0] if reply.body else 'error'}")
    if name:
        reply = await bus.call(Message(
            destination="org.bluez", path=adapter_path, interface="org.freedesktop.DBus.Properties",
            member="Set", signature="ssv", body=["org.bluez.Adapter1", "Alias", Variant("s", name)]))
        if reply.message_type == MessageType.ERROR:
            problems.append(f"Alias: {reply.body[0] if reply.body else 'error'}")
    return problems


async def register_open_agent(bus: MessageBus) -> OpenAgent:
    """Makes the agent above BlueZ's default agent. Raises if BlueZ refuses"""
    from dbus_next import Message, MessageType
    agent = OpenAgent()
    bus.export(AGENT_PATH, agent)
    for member, signature, body in (("RegisterAgent", "os", [AGENT_PATH, CAPABILITY]),
                                    ("RequestDefaultAgent", "o", [AGENT_PATH])):
        reply = await bus.call(Message(destination="org.bluez", path="/org/bluez",
                                       interface="org.bluez.AgentManager1", member=member,
                                       signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.body[0] if reply.body else 'error'}")
    return agent


async def open_up(name: str) -> tuple[MessageBus | None, list[str]]:
    """Prepares the adapter and registers the open agent. Returns the bus (keep it open: the agent lives on
    it) and the problems; (None, [why]) when BlueZ cannot be reached at all"""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as e:
        return None, [f"no D-Bus: {e}"]
    problems = []
    try:
        problems += await prepare_adapter(bus, name=name)
    except Exception as e:
        problems.append(f"adapter: {e}")
    try:
        await register_open_agent(bus)
    except Exception as e:
        problems.append(f"agent: {e}")
    return bus, problems
