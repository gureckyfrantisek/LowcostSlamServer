import asyncio
import queue
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.gnss import subscribe, unsubscribe, NMEA

router = APIRouter()

@router.websocket("/ws/gnss")
async def gnss_stream(websocket: WebSocket):
    await websocket.accept()

    q = subscribe(kinds=[NMEA], maxsize=100)
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
        unsubscribe(q)
