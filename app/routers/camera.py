# The /camera endpoints
from fastapi import APIRouter, responses
from app.core.camera import *

router = APIRouter()

@router.post("/start-recording")
def start_recording_route():
    if start_recording():
        return responses.JSONResponse(
            status_code=200,
            content={"status": "started"}
        )
    
    return responses.JSONResponse(
        status_code=500,
        content={"status": "couldn't start"}
    )


@router.post("/stop-recording")
def stop_recording_route():
    if stop_recording():
        return responses.JSONResponse(
            status_code=200,
            content={"status": "stopped"}
        )
    
    return responses.JSONResponse(
        status_code=500,
        content={"status": "couldn't stop"}
    )


@router.get("/files")
def get_files_route():
    response = get_camera_files_list()

    print(response)
    if response == 1:
        return responses.JSONResponse(
            status_code=500,
            content={"status": "couldn't get files"}
        )
    
    return responses.JSONResponse(
        status_code=200,
        content={"files": response}
    )


@router.delete(
    "/files/{name:path}",
    description="Passing 'all' as name will delete ALL files in camera."
)
def delete_files_route(
    name
):
    if name == "all":
        response = delete_all()
    else:
        response = delete_file(name)

    if response:
        return responses.JSONResponse(
            status_code=200,
            content={"status": "deleted"}
        )
    
    return responses.JSONResponse(
        status_code=500,
        content={"status": "couldn't delete"}
    )