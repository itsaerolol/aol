import subprocess
import threading
import psutil
from config import SCREENPIPE_CMD
from logs import log, log_sp

_proc = None
_lock = threading.Lock()


def _drain(proc):
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_sp(line)
    except Exception:
        pass


def start():
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            return
        log("starting screenpipe...")
        _proc = subprocess.Popen(
            SCREENPIPE_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=True,
        )
        threading.Thread(target=_drain, args=(_proc,), daemon=True).start()
        log(f"screenpipe running (pid {_proc.pid})")


def stop(reason: str):
    global _proc
    with _lock:
        if not _proc or _proc.poll() is not None:
            return
        log(f"stopping screenpipe — {reason}")
        try:
            parent   = psutil.Process(_proc.pid)
            children = parent.children(recursive=True)
            for c in children:
                c.terminate()
            parent.terminate()
            psutil.wait_procs(children + [parent], timeout=5)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            log(f"error killing screenpipe tree: {e}")
            _proc.kill()
        _proc = None
        log("screenpipe stopped")


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def pid() -> int | None:
    return _proc.pid if running() else None
