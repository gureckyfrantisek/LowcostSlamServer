# GATT service configuration, all overridable through environment variables / .env
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from gatt.protocol import uuids
from shared.device import device_id, device_name


@dataclass(frozen=True)
class Config:
    api_base_url: str
    position_stream_url: str
    device_name: str
    device_id: str
    service_uuid: str
    status_poll_interval: float
    api_timeout: float
    chunk_size: int
    log_level: str
    open_pairing: bool


def _default_stream_url(base_url: str) -> str:
    ws_base = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return ws_base.rstrip("/") + "/gnss/position/stream"


def load() -> Config:
    base_url = os.getenv("API_BASE_URL") or os.getenv("FASTAPI_BASE_URL") or \
        f"http://127.0.0.1:{os.getenv('API_PORT', '8000')}"
    base_url = base_url.rstrip("/")

    return Config(
        api_base_url=base_url,
        position_stream_url=os.getenv("POSITION_STREAM_URL") or _default_stream_url(base_url),
        device_name=device_name(),
        device_id=device_id(),
        service_uuid=(os.getenv("GATT_SERVICE_UUID") or uuids.DEVICE_SERVICE_UUID).lower(),
        status_poll_interval=float(os.getenv("STATUS_POLL_INTERVAL", "5")),
        api_timeout=float(os.getenv("API_TIMEOUT", "10")),
        # Payload bytes per BLE packet (+1 header byte). Must fit in the negotiated ATT MTU - 4.
        chunk_size=int(os.getenv("GATT_CHUNK_SIZE", "100")),
        log_level=os.getenv("GATT_LOG_LEVEL", "INFO").upper(),
        # Accept every connection and pairing request without a PIN, from any phone
        open_pairing=os.getenv("BLE_OPEN_PAIRING", "1").lower() in ("1", "true", "yes"),
    )
