# JSON request/response envelopes carried over the Command and Response characteristics
import json
from dataclasses import dataclass
from typing import Any, Optional, Union

# Error codes sent to the mobile app
MALFORMED_REQUEST = "malformed_request"
UNKNOWN_COMMAND = "unknown_command"
INVALID_PARAMS = "invalid_params"
BUSY = "busy"
API_UNAVAILABLE = "api_unavailable"        # connection refused / unreachable
API_TIMEOUT = "api_timeout"
CLIENT_ERROR = "client_error"              # FastAPI answered 4xx
SERVER_ERROR = "server_error"              # FastAPI answered 5xx
DEVICE_UNAVAILABLE = "device_unavailable"  # 5xx while /status reports a needed sensor as not connected
GNSS_UNAVAILABLE = "gnss_unavailable"      # position char: GNSS reported as not connected
NO_POSITION_FIX = "no_position_fix"        # position char: no position received yet
RESPONSE_TOO_LARGE = "response_too_large"
INTERNAL_ERROR = "internal_error"


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Request:
    id: Union[int, str]
    cmd: str
    params: dict


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def peek_id(raw: bytes) -> Optional[Union[int, str]]:
    """Best effort id extraction so even a rejected request can be answered."""
    try:
        value = json.loads(raw).get("id")
    except Exception:
        return None

    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return value
    return None


def parse_request(raw: bytes) -> Request:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProtocolError(MALFORMED_REQUEST, "request is not valid UTF-8 JSON")

    if not isinstance(body, dict):
        raise ProtocolError(MALFORMED_REQUEST, "request must be a JSON object")

    request_id = body.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)) or (
        isinstance(request_id, str) and not 0 < len(request_id) <= 64
    ):
        raise ProtocolError(MALFORMED_REQUEST, "'id' must be an integer or a string of 1-64 characters")

    command = body.get("cmd")
    if not isinstance(command, str) or not command:
        raise ProtocolError(MALFORMED_REQUEST, "'cmd' must be a non-empty string")

    params = body.get("params", {})
    if params is None:
        params = {}

    return Request(id=request_id, cmd=command, params=params)


def ok_response(request_id, http_status: int, data: Any) -> bytes:
    return dumps({"id": request_id, "ok": True, "http_status": http_status, "data": data})


def error_response(request_id, code: str, message: str, http_status: Optional[int] = None) -> bytes:
    error = {"code": code, "message": message}
    if http_status is not None:
        error["http_status"] = http_status

    return dumps({"id": request_id, "ok": False, "error": error})
