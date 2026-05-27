"""
AOL C2 — FastAPI app.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

import activity
import control_loop
import filter_controller
import vision as screenpipe
import ws
import memory_api
import dashboard
from config import C2_PORT, IDLE_THRESHOLD_MINUTES
from logs import filter_log_buffer, log, log_buffer, sp_log_buffer


def _cleanup():
    log("cleanup: stopping screenpipe")
    screenpipe.stop("C2 shutdown")
    log("cleanup: stopping filter agent")
    filter_controller.stop("C2 shutdown")
    log("cleanup: stopping input listeners")
    activity.stop()
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), 0x0003)
    except Exception:
        pass
    log("shutdown complete")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log("C2 lifespan starting")
    activity.start(log_fn=log)
    log("spawning control loop thread")
    threading.Thread(target=control_loop.run, daemon=True).start()
    asyncio.create_task(ws.push_loop())
    log(f"C2 dashboard live at http://localhost:{C2_PORT}")
    try:
        yield
    finally:
        log("C2 lifespan ending — running cleanup")
        _cleanup()


app = FastAPI(lifespan=_lifespan)
app.include_router(ws.router)
app.include_router(memory_api.router)
app.include_router(dashboard.router)


# ---------------------------------------------------------------------------
# Control routes
# ---------------------------------------------------------------------------

@app.get("/status")
def status():
    return ws.build_status()


@app.post("/start")
def api_start():
    log("API /start — clearing override, starting screenpipe")
    control_loop.set_override(False)
    screenpipe.start()
    return {"ok": True}


@app.post("/stop")
def api_stop():
    log("API /stop — setting override, stopping screenpipe + FA")
    control_loop.set_override(True)
    screenpipe.stop("manual stop")
    filter_controller.stop("manual stop")
    return {"ok": True}


@app.post("/screenpipe/stop")
def api_screenpipe_stop():
    """Stop screenpipe only — no override change. Used by FA when it self-started SP."""
    log("API /screenpipe/stop — stopping screenpipe (no override change)")
    screenpipe.stop("fa-cleanup")
    return {"ok": True}


@app.post("/resume")
def api_resume():
    log("API /resume — clearing override")
    control_loop.set_override(False)
    return {"ok": True}


@app.post("/filter/start")
def api_filter_start():
    log("API /filter/start — launching filter agent manually")
    filter_controller.start()
    return {"ok": True}


@app.post("/filter/stop")
def api_filter_stop():
    log("API /filter/stop — stopping filter agent")
    filter_controller.stop("manual stop")
    return {"ok": True}


_HERE = os.path.dirname(os.path.abspath(__file__))


@app.post("/shutdown")
def api_shutdown():
    log("API /shutdown — scheduling shutdown in 0.3s")
    def _do():
        time.sleep(0.3)
        _cleanup()
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


@app.post("/restart")
def api_restart():
    log("API /restart — scheduling restart in 0.3s")
    def _do():
        time.sleep(0.3)
        _cleanup()
        script = os.path.join(_HERE, "aol_c2.py")
        log(f"relaunching: {sys.executable} {script}")
        subprocess.Popen(
            f'cmd /c timeout /t 2 /nobreak >nul && "{sys.executable}" "{script}"',
            shell=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Log routes
# ---------------------------------------------------------------------------

@app.get("/logs")
def get_logs():
    return {"logs": log_buffer[-50:]}


@app.get("/screenpipe-logs")
def get_sp_logs():
    return {"logs": sp_log_buffer[-50:]}


@app.get("/filter-logs")
def get_filter_logs():
    return {"logs": filter_log_buffer[-50:]}
