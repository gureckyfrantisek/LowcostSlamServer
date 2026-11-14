# The /control-point endpoints: measuring a control point during a measurement
from fastapi import APIRouter, responses
from app.core.control_points import ControlPointError, control_points

router = APIRouter()

# what a failed call answers: the code is stable, the app can switch on it
_STATUS = {"not_recording": 409, "already_open": 409, "duplicate_id": 409, "not_started": 409, "invalid_id": 400}


def _error(e: ControlPointError):
    return responses.JSONResponse(status_code=_STATUS.get(e.code, 400),
                                  content={"status": e.message, "code": e.code})


@router.post("/start")
def start_route(point_id: str):
    """The operator is on the point: stores the start time"""
    try:
        point = control_points.start(point_id)
    except ControlPointError as e:
        return _error(e)
    return {"status": "measuring", "point": point}


@router.post("/finish")
def finish_route():
    """Stores the end time and the average position of the time in between"""
    try:
        point = control_points.finish()
    except ControlPointError as e:
        return _error(e)
    warnings = [] if point["positions"] else ["no GNSS position arrived while the point was measured"]
    return {"status": "measured", "point": point, "warnings": warnings}


@router.post("/cancel")
def cancel_route():
    """Forgets the point being measured (nothing is stored)"""
    try:
        point = control_points.cancel()
    except ControlPointError as e:
        return _error(e)
    return {"status": "cancelled", "point": point}


@router.get("")
def list_route():
    """The points measured in the running measurement, and the one being measured"""
    return {**control_points.state(), "points": control_points.points()}
