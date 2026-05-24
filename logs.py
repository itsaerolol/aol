from datetime import datetime
from config import LOG_CAP

log_buffer        = []
sp_log_buffer     = []
filter_log_buffer = []


def _append(buf: list, entry: str):
    buf.append(entry)
    if len(buf) > LOG_CAP:
        buf.pop(0)


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str):
    entry = f"[{timestamp()}] {msg}"
    print(entry, flush=True)
    _append(log_buffer, entry)


def log_sp(line: str):
    _append(sp_log_buffer, f"[{timestamp()}] {line}")


def log_filter(line: str):
    _append(filter_log_buffer, f"[{timestamp()}] {line}")
