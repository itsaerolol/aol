import os
import subprocess
import threading
from config import SCRIPT_DIR, FILTER_AGENT_SCRIPT, FILTER_AGENT_PYTHON
from logs import log, log_filter

_proc = None
_lock = threading.Lock()


def _drain(proc):
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_filter(line)
    except Exception:
        pass


def start():
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            return
        if not os.path.exists(FILTER_AGENT_SCRIPT):
            log(f"filter agent not found: {FILTER_AGENT_SCRIPT}")
            return
        log("starting filter agent...")
        _proc = subprocess.Popen(
            [FILTER_AGENT_PYTHON, FILTER_AGENT_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=SCRIPT_DIR,
        )
        threading.Thread(target=_drain, args=(_proc,), daemon=True).start()
        log(f"filter agent running (pid {_proc.pid})")


def stop(reason: str = "C2 stop"):
    global _proc
    with _lock:
        if not _proc or _proc.poll() is not None:
            return
        log(f"stopping filter agent — {reason}")
        try:
            _proc.terminate()
            _proc.wait(timeout=10)
        except Exception as e:
            log(f"error stopping filter agent: {e}")
            _proc.kill()
        _proc = None
        log("filter agent stopped")


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def pid() -> int | None:
    return _proc.pid if running() else None
