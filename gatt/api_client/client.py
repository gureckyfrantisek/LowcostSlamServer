# Async HTTP client for the FastAPI app. One reusable connection pool for the whole process.
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from gatt.protocol.commands import CommandSpec, build_request
from gatt.protocol import messages

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, code: str, message: str, http_status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class ApiResult:
    http_status: int
    data: Any


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:200]}


def _error_message(response: httpx.Response, body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("status") or body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return "invalid request"
    return response.reason_phrase or f"HTTP {response.status_code}"


class ApiClient:
    def __init__(self, base_url: str, default_timeout: float, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._timeout = default_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=default_timeout,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self):
        await self._client.aclose()

    async def call(self, spec: CommandSpec, params: dict, timeout: Optional[float] = None) -> ApiResult:
        """Runs one known command. Raises ProtocolError for bad params, ApiError for HTTP trouble."""
        path, query = build_request(spec, params)

        try:
            response = await self._client.request(
                spec.method,
                path,
                params=query or None,
                timeout=timeout or spec.timeout or self._timeout,
            )
        except httpx.TimeoutException:
            raise ApiError(messages.API_TIMEOUT, f"{spec.method} {spec.path} timed out")
        except httpx.ConnectError:
            raise ApiError(messages.API_UNAVAILABLE, "cannot connect to the API")
        except httpx.HTTPError as e:
            raise ApiError(messages.API_UNAVAILABLE, f"API connection failed ({type(e).__name__})")

        body = _body(response)
        status = response.status_code

        if status < 400:
            return ApiResult(status, body)

        # /status reports "not ready" as 500 with a normal JSON body, that is data not a failure
        if spec.accept_500_body and status == 500 and isinstance(body, dict) and "sensors" in body:
            return ApiResult(status, body)

        message = _error_message(response, body)

        if status == 422:
            raise ApiError(messages.INVALID_PARAMS, message, status)
        if status < 500:
            raise ApiError(messages.CLIENT_ERROR, message, status)
        raise ApiError(messages.SERVER_ERROR, message, status)
