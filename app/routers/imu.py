# The /imu endpoints
from fastapi import APIRouter, Query, responses
from app.core import imu

router = APIRouter()

@router.get("/calibrate")
def calibration_steps_route():
    """The calibration steps with the device placement for each, and which are done"""
    return responses.JSONResponse(
        status_code=200,
        content={
            **imu.calibration_status(),
            "steps": imu.list_calibration_steps(),
        }
    )

@router.post("/calibrate/{step}")
def calibrate_step_route(step: int, duration: float | None = Query(None, ge=5, le=300)):
    """Runs one calibration step (blocking). Steps are independent and can be repeated.

    duration: seconds, only for the moving (magnetometer) step"""
    if imu.get_calibration_step(step) is None:
        return responses.JSONResponse(
            status_code=404,
            content={"status": "unknown step", "steps": [s["number"] for s in imu.CALIBRATION_STEPS]}
        )

    try:
        result = imu.run_calibration_step(step, duration)
    except imu.CalibrationUnavailable as e:
        return responses.JSONResponse(
            status_code=409,
            content={"status": "unavailable", "reason": str(e)}
        )
    except imu.StepFailed as e:
        return responses.JSONResponse(
            status_code=422,
            content={"status": "failed", "step": step, "reason": str(e)}
        )

    return responses.JSONResponse(
        status_code=200,
        content={"status": "ok", **result}
    )
