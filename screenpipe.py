import subprocess
import threading
import time
import psutil
from config import SCREENPIPE_CMD
from logs import log, log_sp

_proc       = None
_lock       = threading.Lock()
_start_time: float | None = None   # time.time() of last start() call


def _drain(proc):
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_sp(line)
    except Exception:
        pass


def _monitor(proc):
    """Wait for process death and log exit code so crashes are visible."""
    proc.wait()
    code = proc.returncode
    if code is not None and code != 0:
        log(f"screenpipe exited unexpectedly (code {code})")
    else:
        log(f"screenpipe process ended (code {code})")


def _kill_orphaned():
    """Kill any running process whose command line contains 'screenpipe'.
    Cleans up leftover processes from previous C2 sessions that weren't
    shut down cleanly, which would cause port-3030 conflicts."""
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if "screenpipe" in cmdline and proc.pid != _proc_pid():
                    children = proc.children(recursive=True)
                    for c in children:
                        c.terminate()
                    proc.terminate()
                    psutil.wait_procs(children + [proc], timeout=3)
                    log(f"killed orphaned screenpipe (pid {proc.pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        log(f"orphan cleanup error: {e}")


def _proc_pid() -> int | None:
    return _proc.pid if _proc else None


def start_age() -> float | None:
    """Seconds since last start() call, or None if never started."""
    return (time.time() - _start_time) if _start_time is not None else None


def start():
    global _proc, _start_time
    with _lock:
        if _proc and _proc.poll() is None:
            return
        log("starting screenpipe...")
        _kill_orphaned()
        _proc = subprocess.Popen(
            SCREENPIPE_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=True,
        )
        _start_time = time.time()
        threading.Thread(target=_drain,   args=(_proc,), daemon=True).start()
        threading.Thread(target=_monitor, args=(_proc,), daemon=True).start()
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
