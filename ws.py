from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio

import activity
import control_loop
import filter_controller
import vision as screenpipe
from config import BLACKOUT_START_HOUR, BLACKOUT_END_HOUR, IDLE_THRESHOLD_MINUTES
from logs import filter_log_buffer, log_buffer, sp_log_buffer

router = APIRouter()


class WSManager:
    def __init__(self):
        self._conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._conns.add(ws)

    def disconnect(self, ws: WebSocket):
        self._conns.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        for ws in self._conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        self._conns -= dead

    def has_clients(self) -> bool:
        return bool(self._conns)


manager = WSManager()


def build_status() -> dict:
    idle    = round(activity.idle_seconds())
    im, is_ = divmod(idle, 60)
    return {
        "screenpipe":      "running" if screenpipe.running()        else "stopped",
        "filter_agent":    "running" if filter_controller.running() else "stopped",
        "override_pause":  control_loop.is_overridden(),
        "blackout":        activity.in_blackout(),
        "idle_seconds":    idle,
        "idle_threshold":  IDLE_THRESHOLD_MINUTES * 60,
        "idle_display":    f"{im}m {is_}s" if im else f"{is_}s",
        "blackout_window": f"{BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00",
        "sp_pid":          screenpipe.pid(),
        "fa_pid":          filter_controller.pid(),
        "fa_next_run":     control_loop.fa_next_run_display(),
    }


async def push_loop():
    while True:
        if manager.has_clients():
            await manager.broadcast({
                "status":  build_status(),
                "c2_logs": log_buffer[-50:],
                "sp_logs": sp_log_buffer[-50:],
                "fa_logs": filter_log_buffer[-50:],
            })
        await asyncio.sleep(1)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
