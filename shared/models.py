# Models shared between the FastAPI app and the GATT service.
# Keep this module free of app/gatt imports so both processes can load it.
from typing import Optional
from pydantic import BaseModel, Field


class Position(BaseModel):
    """One GNSS position fix.

    latitude/longitude/timestamp_ns are always present, everything else is
    optional because not every receiver reports it.
    """
    latitude: float = Field(ge=-90, le=90, description="Degrees, WGS84")
    longitude: float = Field(ge=-180, le=180, description="Degrees, WGS84")
    timestamp_ns: int = Field(description="UTC time of the fix, ns since the Unix epoch")
    altitude_m: Optional[float] = Field(default=None, description="Meters above mean sea level")
    horizontal_accuracy_m: Optional[float] = Field(default=None, description="Meters, 1 sigma, when the receiver reports it (NMEA GST)")
    fix_type: Optional[str] = Field(default=None, description="gnss, dgnss, rtk_float, rtk_fixed, dead_reckoning, ... (from the NMEA fix quality)")
    satellites: Optional[int] = Field(default=None, ge=0, description="Satellites used in the solution")
    hdop: Optional[float] = Field(default=None, ge=0, description="Horizontal dilution of precision")
    speed_mps: Optional[float] = Field(default=None, ge=0, description="Speed over ground, m/s")
    course_deg: Optional[float] = Field(default=None, ge=0, le=360, description="Course over ground, degrees true")
