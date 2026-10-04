# The /imu endpoints
from fastapi import APIRouter, responses
from app.core.imu import calibrate

router = APIRouter()

@router.post("/calibrate")
def calibrate_imu_route():
    if calibrate():
        return responses.JSONResponse(
                status_code=200,
                content={"status": "calibrated"}
            )