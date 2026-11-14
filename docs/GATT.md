# BLE GATT service (for the mobile app)

The device is controlled over Bluetooth Low Energy. The GATT server (`gatt/`) is a separate process that
adapts BLE to the HTTP API of the server (`app/`): every command is forwarded to the FastAPI app on
localhost, and no business logic lives in `gatt/`.

```text
Phone --BLE--> GATT server (gatt/) --HTTP, localhost--> FastAPI (app/)
                    ^------------ WebSocket (position) -------|
```

Any phone may connect: nothing needs pairing or encryption, and the device accepts a pairing request without a PIN
(see [Connecting](#connecting)).

## Quick start for the app

1. **Scan** for the service UUID `7a1c0001-5b3e-4c8f-9d2a-6e1f0a4b8c10`. The device is named `SLAM-xxx`
   (`xxx` = 3 hex characters of its id, a different one on every board). Show the name to the user; with several
   boards around, they pick by name.
2. **Connect**, request an MTU of at least 104 (Android: `requestMtu(247)`; iOS negotiates by itself),
   discover the service.
3. **Read `Info`** (once): the device id, the protocol version and the settings. Refuse to go on if `protocol` is
   not the one the app knows (it is `1`).
4. **Enable notifications** on `Response`, `Status` and `Position`.
5. Show `Status` (it arrives every 5 s, and once at once after subscribing), `Position`, and send commands to
   `Command`: `{"id": 1, "cmd": "measurement.start", "params": {"project_name": "run1"}}`.

## Service and characteristics

UUID base: `7a1c<xxxx>-5b3e-4c8f-9d2a-6e1f0a4b8c10`. All UUIDs are in `gatt/protocol/uuids.py`.
**One service** only: a BLE advertisement holds 31 bytes, a 128-bit UUID takes 18 and the name 10.

| Characteristic | `xxxx` | Properties | Content |
|---|---|---|---|
| Service | `0001` | | the one the app scans for |
| Command | `0002` | WRITE, WRITE_WITHOUT_RESPONSE | JSON request (framed) |
| Response | `0003` | READ, NOTIFY | JSON response to the last command (framed) |
| Status | `0004` | READ, NOTIFY | JSON device status, every 5 s (framed) |
| Position | `0005` | READ, NOTIFY | JSON position or error (framed) |
| Info | `0006` | READ | JSON: who this device is (read once after connecting) |

Each characteristic carries a Characteristic User Description (`0x2901`) with its name, e.g. `Status (notify)`, so a
BLE explorer such as nRF Connect shows which is which (tap the characteristic or its descriptors). Without it they
are all "Unknown Characteristic": tell them apart by the `xxxx` above, the last digits of the first UUID block
(`7a1c000`**`4`**`-...` is Status).

READ returns the complete latest JSON (no framing). NOTIFY delivers the same JSON as framed packets. A READ of
a value longer than the MTU is a BLE "long read", handled by the phone's stack; prefer the notifications for
`Status`, which is the longest.

## Packet framing

BLE packets are small, so every WRITE and NOTIFY is `1 header byte + up to GATT_CHUNK_SIZE bytes`.

| Header | Meaning |
|---|---|
| `0x03` | whole message in this packet |
| `0x01` | first packet |
| `0x00` | middle packet |
| `0x02` | last packet |

Concatenate the payloads from the first to the last packet and parse as UTF-8 JSON. The same framing is used
when the phone writes a command longer than one packet. `GATT_CHUNK_SIZE` (default 100) must be at most
`ATT MTU - 4`.

## Info (discovery)

```json
{"protocol": 1, "name": "SLAM-840", "id": "8404", "service": "7a1c0001-5b3e-4c8f-9d2a-6e1f0a4b8c10",
 "api_reachable": true,
 "api": {"name": "SLAM-840", "id": "8404", "api_version": 1, "imu": {"hz": 100, "range_g": 2},
         "gnss": {"hz": 1}, "storage": {"local_path": "/home/radxa/measurements"},
         "capabilities": ["measurement", "projects", "camera", "position", "usb_storage"]}}
```

`id` is stable for the board (four characters of its machine id): store it to recognise the device again.
`api` is missing and `api_reachable` is `false` until the API has answered (a few seconds after boot); read again
later. `protocol` changes when the messages change incompatibly.

## Commands

Request (write to Command):

```json
{"id": 12, "cmd": "measurement.start", "params": {"project_name": "run1"}}
```

`id` is an integer or a string (1-64 chars) chosen by the phone and echoed in the response.

Response (notify on Response):

```json
{"id": 12, "ok": true, "http_status": 200, "data": {"status": "started", "storage": {...}, "warnings": []}}
{"id": 12, "ok": false, "error": {"code": "server_error", "message": "already measuring", "http_status": 500}}
```

`data` is the JSON body of the API unchanged. Commands run one at a time (the camera SDK is not re-entrant); up
to 16 may be queued, then `busy` is returned. BlueZ notifies every subscribed client, so with several phones
connected each sees all responses: match them by `id`.

| BLE `cmd` | params | API call | Timeout |
|---|---|---|---|
| `device.status` | - | `GET /status` (a 500 "not ready" body is returned as data) | 10 s |
| `device.info` | - | `GET /info` | 10 s |
| `measurement.start` | `project_name` | `POST /start?project_name=` | 30 s |
| `measurement.stop` | `project_name` | `POST /stop?project_name=` | 60 s |
| `control_point.start` | `point_id` | `POST /control-point/start?point_id=` | 10 s |
| `control_point.finish` | - | `POST /control-point/finish` | 10 s |
| `control_point.cancel` | - | `POST /control-point/cancel` | 10 s |
| `control_point.list` | - | `GET /control-point` | 10 s |
| `projects.list` | - | `GET /projects` | 10 s |
| `projects.files` | `project_name` | `GET /projects/{project_name}` | 10 s |
| `projects.delete` | `project_name` | `DELETE /projects/{project_name}` | 10 s |
| `projects.download` | `project_name`, optional `cleanup` (bool) | `POST /download?project_name=[&cleanup=true]` | 900 s |
| `camera.start_recording` | - | `POST /camera/start-recording` | 15 s |
| `camera.stop_recording` | - | `POST /camera/stop-recording` | 15 s |
| `camera.shutdown` | - | `POST /camera/shutdown` | 10 s |
| `camera.delay_test` | optional `count` (1-50) | `POST /camera/delay_test?count=` | 120 s |
| `camera.files` | - | `GET /camera/files` | 10 s |
| `camera.delete_file` | `name` | `DELETE /camera/files/{name}` (`all` is refused) | 10 s |
| `camera.delete_all_files` | - | `DELETE /camera/files/all` | 60 s |
| `camera.battery` | - | `GET /camera/battery` | 10 s |
| `camera.storage` | - | `GET /camera/storage` | 10 s |

The table in `gatt/protocol/commands.py` is the only way to reach an endpoint; the phone never sends a URL.
Names must not contain `..`, `\` or control characters; project names must not contain `/`.

### What the measurement commands answer

`measurement.start`:

| API answer | `error.code` / `message` | What the app should do |
|---|---|---|
| 200 `started` | - | `data.warnings` may say the storage is low: show it |
| 503 | `server_error` / `sensors not ready: camera, gnss` | `data` is not sent on errors; read `Status.detail` for the reasons. Tell the user which device is missing |
| 507 | `server_error` / `storage full` | less than ten minutes of room: ask them to plug a stick or free space |
| 400 | `client_error` / `invalid project name` | the name must be one plain folder name |
| 500 | `server_error` / `already measuring` | the measurement runs: go to the running screen |
| 500 | `server_error` / `failed to start` | something broke half way (rolled back); retry |

`measurement.stop` answers `data.warnings`: what went wrong during the measurement (a sensor that was lost, a
storage that failed). An empty list means all is well. A non-empty one means the data has a gap: tell the user.

### Control points

A control point is a place the operator measures with a known position, to be tied to exact coordinates in the
post-processing. It is measured **during a measurement**: the operator stands on the point and the app sends
`control_point.start` with a `point_id` (1-32 letters, digits, `_`, `.` or `-`); when the time is up it sends
`control_point.finish`. The device stores the **start and end time** (wall clock `start_ns`/`end_ns`, and the
`CLOCK_MONOTONIC` `start_mono_ns`/`end_mono_ns` that the PPS log is on) and the **average of the GNSS positions**
that arrived in between as the approximate coordinates (`latitude`, `longitude`, `altitude_m`, how many `positions`,
their spread `std_m`, the mean `accuracy_m` and the `fix_type`). The exact coordinates are made in the
post-processing from the raw measurements and the two times. Everything goes to `control_points.csv` in the project.

```json
{"id": 4, "ok": true, "http_status": 200, "data": {"status": "measured", "warnings": [],
 "point": {"point_id": "4001", "start_ns": 1794660400000000000, "end_ns": 1794660415000000000,
           "latitude": 50.098045, "longitude": 14.366902, "altitude_m": 289.2, "positions": 14,
           "std_m": 0.4, "accuracy_m": 3.1, "fix_type": "gnss"}}}
```

`warnings` says `no GNSS position arrived while the point was measured` when the point has times but no
coordinates (no fix): tell the user to measure it again. Errors (`client_error`, with the code in `message` text):

| API answer | Meaning |
|---|---|
| 409 `control points are measured during a measurement` | no measurement is running |
| 409 `point 4001 is still being measured: finish or cancel it first` | only one point at a time |
| 409 `point 4001 was already measured in this measurement` | a point id is used once per measurement; the app should auto-increment, and may let the user re-measure a point after `cancel` |
| 409 `no control point is being measured` | `finish` or `cancel` without a `start` |
| 400 `a point id is 1-32 letters, ...` | invalid id |

`control_point.cancel` forgets the point being measured and stores nothing. If a measurement is stopped while a
point is open, the stop finishes it and warns. The timestamps are taken when the command arrives on the device, so
the Bluetooth delay (some tens of ms) is part of them; the positions lag the true time by the receiver's output
delay (a fraction of a second). Both are fine for a point the operator stands on for several seconds.

The live state is in `Status.recording.control_points`.

## Status (every 5 s)

While a phone is connected, the GATT server asks the API for its status every `STATUS_POLL_INTERVAL` seconds and
notifies it on Status (the first one right after a phone connects):

```json
{"timestamp": 1767225600.1, "api_reachable": true, "status": "not ready",
 "sensors": {"camera": "not connected", "gnss": "ok", "imu": "ok"},
 "detail": {"camera": "no camera found"},
 "recording": {"active": true, "project": "run1", "problems": [], "started_ns": 1794660000000000000, "elapsed_s": 83.0,
               "control_points": {"count": 2, "open": {"point_id": "4003", "elapsed_s": 4.1, "positions": 4}}},
 "disk": {"location": "usb", "usb_present": true, "free_mb": 28131, "hours_left": 1223.1, "level": "ok"},
 "warnings": [],
 "events": [{"ts_ns": 1767225590000000000, "source": "camera", "kind": "lost", "detail": "stopped answering"}],
 "battery": {}, "storage": {},
 "errors": {"storage": {"code": "server_error", "message": "couldn't check storage"}}}
```

| Field | Meaning |
|---|---|
| `status` | `ready` when camera, GNSS and IMU all answer, else `not ready` |
| `sensors` | `ok` or `not connected` per device. The server reconnects them by itself, so this changes back without the app doing anything |
| `detail` | only for a device that is not ok: why (`no camera found`, `GNSS receiver does not answer ...`) |
| `recording` | `active` and `project` of a running measurement; `problems`: what is wrong right now (a sensor lost, storage failed). While it runs also `started_ns` (device wall clock), `elapsed_s` (counted on the device: show this, it stays right after a reconnect and does not depend on the clocks agreeing) and `control_points`: `count` measured and `open`, the point being measured now (`point_id`, `elapsed_s`, `positions` so far) or `null` |
| `disk` | where recordings go (`usb` or `local`), room left, and `level`: `ok`, `low` (less than 3 hours of recording fit) or `full` (less than 10 minutes, starting is refused) |
| `warnings` | sentences to show to the user: low storage, sensor lost during the measurement |
| `events` | the last three: `source` (camera, gnss, imu, server), `kind` (`lost`, `back`, `storage_error`, `error`) |
| `battery`, `storage` | the **camera's** values exactly as the camera reports them (`storage` here is the camera card, not the disk) |
| `errors` | only when something failed; if the API is down `api_reachable` is `false` and `errors.status.code` is `api_unavailable` |

## Position

The device streams its GNSS position: the receiver's NMEA sentences are parsed on the device and pushed to the
phone for every fix (about once a second).

```json
{"latitude": 50.098045, "longitude": 14.366902, "timestamp_ns": 1794660415000000000, "altitude_m": 289.2,
 "horizontal_accuracy_m": 3.3, "fix_type": "gnss", "satellites": 12, "hdop": 0.71, "speed_mps": 0.01, "course_deg": 87.5}
```

`latitude`, `longitude` and `timestamp_ns` (UTC, ns since the Unix epoch) are always there. The others are left out
when the receiver does not say: `horizontal_accuracy_m` is the 1-sigma error from NMEA GST, `fix_type` is `gnss`,
`dgnss`, `rtk_float`, `rtk_fixed`, ..., `course_deg` only while the device moves.

Instead of a position the characteristic may carry `{"error": {"code": ..., "message": ...}}`:

| `error.code` | Meaning |
|---|---|
| `no_position_fix` | nothing received yet, or the receiver **lost its fix** (the last position is stale: do not show it as current) |
| `gnss_unavailable` | `Status` says the GNSS receiver is not connected |
| `api_unavailable` | the stream to the API is down; the last good position stays readable until a new one arrives |

The position stream is open while a phone is connected and closed when the last one leaves.

## Error codes

| `error.code` | When |
|---|---|
| `malformed_request` | not UTF-8 JSON, missing `id`/`cmd`, bad packet framing |
| `unknown_command` | `cmd` not in the table |
| `invalid_params` | missing/unknown/invalid param, or the API answered 422 |
| `busy` | more than 16 commands queued |
| `api_unavailable` | the API is down, connection refused |
| `api_timeout` | the API did not answer in time |
| `client_error` | the API answered 4xx (`message` = its `status` text) |
| `server_error` | the API answered 5xx (`message` = its `status` text) |
| `device_unavailable` | 5xx while the last status poll reports a device the command needs as not connected |
| `response_too_large` | response over 64 KiB |
| `internal_error` | bug in the GATT service |

The GATT process keeps running whatever the API does.

## Connecting

- The GATT server **advertises as soon as it runs** (every boot) and keeps retrying while Bluetooth is not up.
- The adapter is set to powered, pairable and discoverable with no timeouts, and a `NoInputNoOutput` agent says
  yes to every pairing or authorization request. So any phone can connect and pair, with no PIN and no
  confirmation (`BLE_OPEN_PAIRING=0` turns that off). The characteristics need no encryption, so most phones do
  not pair at all.
- One phone at a time is the supported case. After a phone disconnects, advertising goes on. With several phones
  connected every one sees all notifications, and commands from all share one reassembly buffer.
- There is no authentication: anybody in range can start and stop a measurement and delete projects.

## Layout

| Path | Purpose |
|---|---|
| `gatt/main.py` | entry point, wires everything, handles shutdown |
| `gatt/adapter.py` | open pairing: adapter properties and the pairing agent |
| `gatt/server.py` | the only module that talks to bless (BLE transport, packet splitting) |
| `gatt/services/device.py` | Command -> API -> Response, Status publishing |
| `gatt/services/position.py` | Position characteristic content |
| `gatt/status_poller.py` | 5 s status polling task, compacts the status |
| `gatt/position_stream.py` | WebSocket subscription task (reconnects with backoff) |
| `gatt/clients.py` | connected-client tracking via BlueZ D-Bus |
| `gatt/api_client/client.py` | one reusable async httpx client, HTTP error mapping |
| `gatt/protocol/` | `uuids.py`, `commands.py` (command table), `messages.py`, `framing.py` |
| `shared/` | `models.py` (`Position`), `device.py` (id and name), used by both processes |
| `app/core/nmea.py`, `app/core/position.py` | NMEA -> Position, and the hub the API streams from |

## Configuration (environment or `.env`)

| Variable | Default |
|---|---|
| `API_BASE_URL` | `http://127.0.0.1:8000` (or `API_PORT`) |
| `POSITION_STREAM_URL` | `ws://127.0.0.1:8000/gnss/position/stream` (derived from the base URL) |
| `DEVICE_NAME` | `SLAM-<3 hex>`, at most 8 characters to fit the advertisement |
| `DEVICE_ID` | first 4 characters of `/etc/machine-id` |
| `GATT_SERVICE_UUID` | `7a1c0001-5b3e-4c8f-9d2a-6e1f0a4b8c10` |
| `BLE_OPEN_PAIRING` | `1` |
| `STATUS_POLL_INTERVAL` | `5` (seconds) |
| `API_TIMEOUT` | `10` (seconds) |
| `GATT_CHUNK_SIZE` | `100` |
| `GATT_LOG_LEVEL` | `INFO` |
| `POSITION_SIMULATOR` | `0` (API side, development only: a fake position, `fix_type: "simulated"`) |

## On the Radxa

`sudo sh deploy/install.sh` installs BlueZ and both services (`lowcost-slam-api`, `lowcost-slam-gatt`), enables
them at boot and starts them (README). Then:

```bash
systemctl status lowcost-slam-gatt
journalctl -u lowcost-slam-gatt -f
```

## Testing with a phone (nRF Connect or similar)

1. Scan: a device `SLAM-xxx` with the service `7a1c0001-...` appears.
2. Connect, request MTU 247, read `Info`.
3. Enable notifications on Response, Status and Position. Status arrives within a second, Position about every second
   once the receiver has a fix.
4. Write the bytes `037B226964223A312C22636D64223A226465766963652E737461747573227D` (hex: `0x03` and `{"id":1,"cmd":"device.status"}`): a Response notification follows.
   `{"id":1,"cmd":"device.status"}`: a Response notification follows.

Without a GNSS fix (indoors) start the API with `POSITION_SIMULATOR=1` to get a moving fake position.

## Tests

`python -m pytest tests` (see the README). The BLE transport is tested against a fake bless; the Bluetooth
hardware itself is not part of the automated tests.

## Known limits

- The BLE side was developed without Bluetooth hardware access; what the tests cannot show is how the Radxa's
  adapter and BlueZ behave: whether the advertisement of 31 bytes is accepted, whether a READ of a long value
  returns it complete on the phone, and whether the pairing agent is used. See the checklist above and report what
  differs.
- One phone at a time (see Connecting).
- No authentication.
