# Tracks connected BLE centrals.
#
# bless does not report connections, so we watch BlueZ over D-Bus
# (org.bluez.Device1.Connected). BlueZ itself keeps per-connection
# subscriptions and stops notifying a client once it disconnects; this tracker
# lets us skip the work (HTTP polling, position stream) while nobody is connected.
import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)

DEVICE_IFACE = "org.bluez.Device1"


class ClientTracker:
    def __init__(self):
        self._connected: set[str] = set()
        self._unknown = False
        self._last_count = 0
        self._bus = None
        self._listeners: list[Callable[[int], None]] = []
        self.has_clients = asyncio.Event()
        self.no_clients = asyncio.Event()
        self.no_clients.set()

    @property
    def count(self) -> int:
        return len(self._connected)

    def add_listener(self, callback: Callable[[int], None]):
        """callback(connected_count) runs whenever the number of clients changes."""
        self._listeners.append(callback)

    async def start(self):
        try:
            await self._watch_bluez()
        except Exception as e:
            # Without D-Bus visibility assume someone is always connected, so nothing
            # stops working; we just lose the idle optimisation.
            log.warning("Cannot watch BlueZ connections (%s), assuming a client is always connected", e)
            self._unknown = True
            self._refresh()

    async def stop(self):
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _watch_bluez(self):
        from dbus_next import BusType, Message, MessageType
        from dbus_next.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus = bus

        reply = await bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager", member="GetManagedObjects",
        ))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.body[0] if reply.body else "GetManagedObjects failed")

        for path, interfaces in reply.body[0].items():
            device = interfaces.get(DEVICE_IFACE)
            if device and device.get("Connected") and device["Connected"].value:
                self._connected.add(path)

        def on_message(message):
            if (message.message_type == MessageType.SIGNAL
                    and message.member == "PropertiesChanged"
                    and message.body and message.body[0] == DEVICE_IFACE
                    and "Connected" in message.body[1]):
                if message.body[1]["Connected"].value:
                    self._connected.add(message.path)
                else:
                    self._connected.discard(message.path)
                self._refresh()

        bus.add_message_handler(on_message)

        await bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=["type='signal',sender='org.bluez',"
                  "interface='org.freedesktop.DBus.Properties',"
                  "member='PropertiesChanged',arg0='org.bluez.Device1'"],
        ))

        self._refresh()

    def _refresh(self):
        connected = self._unknown or bool(self._connected)

        if connected:
            self.has_clients.set()
            self.no_clients.clear()
        else:
            self.has_clients.clear()
            self.no_clients.set()

        if self.count != self._last_count:
            self._last_count = self.count
            log.info("BLE clients connected: %d", self.count)
            for callback in self._listeners:
                try:
                    callback(self.count)
                except Exception:
                    log.exception("client listener failed")
