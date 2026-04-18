# The /camera endpoints
from fastapi import APIRouter
from app.core.camera import *

router = APIRouter()

@router.post("/start-recording")
async def start_recording_route():
    return start_recording()


@router.post("/stop-recording")
async def stop_recording_route():
    return stop_recording()