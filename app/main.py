from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.routers import camera, imu, gnss, common, websocket
from app.core.camera import open_camera, close_camera
from app.core.gnss import open_gnss_port, close_gnss_port
from app.core.imu import open_imu_port, close_imu_port

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opening all needs to be more robust
    open_all()
    yield   # The server runs int here
    close_all()

def open_all():
    open_camera()
    open_gnss_port()
    open_imu_port()

def close_all():
    close_camera()
    close_gnss_port()
    close_imu_port()

app = FastAPI(title="Lowcost Slam API", lifespan=lifespan)

app.include_router(camera.router, prefix="/camera", tags=["Camera"])
app.include_router(imu.router, prefix="/imu", tags=["IMU"])
app.include_router(gnss.router, prefix="/gnss", tags=["GNSS"])
app.include_router(common.router, tags=["Common"])
app.include_router(websocket.router)