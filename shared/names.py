# Names the phone sends. Both processes check them, so the rule lives here.
import re

_POINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}")


def valid_point_id(text) -> bool:
    """A control point id: 1 to 32 letters, digits, '_', '.', '-', starting with a letter or digit.
    It ends up in a CSV file, so no commas, quotes or spaces."""
    return isinstance(text, str) and _POINT_ID.fullmatch(text) is not None
