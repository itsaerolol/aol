"""
AOL C2 — Command & Control
Runs the Screenpipe activity controller and serves a web dashboard at localhost:8080.
All AOL components are monitored and controlled from one place.
"""

import os
import time
import subprocess
import threading
from datetime import datetime
from contextlib import asynccontextmanager

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pynput import mouse, keyboard

# --- Log Catching ---
import sys

if sys.stdout is None:
    sys.stdout = open(os.path.expanduser("~/aol_c2.log"), "a", buffering=1)
    sys.stderr = sys.stdout
    print("Logging C2 activity to ~/aol_c2.log...")

# --- Config ---
IDLE_THRESHOLD_MINUTES = 10
BLACKOUT_START_HOUR = 4
BLACKOUT_END_HOUR = 10
CHECK_INTERVAL_SECONDS = 30
SCREENPIPE_CMD = ["npx", "screenpipe@latest", "record"]
SCREENPIPE_API = "http://localhost:3030"
C2_PORT = 45139

# --- State ---
last_activity = time.time()
screenpipe_proc = None
is_paused = False
lock = threading.Lock()
log_buffer = []
override_pause = False
mouse_listener = None
kb_listener = None


# --- Logging ---

def log(msg: str):
    entry = f"[{timestamp()}] {msg}"
    print(entry)
    log_buffer.append(entry)
    if len(log_buffer) > 200:
        log_buffer.pop(0)


# --- Activity Tracking ---

def on_activity(*args):
    global last_activity
    last_activity = time.time()


def on_keyboard_press(key):
    global last_activity
    last_activity = time.time()


def idle_seconds() -> float:
    return time.time() - last_activity


def in_blackout() -> bool:
    hour = datetime.now().hour
    return BLACKOUT_START_HOUR <= hour < BLACKOUT_END_HOUR


# --- Screenpipe Control ---

def start_screenpipe():
    global screenpipe_proc, is_paused
    with lock:
        if screenpipe_proc and screenpipe_proc.poll() is None:
            return
        log("starting screenpipe...")
        screenpipe_proc = subprocess.Popen(
            SCREENPIPE_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=True,
        )
        is_paused = False
        log(f"screenpipe running (pid {screenpipe_proc.pid})")


def stop_screenpipe(reason: str):
    global screenpipe_proc, is_paused
    with lock:
        if not screenpipe_proc or screenpipe_proc.poll() is not None:
            return
        log(f"stopping screenpipe — {reason}")
        try:
            parent = psutil.Process(screenpipe_proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            psutil.wait_procs(children + [parent], timeout=5)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            log(f"error terminating process tree: {e}")
            screenpipe_proc.kill()
        screenpipe_proc = None
        is_paused = True
        log("screenpipe stopped")


def is_running() -> bool:
    return screenpipe_proc is not None and screenpipe_proc.poll() is None


# --- Control Loop ---

def control_loop():
    log("AOL controller active")
    log(f"idle threshold: {IDLE_THRESHOLD_MINUTES} min | blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00")

    if not in_blackout():
        start_screenpipe()
    else:
        log("in blackout window, screenpipe not started")

    last_check_activity = last_activity

    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)

        if override_pause:
            if is_running():
                stop_screenpipe("manual override")
            continue

        idle = idle_seconds()
        blackout = in_blackout()
        moved = last_activity > last_check_activity
        last_check_activity = last_activity

        log(f"{'moved' if moved else 'idle'} {idle:.0f}s | blackout={blackout} | running={is_running()}")

        if blackout:
            if is_running():
                stop_screenpipe("blackout window")
            continue

        if idle > IDLE_THRESHOLD_MINUTES * 60:
            if is_running():
                stop_screenpipe(f"idle {idle / 60:.1f} min")
        else:
            if not is_running():
                log("activity detected, resuming...")
                start_screenpipe()


# --- Listeners ---

def start_listeners():
    global mouse_listener, kb_listener
    mouse_listener = mouse.Listener(
        on_move=on_activity,
        on_click=on_activity,
        on_scroll=on_activity,
        daemon=True
    )
    kb_listener = keyboard.Listener(on_press=on_keyboard_press, daemon=True)
    mouse_listener.start()
    kb_listener.start()
    log("input listeners active")


def stop_listeners():
    global mouse_listener, kb_listener
    try:
        if mouse_listener:
            mouse_listener.stop()
        if kb_listener:
            kb_listener.stop()
    except Exception:
        pass


# --- Cleanup ---

def cleanup():
    log("shutting down...")
    stop_screenpipe("C2 shutdown")
    stop_listeners()
    # restore Windows console input mode so terminal isn't left in pynput state
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), 0x0003)
    except Exception:
        pass
    log("shutdown complete")


# --- FastAPI ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_listeners()
    threading.Thread(target=control_loop, daemon=True).start()
    log(f"C2 dashboard live at http://localhost:{C2_PORT}")
    yield
    cleanup()

app = FastAPI(lifespan=lifespan)


@app.get("/status")
def status():
    return {
        "screenpipe": "running" if is_running() else "stopped",
        "override_pause": override_pause,
        "blackout": in_blackout(),
        "idle_seconds": round(idle_seconds()),
        "idle_threshold_seconds": IDLE_THRESHOLD_MINUTES * 60,
        "blackout_window": f"{BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00",
        "pid": screenpipe_proc.pid if is_running() else None,
        "timestamp": timestamp(),
    }


@app.post("/start")
def api_start():
    global override_pause
    override_pause = False
    start_screenpipe()
    return {"ok": True, "message": "screenpipe started"}


@app.post("/stop")
def api_stop():
    global override_pause
    override_pause = True
    stop_screenpipe("C2 manual stop")
    return {"ok": True, "message": "screenpipe stopped (override active)"}


@app.post("/resume")
def api_resume():
    global override_pause
    override_pause = False
    log("override cleared, resuming auto-control")
    return {"ok": True, "message": "auto-control resumed"}


@app.get("/logs")
def get_logs():
    return {"logs": log_buffer[-50:]}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# --- Dashboard HTML ---

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOL C2</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0a; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 2rem; }
  h1 { font-size: 1.1rem; letter-spacing: 0.2em; color: #fff; margin-bottom: 0.25rem; }
  .sub { font-size: 0.7rem; color: #555; margin-bottom: 2rem; letter-spacing: 0.1em; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem; }
  .card { background: #111; border: 1px solid #222; border-radius: 6px; padding: 1.25rem; }
  .card-label { font-size: 0.65rem; color: #555; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.5rem; }
  .card-value { font-size: 1.4rem; font-weight: bold; }
  .green { color: #4ade80; }
  .red { color: #f87171; }
  .yellow { color: #facc15; }
  .gray { color: #555; }
  .actions { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  button { background: #1a1a1a; border: 1px solid #333; color: #e0e0e0; padding: 0.6rem 1.2rem;
           font-family: 'Courier New', monospace; font-size: 0.8rem; letter-spacing: 0.1em;
           cursor: pointer; border-radius: 4px; transition: all 0.15s; text-transform: uppercase; }
  button:hover { background: #252525; border-color: #555; }
  button.danger { border-color: #7f1d1d; color: #f87171; }
  button.danger:hover { background: #1c0a0a; }
  button.success { border-color: #14532d; color: #4ade80; }
  button.success:hover { background: #0a1c10; }
  .log-box { background: #080808; border: 1px solid #1a1a1a; border-radius: 6px;
             padding: 1rem; height: 280px; overflow-y: auto; font-size: 0.72rem; line-height: 1.6; }
  .log-entry { color: #555; }
  .log-entry.recent { color: #888; }
  .section-label { font-size: 0.65rem; color: #444; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.5rem; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 0.5rem; }
  .dot-green { background: #4ade80; box-shadow: 0 0 6px #4ade80; }
  .dot-red { background: #f87171; }
  .dot-yellow { background: #facc15; }
</style>
</head>
<body>
<h1>AOL // C2</h1>
<p class="sub">AUTONOMOUS OPERATIONAL LAYER · COMMAND & CONTROL</p>

<div class="grid">
  <div class="card">
    <div class="card-label">Screenpipe</div>
    <div class="card-value" id="sp-status">—</div>
  </div>
  <div class="card">
    <div class="card-label">Idle Time</div>
    <div class="card-value" id="idle-time">—</div>
  </div>
  <div class="card">
    <div class="card-label">Blackout Window</div>
    <div class="card-value" id="blackout">—</div>
  </div>
  <div class="card">
    <div class="card-label">Override</div>
    <div class="card-value" id="override">—</div>
  </div>
</div>

<div class="actions">
  <button class="success" onclick="api('/start', 'POST')">▶ Start</button>
  <button class="danger" onclick="api('/stop', 'POST')">■ Stop</button>
  <button onclick="api('/resume', 'POST')">↺ Resume Auto</button>
</div>

<div class="section-label">System Log</div>
<div class="log-box" id="log-box"></div>

<script>
async function api(endpoint, method = 'GET') {
  await fetch(endpoint, { method });
  refresh();
}

async function refresh() {
  const [status, logs] = await Promise.all([
    fetch('/status').then(r => r.json()),
    fetch('/logs').then(r => r.json())
  ]);

  const running = status.screenpipe === 'running';
  const blackout = status.blackout;
  const override = status.override_pause;

  document.getElementById('sp-status').innerHTML =
    `<span class="status-dot ${running ? 'dot-green' : 'dot-red'}"></span>` +
    `<span class="${running ? 'green' : 'red'}">${status.screenpipe.toUpperCase()}</span>`;

  const idle = status.idle_seconds;
  const idleMin = Math.floor(idle / 60);
  const idleSec = idle % 60;
  document.getElementById('idle-time').textContent =
    idleMin > 0 ? `${idleMin}m ${idleSec}s` : `${idleSec}s`;

  document.getElementById('blackout').innerHTML =
    blackout
      ? `<span class="yellow">ACTIVE</span>`
      : `<span class="gray">INACTIVE</span>`;

  document.getElementById('override').innerHTML =
    override
      ? `<span class="yellow">PAUSED</span>`
      : `<span class="green">AUTO</span>`;

  const logBox = document.getElementById('log-box');
  const entries = logs.logs;
  logBox.innerHTML = entries.slice().reverse().map((e, i) =>
    `<div class="log-entry ${i < 5 ? 'recent' : ''}">${e}</div>`
  ).join('');
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=C2_PORT, log_level="error")
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
