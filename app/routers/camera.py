# The /camera endpoints
from fastapi import APIRouter, responses
from app.core.camera import *

router = APIRouter()

@router.post("/start-recording")
def start_recording_route():
    return start_recording()


@router.post("/stop-recording")
def stop_recording_route():
    return stop_recording()


@router.get("/files")
def get_files_route():
    return get_camera_files_list()


@router.delete(
    "/files/{name:path}",
    description="Passing 'all' as name will delete ALL files in camera."
)
def delete_files_route(
    name
):
    if name == "all":
        return delete_all()

    return delete_file(name)