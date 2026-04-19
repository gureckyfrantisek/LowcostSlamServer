# GNSS connection and functions
import threading
import time

_stop_event: threading.Event = None
_thread: threading.Thread = None

# For now this just simulates data logging
def start_logging(project_path) -> int:
    global _stop_event, _thread

    _stop_event = threading.Event()

    def _log():
        file_path = f"{project_path}/gnss.txt"

        with open(file_path, "w") as f:
            f.write("timestamp_ns,data\n")
            while not _stop_event.is_set():
                f.write(f"{time.time_ns()}, testing GNSS logging ...\n")
                f.flush()
                # Simulates the step
                time.sleep(0.01)

    _thread = threading.Thread(target=_log, daemon=True)
    start_ns = time.time_ns()
    _thread.start()

    return start_ns

def stop_logging():
    global _stop_event, _thread
    if _stop_event:
        _stop_event.set()
    if _thread:
        _thread.join()
        _thread = None