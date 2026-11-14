# The ONLY BLE command -> FastAPI endpoint mapping. Nothing outside this table
# can be reached over BLE, and the mobile app never supplies a URL.
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from gatt.protocol.messages import ProtocolError, INVALID_PARAMS
from shared.names import valid_point_id

SENSORS_ALL = ("camera", "gnss", "imu")


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                    # project | camera_path | int | bool
    required: bool = True
    location: str = "query"      # query | path
    min_value: Optional[int] = None
    max_value: Optional[int] = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    method: str
    path: str                    # may contain {param} placeholders for path params
    params: tuple[Param, ...] = ()
    timeout: Optional[float] = None       # seconds, None = client default
    accept_500_body: bool = False         # /status answers 500 + JSON body when "not ready"
    sensors: tuple[str, ...] = ()         # sensors this command needs, for device_unavailable


def _text(name: str, value: Any, *, allow_slash: bool) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 255:
        raise ProtocolError(INVALID_PARAMS, f"'{name}' must be a string of 1-255 characters")

    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise ProtocolError(INVALID_PARAMS, f"'{name}' contains forbidden characters")

    # The API joins these into filesystem paths, so block traversal here too
    segments = value.split("/")
    if ".." in segments or (not allow_slash and (len(segments) > 1 or value == ".")):
        raise ProtocolError(INVALID_PARAMS, f"'{name}' is not a valid name")

    return value


def _coerce(spec: CommandSpec, param: Param, value: Any) -> str:
    """Validates one value and returns it ready for the URL (path or query)."""
    match param.kind:
        case "project":
            return _text(param.name, value, allow_slash=False)

        case "camera_path":
            text = _text(param.name, value, allow_slash=True)
            # 'all' wipes the camera on the API side, it has its own explicit command
            if text == "all":
                raise ProtocolError(INVALID_PARAMS, "use camera.delete_all_files to delete everything")
            return text

        case "point_id":
            if not valid_point_id(value):
                raise ProtocolError(INVALID_PARAMS, f"'{param.name}' must be 1-32 letters, digits, '_', '.' or '-'")
            return value

        case "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProtocolError(INVALID_PARAMS, f"'{param.name}' must be an integer")
            if param.min_value is not None and value < param.min_value:
                raise ProtocolError(INVALID_PARAMS, f"'{param.name}' must be >= {param.min_value}")
            if param.max_value is not None and value > param.max_value:
                raise ProtocolError(INVALID_PARAMS, f"'{param.name}' must be <= {param.max_value}")
            return str(value)

        case "bool":
            if not isinstance(value, bool):
                raise ProtocolError(INVALID_PARAMS, f"'{param.name}' must be a boolean")
            return "true" if value else "false"

    raise ValueError(f"unknown param kind {param.kind}")


def build_request(spec: CommandSpec, params: Any) -> tuple[str, dict]:
    """Validates BLE params and returns (url path, query dict) for the HTTP call."""
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, "'params' must be an object")

    unknown = set(params) - {p.name for p in spec.params}
    if unknown:
        raise ProtocolError(INVALID_PARAMS, f"unknown parameter(s): {', '.join(sorted(unknown))}")

    path_values = {}
    query = {}

    for param in spec.params:
        value = params.get(param.name)
        if value is None:
            if param.required:
                raise ProtocolError(INVALID_PARAMS, f"missing parameter '{param.name}'")
            continue

        text = _coerce(spec, param, value)

        if param.location == "path":
            safe = "/" if param.kind == "camera_path" else ""
            path_values[param.name] = quote(text, safe=safe)
        elif param.kind == "bool":
            # FastAPI reads unannotated query params as strings, so "false" would be
            # truthy there. Only ever send the flag when it is true.
            if text == "true":
                query[param.name] = text
        else:
            query[param.name] = text

    return spec.path.format(**path_values), query


def _specs(*specs: CommandSpec) -> dict[str, CommandSpec]:
    return {spec.name: spec for spec in specs}


COMMANDS = _specs(
    # Common
    CommandSpec("device.status", "GET", "/status", accept_500_body=True),
    CommandSpec("device.info", "GET", "/info"),
    CommandSpec("measurement.start", "POST", "/start",
                params=(Param("project_name", "project"),), timeout=30, sensors=SENSORS_ALL),
    CommandSpec("measurement.stop", "POST", "/stop",
                params=(Param("project_name", "project"),), timeout=60, sensors=SENSORS_ALL),
    # Control points, during a measurement: start when the operator is on the point, finish when done
    CommandSpec("control_point.start", "POST", "/control-point/start",
                params=(Param("point_id", "point_id"),), sensors=("gnss",)),
    CommandSpec("control_point.finish", "POST", "/control-point/finish"),
    CommandSpec("control_point.cancel", "POST", "/control-point/cancel"),
    CommandSpec("control_point.list", "GET", "/control-point"),
    CommandSpec("projects.list", "GET", "/projects"),
    CommandSpec("projects.files", "GET", "/projects/{project_name}",
                params=(Param("project_name", "project", location="path"),)),
    CommandSpec("projects.delete", "DELETE", "/projects/{project_name}",
                params=(Param("project_name", "project", location="path"),)),
    CommandSpec("projects.download", "POST", "/download",
                params=(Param("project_name", "project"), Param("cleanup", "bool", required=False)),
                timeout=900, sensors=("camera",)),

    # Camera
    CommandSpec("camera.start_recording", "POST", "/camera/start-recording", timeout=15, sensors=("camera",)),
    CommandSpec("camera.stop_recording", "POST", "/camera/stop-recording", timeout=15, sensors=("camera",)),
    CommandSpec("camera.shutdown", "POST", "/camera/shutdown", sensors=("camera",)),
    CommandSpec("camera.delay_test", "POST", "/camera/delay_test",
                params=(Param("count", "int", required=False, min_value=1, max_value=50),),
                timeout=120, sensors=("camera",)),
    CommandSpec("camera.files", "GET", "/camera/files", sensors=("camera",)),
    CommandSpec("camera.delete_file", "DELETE", "/camera/files/{name}",
                params=(Param("name", "camera_path", location="path"),), sensors=("camera",)),
    CommandSpec("camera.delete_all_files", "DELETE", "/camera/files/all", timeout=60, sensors=("camera",)),
    CommandSpec("camera.info", "GET", "/camera/info"),
    CommandSpec("camera.battery", "GET", "/camera/battery", sensors=("camera",)),
    CommandSpec("camera.storage", "GET", "/camera/storage", sensors=("camera",)),
)
