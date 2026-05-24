import time
import threading
from config import CHECK_INTERVAL_SECONDS, BLACKOUT_START_HOUR, BLACKOUT_END_HOUR, IDLE_THRESHOLD_MINUTES
from logs import log
import activity
import screenpipe
import filter_controller

_override_pause = False
_lock           = threading.Lock()


def set_override(paused: bool):
    global _override_pause
    with _lock:
        _override_pause = paused


def is_overridden() -> bool:
    return _override_pause


def run():
    log("AOL controller active")
    log(
        f"idle threshold: {IDLE_THRESHOLD_MINUTES}m | "
        f"blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00"
    )

    if not activity.in_blackout():
        screenpipe.start()
        filter_controller.start()
    else:
        log("in blackout window, screenpipe + filter agent not started")

    last_check = activity.last_activity_time()

    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)

        if _override_pause:
            if screenpipe.running():
                screenpipe.stop("manual override")
            if filter_controller.running():
                filter_controller.stop("manual override")
            continue

        idle     = activity.idle_seconds()
        blackout = activity.in_blackout()
        t        = activity.last_activity_time()
        moved    = t > last_check
        last_check = t

        log(
            f"{'moved' if moved else 'idle'} {idle:.0f}s | "
            f"blackout={blackout} | sp={screenpipe.running()} | fa={filter_controller.running()}"
        )

        if blackout:
            if screenpipe.running():
                screenpipe.stop("blackout window")
            if filter_controller.running():
                filter_controller.stop("blackout window")
            continue

        if idle > IDLE_THRESHOLD_MINUTES * 60:
            if screenpipe.running():
                screenpipe.stop(f"idle {idle/60:.1f}m")
            if filter_controller.running():
                filter_controller.stop(f"idle {idle/60:.1f}m")
        else:
            if not screenpipe.running():
                log("activity detected — resuming screenpipe + filter agent")
                screenpipe.start()
                filter_controller.start()
