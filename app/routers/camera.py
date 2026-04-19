# The /camera endpoints
from fastapi import APIRouter
from app.core.camera import *

router = APIRouter()

@router.post("/start-recording")
def start_recording_route():
    return start_recording()


@router.post("/stop-recording")
def stop_recording_route():
    return stop_recording()