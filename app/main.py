import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.routers import camera, imu, gnss, common, websocket
from app.core import common as measurement, config, position, supervisors

@asynccontextmanager
async def lifespan(app: FastAPI):
    # The server comes up with whatever is connected; the rest is connected when it appears
    position.hub.bind_loop(asyncio.get_running_loop())
    open_all()
    position.feed.start()
    simulator = asyncio.create_task(position.run_simulator()) if config.POSITION_SIMULATOR else None
    yield   # The server runs int here
    if simulator:
        simulator.cancel()
    position.feed.stop()
    close_all()

def open_all():
    supervisors.start_all()

def close_all():
    # A service stop in the middle of a measurement closes it properly (files, meta.json)
    if measurement.get_recording_state()["active"]:
        measurement.stop_measurement(measurement.get_recording_state()["project"])
    supervisors.stop_all()

app = FastAPI(title="Lowcost Slam API", lifespan=lifespan)

app.include_router(camera.router, prefix="/camera", tags=["Camera"])
app.include_router(imu.router, prefix="/imu", tags=["IMU"])
app.include_router(gnss.router, prefix="/gnss", tags=["GNSS"])
app.include_router(common.router, tags=["Common"])
app.include_router(websocket.router)