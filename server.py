"""
AOL C2 — FastAPI app.
Mounts routers from ws, memory_api, html.
Lifespan starts all subsystems.
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
import screenpipe
import ws
import memory_api
import dashboard
from config import C2_PORT, IDLE_THRESHOLD_MINUTES
from logs import filter_log_buffer, log, log_buffer, sp_log_buffer


def _cleanup():
    screenpipe.stop("C2 shutdown")
    filter_controller.stop("C2 shutdown")
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
    activity.start(log_fn=log)
    threading.Thread(target=control_loop.run, daemon=True).start()
    asyncio.create_task(ws.push_loop())
    log(f"C2 dashboard live at http://localhost:{C2_PORT}")
    try:
        yield
    finally:
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
    control_loop.set_override(False)
    screenpipe.start()
    filter_controller.start()
    return {"ok": True}


@app.post("/stop")
def api_stop():
    control_loop.set_override(True)
    screenpipe.stop("manual stop")
    filter_controller.stop("manual stop")
    return {"ok": True}


@app.post("/resume")
def api_resume():
    control_loop.set_override(False)
    log("override cleared — resuming auto-control")
    return {"ok": True}


@app.post("/filter/start")
def api_filter_start():
    filter_controller.start()
    return {"ok": True}


@app.post("/filter/stop")
def api_filter_stop():
    filter_controller.stop("manual stop")
    return {"ok": True}


_HERE = os.path.dirname(os.path.abspath(__file__))


@app.post("/shutdown")
def api_shutdown():
    def _do():
        time.sleep(0.3)   # let the HTTP response escape
        _cleanup()
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


@app.post("/restart")
def api_restart():
    def _do():
        time.sleep(0.3)
        _cleanup()
        script = os.path.join(_HERE, "aol_c2.py")
        # Wait 2 s (for the port to free) then relaunch — fully detached so it
        # survives after this process exits.
        subprocess.Popen(
            f'cmd /c timeout /t 2 /nobreak >nul && "{sys.executable}" "{script}"',
            shell=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Log routes (kept for curl / external tooling)
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
