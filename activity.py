import time
from datetime import datetime
from pynput import mouse, keyboard
from config import BLACKOUT_START_HOUR, BLACKOUT_END_HOUR, IDLE_THRESHOLD_MINUTES

_last_activity  = time.time()
_mouse_listener = None
_kb_listener    = None
_log_fn         = None


def _on_activity(*_):
    global _last_activity
    _last_activity = time.time()


def last_activity_time() -> float:
    return _last_activity


def idle_seconds() -> float:
    return time.time() - _last_activity


def is_idle() -> bool:
    return idle_seconds() > IDLE_THRESHOLD_MINUTES * 60


def in_blackout() -> bool:
    h = datetime.now().hour
    result = BLACKOUT_START_HOUR <= h < BLACKOUT_END_HOUR
    return result


def start(log_fn=None):
    global _mouse_listener, _kb_listener, _log_fn
    _log_fn = log_fn
    _mouse_listener = mouse.Listener(on_click=_on_activity, daemon=True)
    _kb_listener    = keyboard.Listener(on_press=_on_activity, daemon=True)
    _mouse_listener.start()
    _kb_listener.start()
    if log_fn:
        log_fn(
            f"input listeners active | "
            f"idle threshold: {IDLE_THRESHOLD_MINUTES}m | "
            f"blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00"
        )


def stop():
    try:
        if _mouse_listener:
            _mouse_listener.stop()
        if _kb_listener:
            _kb_listener.stop()
        if _log_fn:
            _log_fn("input listeners stopped")
    except Exception as e:
        if _log_fn:
            _log_fn(f"input listener stop error: {e}")
