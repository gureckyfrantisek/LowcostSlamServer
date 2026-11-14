# The / endpoints for common actions
from fastapi import APIRouter, HTTPException, responses
from app.core.common import *

router = APIRouter()

@router.get("/status")
def get_status_route():
    status = get_status()

    camera_ok = not (status & 4)
    gnss_ok   = not (status & 2)
    imu_ok    = not (status & 1)

    all_connected = camera_ok and gnss_ok and imu_ok
    storage_status = storage.status()
    recording = get_recording_state()
    warnings = recording["problems"] + ([storage_status["message"]] if storage_status["level"] != "ok" else [])

    return responses.JSONResponse(
        status_code=200 if all_connected else 500,
        content={
            "status": "ready" if all_connected else "not ready",
            "sensors": {
                "camera": "ok" if camera_ok else "not connected",
                "gnss":   "ok" if gnss_ok   else "not connected",
                "imu":    "ok" if imu_ok     else "not connected",
            },
            "details": get_sensor_states(),
            "recording": recording,
            "storage": storage_status,
            "warnings": warnings,
            "events": events.recent(10),
        }
    )

@router.post("/start")
def start_measurement_route(project_name):
    response = start_measurement(project_name)
    
    match response:
        case True:
            storage_status = storage.status()
            return responses.JSONResponse(
                status_code=200,
                content={
                    "status": "started",
                    "storage": storage_status,
                    "warnings": [storage_status["message"]] if storage_status["level"] != "ok" else [],
                }
            )
        
        case 1:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "already measuring"}
            )
        
        case 2:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "failed to start camera"}
            )

        case 4:
            return responses.JSONResponse(
                status_code=400,
                content={"status": "invalid project name"}
            )

        case 5:
            return responses.JSONResponse(
                status_code=507,
                content={"status": "storage full", "storage": storage.status()}
            )

        case 7:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "failed to start", "reason": get_start_error()}
            )

        case 6:
            missing = {name: state["detail"] for name, state in get_sensor_states().items() if state["state"] != "ok"}
            return responses.JSONResponse(
                status_code=503,
                content={"status": "sensors not ready: " + ", ".join(missing), "missing": missing}
            )

@router.post("/stop")
def stop_measurement_route(project_name):
    problems = stop_measurement(project_name)
    if problems is False:
        return responses.JSONResponse(
            status_code=500,
            content={"status": "couldn't stop"}
        )

    # Stopped, but something went wrong during the measurement: the app should tell the user
    return responses.JSONResponse(
        status_code=200,
        content={"status": "stopped", "warnings": problems}
    )

@router.get("/projects")
def get_projects_route():
    projects = get_projects()

    if not projects:
        return responses.JSONResponse(
            status_code=500,
            content={"status": "no local projects"}
        )

    return responses.JSONResponse(
        status_code=200,
        content={"projects": projects}
    )


@router.get("/projects/{project_name}")
def get_project_files_route(project_name):
    response = get_project_files(project_name)

    match response:
        case 1:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "no local projects"}
            )
        
        case 2:
            return responses.JSONResponse(
                status_code=400,
                content={"status": "invalid project"}
            )
        case 3:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "directory list failed"}
            )
        
        case _:
            return responses.JSONResponse(
                status_code=200,
                content={"project_files": response}
            )


@router.delete("/projects/{project_name}")
def delete_project_files_route(project_name):
    response = delete_project_files(project_name)

    match response:
        case True:
            return responses.JSONResponse(
                status_code=200,
                content={"status": "deleted"}
            )
        
        case 1:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "no local projects"}
            )

        case 2:
            return responses.JSONResponse(
                status_code=400,
                content={"status": "invalid project"}
            )

        case 3:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "directory deletion failed"}
            )


@router.post("/download")
def download_project_route(
    project_name,
    cleanup: bool = False
):
    response = download_project_data(project_name, cleanup)

    match response:
        case True:
            return responses.JSONResponse(
                status_code=200,
                content={"status": "downloaded"}
            )
        
        case 1:
            return responses.JSONResponse(
                status_code=400,
                content={"status": "invalid project"}
            )
        
        case 2:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "USB unavailable"}
            )
        
        case 3:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "copy failed"}
            )
        
        case 4:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "camera download failed"}
            )
        
        case 5:
            return responses.JSONResponse(
                status_code=500,
                content={"status": "cleanup failed"}
            )