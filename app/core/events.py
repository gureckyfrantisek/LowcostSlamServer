# What happened to the sensors and the storage, for the status and for the recording itself.
# A recording gets an events.csv: a gap in the data is explained there (sensor lost, back, disk full).
import collections
import logging
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()
_recent: collections.deque = collections.deque(maxlen=50)  # (ts_ns, source, kind, detail)
_file = None  # events.csv of the running recording


def start_recording(project_path: str):
    """Events are also written to the project folder until stop_recording()"""
    global _file
    stop_recording()
    try:
        f = open(f"{project_path}/events.csv", "w", buffering=1)
        f.write("ts_ns,source,kind,detail\n")
        with _lock:
            _file = f
    except OSError as e:
        log.warning("cannot write the events file: %s", e)


def stop_recording():
    global _file
    with _lock:
        f, _file = _file, None
    if f:
        try:
            f.close()
        except OSError:
            pass


def record(source: str, kind: str, detail: str = ""):
    """Remembers an event (and logs it). `kind` is a short word: lost, back, error, ..."""
    ts = time.time_ns()
    detail = detail.replace("\n", " ").replace(",", ";")
    log.warning("%s %s %s", source, kind, detail) if kind != "back" else log.info("%s %s %s", source, kind, detail)
    with _lock:
        _recent.append((ts, source, kind, detail))
        if _file:
            try:
                _file.write(f"{ts},{source},{kind},{detail}\n")
            except (OSError, ValueError):
                pass  # the storage itself is what failed: the log and the status still have it


def recent(count: int = 10) -> list[dict]:
    with _lock:
        items = list(_recent)[-count:]
    return [dict(ts_ns=t, source=s, kind=k, detail=d) for t, s, k, d in items]
