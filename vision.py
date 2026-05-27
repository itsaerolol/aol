import subprocess
import threading
import psutil
from config import SCREENPIPE_CMD
from logs import log, log_sp

_proc = None


def _drain(proc):
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_sp(line)
    except (OSError, ValueError):
        pass


def start():
    global _proc
    if _proc and _proc.poll() is None:
        log(f"screenpipe already running (pid {_proc.pid}) — skipping start")
        return
    cmd = " ".join(SCREENPIPE_CMD)
    log(f"starting screenpipe: {cmd}")
    _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=True)
    threading.Thread(target=_drain, args=(_proc,), daemon=True).start()
    log(f"screenpipe running (pid {_proc.pid})")


def stop(reason: str):
    global _proc
    if not _proc:
        log("screenpipe stop called — no process tracked, nothing to stop")
        return
    code = _proc.poll()
    if code is not None:
        log(f"screenpipe stop called — process already dead (code {code})")
        _proc = None
        return
    log(f"stopping screenpipe (pid {_proc.pid}) — {reason}")
    try:
        parent = psutil.Process(_proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.NoSuchProcess:
        pass
    try:
        _proc.wait(timeout=5)
        log("screenpipe stopped")
    except subprocess.TimeoutExpired:
        log("screenpipe wait timed out after kill")
    _proc = None


def running() -> bool:
    alive = _proc is not None and _proc.poll() is None
    return alive


def pid() -> int | None:
    return _proc.pid if running() else None


def start_age() -> float | None:
    return None  # kept for control_loop compat
