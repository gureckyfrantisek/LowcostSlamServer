from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.routers import camera, imu, gnss, common
from app.core.camera import open_camera, close_camera

@asynccontextmanager
async def lifespan(app: FastAPI):
    open_camera()
    yield   # The server runs int here
    close_camera()

app = FastAPI(title="Lowcost Slam API", lifespan=lifespan)

app.include_router(camera.router, prefix="/camera", tags=["Camera"])
app.include_router(imu.router, prefix="/imu", tags=["IMU"])
app.include_router(gnss.router, prefix="/gnss", tags=["GNSS"])
app.include_router(common.router, tags=["Common"])