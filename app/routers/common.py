# The / endpoints for common actions
from fastapi import APIRouter, HTTPException, responses
from app.core.common import get_status, start_measurement, stop_measurement, download_all_data

router = APIRouter()

@router.get("/status")
def get_status_route():
    status = get_status()

    camera_ok = not (status & 4)
    gnss_ok   = not (status & 2)
    imu_ok    = not (status & 1)

    all_connected = camera_ok and gnss_ok and imu_ok

    return responses.JSONResponse(
        status_code=200 if all_connected else 500,
        content={
            "status": "ready" if all_connected else "not ready",
            "sensors": {
                "camera": "ok" if camera_ok else "not connected",
                "gnss":   "ok" if gnss_ok   else "not connected",
                "imu":    "ok" if imu_ok     else "not connected",
            }
        }
    )

@router.post("/start/{project_name}")
def start_measurement_route(project_name: str):
    result = start_measurement(project_name)
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to start measurement")
    
    return {
        "name": result.name,
        "t0_ns": result.t0_ns,
        "camera_media_time_at_t0_ms": result.camera_media_time_at_t0_ms,
        "gnss_start_offset_ns": result.gnss_start_offset_ns,
        "imu_start_offset_ns": result.imu_start_offset_ns,
    }

@router.post("/stop")
def stop_measurement_route():
    if not stop_measurement():
        raise HTTPException(status_code=400, detail="No active measurement")
    return False

@router.post("/download-all/{project_name}")
def download_all_route(
    project_name,
    cleanup=False
):
    return download_all_data(project_name, cleanup)