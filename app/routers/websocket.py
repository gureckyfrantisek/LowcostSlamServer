import asyncio
import queue
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core import gnss, imu

router = APIRouter()

@router.websocket("/ws/gnss")
async def gnss_stream(websocket: WebSocket):
    await websocket.accept()

    q = gnss.subscribe(kinds=[gnss.NMEA], maxsize=100)
    try:
        while True:
            try:
                # q.get blocks, so run it off the event loop
                ts, _, line = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                continue
            await websocket.send_text(f"{ts},{line.decode(errors='replace').rstrip()}")
    except WebSocketDisconnect:
        pass
    finally:
        gnss.unsubscribe(q)


@router.websocket("/ws/imu")
async def imu_stream(websocket: WebSocket):
    await websocket.accept()

    q = imu.subscribe(maxsize=100)
    try:
        while True:
            try:
                # q.get blocks, so run it off the event loop
                ts, line = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                continue
            await websocket.send_text(f"{ts},{line.decode(errors='replace').rstrip()}")
    except WebSocketDisconnect:
        pass
    finally:
        imu.unsubscribe(q)