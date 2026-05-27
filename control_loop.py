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
import vision as screenpipe
import filter_controller

_override_pause    = False
_lock              = threading.Lock()
_fa_last_run_date: datetime.date | None = None

SP_GRACE_SECONDS = 90


def _fa_due() -> bool:
    now = datetime.datetime.now()
    if now.hour < FILTER_AGENT_HOUR:
        return False
    if now.hour == FILTER_AGENT_HOUR and now.minute < FILTER_AGENT_MINUTE:
        return False
    due = _fa_last_run_date != now.date()
    return due


def _init_fa_schedule():
    global _fa_last_run_date
    meta_path = os.path.join(os.path.expanduser("~/aol-memory"), "filter_metadata.json")
    log(f"reading FA metadata: {meta_path}")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        last = meta.get("last_run_date")
        if last:
            _fa_last_run_date = datetime.date.fromisoformat(last)
            log(f"FA schedule restored: last run {last} — next run today at {FILTER_AGENT_HOUR:02d}:{FILTER_AGENT_MINUTE:02d} if not yet fired")
        else:
            log("FA metadata has no last_run_date — FA will fire at next 21:30")
    except FileNotFoundError:
        log("FA metadata not found — first run, FA will fire at next 21:30")
    except Exception as e:
        log(f"FA metadata read error: {e} — FA will fire at next 21:30")


def set_override(paused: bool):
    global _override_pause
    with _lock:
        prev = _override_pause
        _override_pause = paused
        if prev != paused:
            log(f"override {'ENABLED — auto-control paused' if paused else 'CLEARED — auto-control resumed'}")


def is_overridden() -> bool:
    return _override_pause


def fa_next_run_display() -> str:
    now = datetime.datetime.now()
    fa_today = now.replace(hour=FILTER_AGENT_HOUR, minute=FILTER_AGENT_MINUTE, second=0, microsecond=0)
    already_ran = _fa_last_run_date == now.date()
    if not already_ran and now < fa_today:
        delta = fa_today - now
    else:
        delta = (fa_today + datetime.timedelta(days=1)) - now
    h = int(delta.total_seconds() // 3600)
    m = int((delta.total_seconds() % 3600) // 60)
    return f"in {h}h {m}m" if h else f"in {m}m"


def run():
    global _fa_last_run_date
    log("control loop starting")
    log(
        f"config: check every {CHECK_INTERVAL_SECONDS}s | "
        f"idle threshold: {IDLE_THRESHOLD_MINUTES}m | "
        f"blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00 | "
        f"FA: {FILTER_AGENT_HOUR:02d}:{FILTER_AGENT_MINUTE:02d} daily"
    )

    _init_fa_schedule()

    blackout_now = activity.in_blackout()
    log(f"startup blackout check: {'IN blackout — screenpipe will not start' if blackout_now else 'not in blackout — starting screenpipe'}")
    if not blackout_now:
        screenpipe.start()
    else:
        log("screenpipe held off until blackout ends")

    last_check     = activity.last_activity_time()
    _sp_was_running = screenpipe.running()
    _was_idle       = False
    _was_blackout   = blackout_now
    tick            = 0

    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        tick += 1

        if _override_pause:
            sp_alive = screenpipe.running()
            fa_alive = filter_controller.running()
            if sp_alive:
                log(f"override active — stopping screenpipe")
                screenpipe.stop("manual override")
            if fa_alive:
                log(f"override active — stopping filter agent")
                filter_controller.stop("manual override")
            log(f"[tick {tick}] override active — sleeping")
            continue

        idle     = activity.idle_seconds()
        blackout = activity.in_blackout()
        t        = activity.last_activity_time()
        moved    = t > last_check
        last_check = t
        sp_alive = screenpipe.running()
        fa_alive = filter_controller.running()

        log(
            f"[tick {tick}] {'MOVED' if moved else 'idle'} {idle:.0f}s ({idle/60:.1f}m) | "
            f"blackout={blackout} | sp={sp_alive} | fa={fa_alive}"
        )

        # Log state transitions
        if blackout != _was_blackout:
            log(f"  blackout transition: {'entered' if blackout else 'exited'} blackout window")
            _was_blackout = blackout

        is_idle_now = idle > IDLE_THRESHOLD_MINUTES * 60
        if is_idle_now != _was_idle:
            log(f"  idle transition: {'now IDLE ({idle:.0f}s)' if is_idle_now else 'activity resumed after idle'}")
            _was_idle = is_idle_now

        if sp_alive != _sp_was_running:
            log(f"  screenpipe state changed: {'started' if sp_alive else 'STOPPED (unexpected)'}")
            _sp_was_running = sp_alive

        # Blackout — kill everything
        if blackout:
            if sp_alive:
                log("  blackout active — stopping screenpipe")
                screenpipe.stop("blackout window")
            if fa_alive:
                log("  blackout active — stopping filter agent")
                filter_controller.stop("blackout window")
            continue

        # Idle management
        if idle > IDLE_THRESHOLD_MINUTES * 60:
            if sp_alive:
                log(f"  idle {idle/60:.1f}m > threshold {IDLE_THRESHOLD_MINUTES}m — stopping screenpipe")
                screenpipe.stop(f"idle {idle/60:.1f}m")
        else:
            if not sp_alive:
                log(f"  activity detected (idle {idle:.0f}s) — starting screenpipe")
                screenpipe.start()

        # Daily FA trigger
        if _fa_due():
            log(f"  FA due — firing daily filter agent at {FILTER_AGENT_HOUR:02d}:{FILTER_AGENT_MINUTE:02d}")
            filter_controller.start()
            _fa_last_run_date = datetime.datetime.now().date()
            log(f"  FA last run date set to {_fa_last_run_date}")
        else:
            now = datetime.datetime.now()
            mins_until = (FILTER_AGENT_HOUR * 60 + FILTER_AGENT_MINUTE) - (now.hour * 60 + now.minute)
            if mins_until < 0:
                mins_until += 1440
            if tick % 10 == 0:  # log FA countdown every ~5 min
                log(f"  FA next run: {mins_until}m from now (last run: {_fa_last_run_date})")
