# The / endpoints for common actions
from fastapi import APIRouter, HTTPException
from app.core.common import start_measurement, stop_measurement, download_all_data

router = APIRouter()

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
    return {"status": "stopped"}

@router.post("/download-all/{project_name}")
def download_all_route(
    project_name,
    cleanup=False
):
    return download_all_data(project_name, cleanup)