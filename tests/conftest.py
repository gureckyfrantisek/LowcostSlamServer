import sys
import types

import pytest

# The camera SDK is a compiled module that only exists on the Radxa
try:
    from sdk import camerasdk  # noqa: F401
except Exception:
    sdk = types.ModuleType("sdk")
    sdk.camerasdk = types.ModuleType("sdk.camerasdk")
    sys.modules["sdk"], sys.modules["sdk.camerasdk"] = sdk, sdk.camerasdk


@pytest.fixture
def bus(monkeypatch):
    """A fake I2C bus behind app.core.i2c"""
    from app.core import i2c
    from tests.fakes import FakeBus
    fake = FakeBus()
    monkeypatch.setattr(i2c, "SMBus", lambda *_: fake)
    yield fake
    from app.core import gnss, imu
    imu.close_imu_port()
    gnss.close_gnss_port()
    assert i2c.open_buses() == 0, "a sensor kept an I2C bus open"
