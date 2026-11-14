# The /gnss endpoints
import asyncio
from fastapi import APIRouter, WebSocket, responses
from app.core.position import hub

router = APIRouter()


@router.get("/position")
def get_position_route():
    position = hub.latest

    if position is None:
        return responses.JSONResponse(
            status_code=503,
            content={"status": "no position"}
        )

    return responses.JSONResponse(
        status_code=200,
        content={"position": position.model_dump(mode="json")}
    )


@router.websocket("/position/stream")
async def position_stream_route(websocket: WebSocket):
    """Pushes {"position": {...}} for every new fix, starting with the latest.
    {"position": null} means the receiver lost its fix."""
    await websocket.accept()
    queue = hub.subscribe()

    if hub.latest is not None:
        queue.put_nowait(hub.latest)

    # receive() completes when the client goes away, so idle streams get cleaned up
    disconnect = asyncio.create_task(websocket.receive())
    try:
        while True:
            next_position = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {next_position, disconnect},
                return_when=asyncio.FIRST_COMPLETED
            )

            if disconnect in done:
                next_position.cancel()
                break

            position = next_position.result()
            await websocket.send_json(
                {"position": position.model_dump(mode="json") if position is not None else None}
            )
    except Exception as e:
        print(f"Position stream closed: {e}")
    finally:
        hub.unsubscribe(queue)
        disconnect.cancel()
