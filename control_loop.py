import datetime
import json
import os
import time
import threading
from config import (
    CHECK_INTERVAL_SECONDS, BLACKOUT_START_HOUR, BLACKOUT_END_HOUR,
    IDLE_THRESHOLD_MINUTES, FILTER_AGENT_HOUR, FILTER_AGENT_MINUTE,
)
from logs import log
import activity
import screenpipe
import filter_controller

_override_pause    = False
_lock              = threading.Lock()
_fa_last_run_date: datetime.date | None = None

# Don't attempt to restart screenpipe within this many seconds of the last
# start() call — gives it time to initialise and avoids hammering a crashing process.
SP_GRACE_SECONDS = 90


def _fa_due() -> bool:
    """True if it is at or past 21:30 and FA has not already run today."""
    now = datetime.datetime.now()
    if now.hour < FILTER_AGENT_HOUR:
        return False
    if now.hour == FILTER_AGENT_HOUR and now.minute < FILTER_AGENT_MINUTE:
        return False
    return _fa_last_run_date != now.date()


def _init_fa_schedule():
    """Restore last-run date from metadata so C2 restarts don't double-fire FA."""
    global _fa_last_run_date
    try:
        meta_path = os.path.join(os.path.expanduser("~/aol-memory"), "filter_metadata.json")
        with open(meta_path) as f:
            meta = json.load(f)
        last = meta.get("last_run_date")
        if last:
            _fa_last_run_date = datetime.date.fromisoformat(last)
            log(f"FA schedule restored: last run {last}")
    except Exception:
        pass  # first run or metadata missing — FA will fire at next 21:30


def set_override(paused: bool):
    global _override_pause
    with _lock:
        _override_pause = paused


def is_overridden() -> bool:
    return _override_pause


def run():
    global _fa_last_run_date
    log("AOL controller active")
    log(
        f"idle threshold: {IDLE_THRESHOLD_MINUTES}m | "
        f"blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00 | "
        f"FA schedule: {FILTER_AGENT_HOUR:02d}:{FILTER_AGENT_MINUTE:02d} daily"
    )

    _init_fa_schedule()

    if not activity.in_blackout():
        screenpipe.start()
    else:
        log("in blackout window, screenpipe not started")

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
        else:
            if not screenpipe.running():
                age = screenpipe.start_age()
                if age is not None and age < SP_GRACE_SECONDS:
                    log(f"screenpipe not yet up — grace period ({SP_GRACE_SECONDS - int(age)}s left)")
                else:
                    log("activity detected — resuming screenpipe")
                    screenpipe.start()

        # Daily FA trigger — fires at 21:30 regardless of idle state
        if _fa_due():
            log(f"scheduled daily FA run at {FILTER_AGENT_HOUR:02d}:{FILTER_AGENT_MINUTE:02d} — starting filter agent")
            filter_controller.start()
            _fa_last_run_date = datetime.datetime.now().date()
