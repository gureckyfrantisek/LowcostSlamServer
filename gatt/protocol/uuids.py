# All GATT UUIDs live here. Never inline a UUID anywhere else.
#
# Custom 128-bit base: 7a1c<xxxx>-5b3e-4c8f-9d2a-6e1f0a4b8c10
#
# One service only: a BLE advertisement holds 31 bytes, a 128-bit UUID takes 18 of them, so the
# advertisement carries the one service UUID and the short device name, and the app finds the
# device by that UUID.
_BASE = "7a1c{:04x}-5b3e-4c8f-9d2a-6e1f0a4b8c10"

DEVICE_SERVICE_UUID = _BASE.format(0x0001)
COMMAND_CHAR_UUID = _BASE.format(0x0002)    # WRITE / WRITE_WITHOUT_RESPONSE
RESPONSE_CHAR_UUID = _BASE.format(0x0003)   # READ / NOTIFY
STATUS_CHAR_UUID = _BASE.format(0x0004)     # READ / NOTIFY
POSITION_CHAR_UUID = _BASE.format(0x0005)   # READ / NOTIFY: continuous GNSS position
INFO_CHAR_UUID = _BASE.format(0x0006)       # READ: who this device is (discovery)
