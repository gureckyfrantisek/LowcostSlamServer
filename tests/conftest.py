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


@pytest.fixture(autouse=True)
def simulated_hardware(monkeypatch, tmp_path):
    """No real GPIO edges, no real PM QoS request, a fast sensor so the polling reader is quick"""
    from app.core import config, imu
    from app.core.timing import PmQos
    monkeypatch.setattr(imu, "open_edges", lambda: (None, "simulated"))
    monkeypatch.setattr(imu, "_qos", PmQos(str(tmp_path / "cpu_dma_latency"), 10))
    monkeypatch.setattr(config, "IMU_I2C_HZ", 400)
    from app.core import events, gnss
    monkeypatch.setattr(gnss, "RAWX_SILENT_S", 1e9)   # the simulated receiver sends no RAWX of its own
    imu._stats.clear()                                  # counters run on across a test otherwise
    gnss._stats.clear()
    gnss._stats["frames"] = 0
    events._recent.clear()


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
