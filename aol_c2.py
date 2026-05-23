"""
AOL C2 — Command & Control
Manages Screenpipe + Filter Agent as subprocesses.
Dashboard at localhost:45139 — three live log panels.
"""

import os
import sys
import time
import subprocess
import threading
from datetime import datetime
from contextlib import asynccontextmanager

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pynput import mouse, keyboard

# ---------------------------------------------------------------------------
# Log redirect (pythonw has no stdout)
# ---------------------------------------------------------------------------

if sys.stdout is None:
    sys.stdout = open(os.path.expanduser("~/aol_c2.log"), "a", buffering=1)
    sys.stderr = sys.stdout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IDLE_THRESHOLD_MINUTES = 10
BLACKOUT_START_HOUR    = 4
BLACKOUT_END_HOUR      = 10
CHECK_INTERVAL_SECONDS = 30
C2_PORT                = 45139

SCREENPIPE_CMD = ["npx", "screenpipe@latest", "record"]
SCREENPIPE_API = "http://localhost:3030"

# Filter agent — same dir as this script, same Python interpreter
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
FILTER_AGENT_SCRIPT = os.path.join(SCRIPT_DIR, "filter_agent.py")
FILTER_AGENT_PYTHON = sys.executable   # override with full 3.14 path if needed

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

last_activity = time.time()
override_pause = False
mouse_listener = None
kb_listener    = None

# per-process state bundled into dicts to avoid long global lists
screenpipe_proc = None
filter_proc     = None

sp_lock     = threading.Lock()
filter_lock = threading.Lock()

log_buffer         = []          # C2 own log
sp_log_buffer      = []          # Screenpipe stderr
filter_log_buffer  = []          # Filter Agent stdout

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _append(buf: list, entry: str, cap: int = 200):
    buf.append(entry)
    if len(buf) > cap:
        buf.pop(0)


def log(msg: str):
    entry = f"[{timestamp()}] {msg}"
    print(entry, flush=True)
    _append(log_buffer, entry)


def log_sp(line: str):
    _append(sp_log_buffer, f"[{timestamp()}] {line}")


def log_filter(line: str):
    _append(filter_log_buffer, f"[{timestamp()}] {line}")

# ---------------------------------------------------------------------------
# Activity tracking
# ---------------------------------------------------------------------------

def on_activity(*args):
    global last_activity
    last_activity = time.time()


def on_keyboard_press(key):
    global last_activity
    last_activity = time.time()


def idle_seconds() -> float:
    return time.time() - last_activity


def in_blackout() -> bool:
    return BLACKOUT_START_HOUR <= datetime.now().hour < BLACKOUT_END_HOUR

# ---------------------------------------------------------------------------
# Generic log drain
# ---------------------------------------------------------------------------

def drain_pipe(proc, log_fn):
    """Read stdout/stderr from a subprocess line-by-line into a log buffer."""
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_fn(line)
    except Exception:
        pass


def drain_stdout(proc, log_fn):
    """Read stdout from a subprocess."""
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log_fn(line)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Screenpipe control
# ---------------------------------------------------------------------------

def start_screenpipe():
    global screenpipe_proc
    with sp_lock:
        if screenpipe_proc and screenpipe_proc.poll() is None:
            return
        log("starting screenpipe...")
        screenpipe_proc = subprocess.Popen(
            SCREENPIPE_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=True,
        )
        threading.Thread(
            target=drain_pipe,
            args=(screenpipe_proc, log_sp),
            daemon=True,
        ).start()
        log(f"screenpipe running (pid {screenpipe_proc.pid})")


def stop_screenpipe(reason: str):
    global screenpipe_proc
    with sp_lock:
        if not screenpipe_proc or screenpipe_proc.poll() is not None:
            return
        log(f"stopping screenpipe — {reason}")
        try:
            parent   = psutil.Process(screenpipe_proc.pid)
            children = parent.children(recursive=True)
            for c in children:
                c.terminate()
            parent.terminate()
            psutil.wait_procs(children + [parent], timeout=5)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            log(f"error killing screenpipe tree: {e}")
            screenpipe_proc.kill()
        screenpipe_proc = None
        log("screenpipe stopped")


def sp_running() -> bool:
    return screenpipe_proc is not None and screenpipe_proc.poll() is None

# ---------------------------------------------------------------------------
# Filter Agent control
# ---------------------------------------------------------------------------

def start_filter_agent():
    global filter_proc
    with filter_lock:
        if filter_proc and filter_proc.poll() is None:
            return
        if not os.path.exists(FILTER_AGENT_SCRIPT):
            log(f"filter agent not found: {FILTER_AGENT_SCRIPT}")
            return
        log("starting filter agent...")
        filter_proc = subprocess.Popen(
            [FILTER_AGENT_PYTHON, FILTER_AGENT_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            cwd=SCRIPT_DIR,
        )
        threading.Thread(
            target=drain_stdout,
            args=(filter_proc, log_filter),
            daemon=True,
        ).start()
        log(f"filter agent running (pid {filter_proc.pid})")


def stop_filter_agent(reason: str = "C2 stop"):
    global filter_proc
    with filter_lock:
        if not filter_proc or filter_proc.poll() is not None:
            return
        log(f"stopping filter agent — {reason}")
        try:
            filter_proc.terminate()
            filter_proc.wait(timeout=10)
        except Exception as e:
            log(f"error stopping filter agent: {e}")
            filter_proc.kill()
        filter_proc = None
        log("filter agent stopped")


def filter_running() -> bool:
    return filter_proc is not None and filter_proc.poll() is None

# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------

def control_loop():
    log("AOL controller active")
    log(f"idle threshold: {IDLE_THRESHOLD_MINUTES}m | blackout: {BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00")

    if not in_blackout():
        start_screenpipe()
    else:
        log("in blackout window, screenpipe not started")

    start_filter_agent()

    last_check = last_activity

    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)

        if override_pause:
            if sp_running():
                stop_screenpipe("manual override")
            continue

        idle    = idle_seconds()
        blackout = in_blackout()
        moved   = last_activity > last_check
        last_check = last_activity

        log(f"{'moved' if moved else 'idle'} {idle:.0f}s | blackout={blackout} | sp={sp_running()} | fa={filter_running()}")

        if blackout:
            if sp_running():
                stop_screenpipe("blackout window")
            continue

        if idle > IDLE_THRESHOLD_MINUTES * 60:
            if sp_running():
                stop_screenpipe(f"idle {idle/60:.1f}m")
        else:
            if not sp_running():
                log("activity detected — resuming screenpipe")
                start_screenpipe()

        # auto-restart filter agent if it died unexpectedly
        if not filter_running():
            log("filter agent not running — restarting")
            start_filter_agent()

# ---------------------------------------------------------------------------
# Input listeners
# ---------------------------------------------------------------------------

def start_listeners():
    global mouse_listener, kb_listener
    mouse_listener = mouse.Listener(
        on_move=on_activity, on_click=on_activity, on_scroll=on_activity, daemon=True)
    kb_listener = keyboard.Listener(on_press=on_keyboard_press, daemon=True)
    mouse_listener.start()
    kb_listener.start()
    log("input listeners active")


def stop_listeners():
    try:
        if mouse_listener: mouse_listener.stop()
        if kb_listener:    kb_listener.stop()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup():
    log("shutting down...")
    stop_screenpipe("C2 shutdown")
    stop_filter_agent("C2 shutdown")
    stop_listeners()
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), 0x0003)
    except Exception:
        pass
    log("shutdown complete")

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

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
        "screenpipe":      "running" if sp_running()     else "stopped",
        "filter_agent":    "running" if filter_running() else "stopped",
        "override_pause":  override_pause,
        "blackout":        in_blackout(),
        "idle_seconds":    round(idle_seconds()),
        "idle_threshold":  IDLE_THRESHOLD_MINUTES * 60,
        "blackout_window": f"{BLACKOUT_START_HOUR:02d}:00–{BLACKOUT_END_HOUR:02d}:00",
        "sp_pid":          screenpipe_proc.pid if sp_running()     else None,
        "fa_pid":          filter_proc.pid    if filter_running()  else None,
        "timestamp":       timestamp(),
    }


@app.post("/start")
def api_start():
    global override_pause
    override_pause = False
    start_screenpipe()
    return {"ok": True}


@app.post("/stop")
def api_stop():
    global override_pause
    override_pause = True
    stop_screenpipe("manual stop")
    return {"ok": True}


@app.post("/resume")
def api_resume():
    global override_pause
    override_pause = False
    log("override cleared — resuming auto-control")
    return {"ok": True}


@app.post("/filter/start")
def api_filter_start():
    start_filter_agent()
    return {"ok": True}


@app.post("/filter/stop")
def api_filter_stop():
    stop_filter_agent("manual stop")
    return {"ok": True}


@app.get("/logs")
def get_logs():
    return {"logs": log_buffer[-50:]}


@app.get("/screenpipe-logs")
def get_sp_logs():
    return {"logs": sp_log_buffer[-50:]}


@app.get("/filter-logs")
def get_filter_logs():
    return {"logs": filter_log_buffer[-50:]}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOL C2</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0a; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 2rem; }
  h1  { font-size: 1.1rem; letter-spacing: 0.2em; color: #fff; margin-bottom: 0.25rem; }
  .sub { font-size: 0.7rem; color: #444; margin-bottom: 2rem; letter-spacing: 0.1em; }

  /* status cards — 3 col */
  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
  .card  { background: #111; border: 1px solid #1e1e1e; border-radius: 6px; padding: 1.1rem; }
  .card-label { font-size: 0.6rem; color: #444; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.4rem; }
  .card-value { font-size: 1.25rem; font-weight: bold; }

  .green  { color: #4ade80; }
  .red    { color: #f87171; }
  .yellow { color: #facc15; }
  .gray   { color: #444; }
  .blue   { color: #60a5fa; }

  /* actions */
  .actions { display: flex; gap: 0.6rem; margin-bottom: 1.5rem; flex-wrap: wrap; align-items: center; }
  .sep { width: 1px; height: 28px; background: #222; margin: 0 0.2rem; }
  button { background: #141414; border: 1px solid #2a2a2a; color: #ccc;
           padding: 0.5rem 1rem; font-family: 'Courier New', monospace;
           font-size: 0.75rem; letter-spacing: 0.08em; cursor: pointer;
           border-radius: 4px; transition: all 0.15s; text-transform: uppercase; }
  button:hover  { background: #1e1e1e; border-color: #444; color: #fff; }
  button.g { border-color: #14532d; color: #4ade80; }
  button.g:hover { background: #0a1c10; }
  button.r { border-color: #7f1d1d; color: #f87171; }
  button.r:hover { background: #1c0a0a; }
  button.b { border-color: #1e3a5f; color: #60a5fa; }
  button.b:hover { background: #0a1828; }

  /* log panels — 3 col */
  .logs  { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .log-panel {}
  .log-label { font-size: 0.6rem; color: #333; letter-spacing: 0.15em;
               text-transform: uppercase; margin-bottom: 0.4rem; }
  .log-box   { background: #060606; border: 1px solid #161616; border-radius: 6px;
               padding: 0.9rem; height: 300px; overflow-y: auto;
               font-size: 0.68rem; line-height: 1.65; }

  .e-c2     { color: #555; }
  .e-c2.r   { color: #888; }
  .e-sp     { color: #3a4a3a; }
  .e-sp.r   { color: #4a6a5a; }
  .e-fa     { color: #2a3a4a; }
  .e-fa.r   { color: #4a6080; }

  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; }
  .dot-g { background: #4ade80; box-shadow: 0 0 5px #4ade80; }
  .dot-r { background: #f87171; }
  .dot-y { background: #facc15; }
  .dot-b { background: #60a5fa; box-shadow: 0 0 5px #60a5fa; }
</style>
</head>
<body>

<h1>AOL // C2</h1>
<p class="sub">AUTONOMOUS OPERATIONAL LAYER &middot; COMMAND &amp; CONTROL</p>

<div class="cards">
  <div class="card">
    <div class="card-label">Screenpipe</div>
    <div class="card-value" id="sp-status">—</div>
  </div>
  <div class="card">
    <div class="card-label">Filter Agent</div>
    <div class="card-value" id="fa-status">—</div>
  </div>
  <div class="card">
    <div class="card-label">Idle</div>
    <div class="card-value" id="idle">—</div>
  </div>
  <div class="card">
    <div class="card-label">Blackout</div>
    <div class="card-value" id="blackout">—</div>
  </div>
  <div class="card">
    <div class="card-label">Override</div>
    <div class="card-value" id="override">—</div>
  </div>
  <div class="card">
    <div class="card-label">PIDs</div>
    <div class="card-value" id="pids" style="font-size:0.85rem;padding-top:4px;">—</div>
  </div>
</div>

<div class="actions">
  <button class="g" onclick="api('/start','POST')">▶ SP Start</button>
  <button class="r" onclick="api('/stop','POST')">■ SP Stop</button>
  <button onclick="api('/resume','POST')">↺ Auto</button>
  <div class="sep"></div>
  <button class="b" onclick="api('/filter/start','POST')">▶ FA Start</button>
  <button class="r" onclick="api('/filter/stop','POST')">■ FA Stop</button>
</div>

<div class="logs">
  <div class="log-panel">
    <div class="log-label">C2 Log</div>
    <div class="log-box" id="log-c2"></div>
  </div>
  <div class="log-panel">
    <div class="log-label">Screenpipe Output</div>
    <div class="log-box" id="log-sp"></div>
  </div>
  <div class="log-panel">
    <div class="log-label">Filter Agent</div>
    <div class="log-box" id="log-fa"></div>
  </div>
</div>

<script>
async function api(ep, method='GET') {
  await fetch(ep, { method });
  refresh();
}

function renderLog(boxId, entries, cls) {
  const box = document.getElementById(boxId);
  if (!entries.length) {
    box.innerHTML = `<span style="color:#222">no output yet</span>`;
    return;
  }
  box.innerHTML = entries.slice().reverse().map((e, i) =>
    `<div class="e-${cls}${i < 5 ? ' r' : ''}">${e}</div>`
  ).join('');
}

async function refresh() {
  const [s, c2, sp, fa] = await Promise.all([
    fetch('/status').then(r => r.json()),
    fetch('/logs').then(r => r.json()),
    fetch('/screenpipe-logs').then(r => r.json()),
    fetch('/filter-logs').then(r => r.json()),
  ]);

  const spOn = s.screenpipe    === 'running';
  const faOn = s.filter_agent  === 'running';

  document.getElementById('sp-status').innerHTML =
    `<span class="dot ${spOn ? 'dot-g' : 'dot-r'}"></span><span class="${spOn ? 'green' : 'red'}">${s.screenpipe.toUpperCase()}</span>`;

  document.getElementById('fa-status').innerHTML =
    `<span class="dot ${faOn ? 'dot-b' : 'dot-r'}"></span><span class="${faOn ? 'blue' : 'red'}">${s.filter_agent.toUpperCase()}</span>`;

  const im = Math.floor(s.idle_seconds / 60), is = s.idle_seconds % 60;
  document.getElementById('idle').textContent = im > 0 ? `${im}m ${is}s` : `${is}s`;

  document.getElementById('blackout').innerHTML = s.blackout
    ? `<span class="yellow">ACTIVE</span>` : `<span class="gray">INACTIVE</span>`;

  document.getElementById('override').innerHTML = s.override_pause
    ? `<span class="yellow">PAUSED</span>` : `<span class="green">AUTO</span>`;

  document.getElementById('pids').innerHTML =
    `<span class="gray">sp:</span> ${s.sp_pid ?? '—'} &nbsp; <span class="gray">fa:</span> ${s.fa_pid ?? '—'}`;

  renderLog('log-c2', c2.logs, 'c2');
  renderLog('log-sp', sp.logs, 'sp');
  renderLog('log-fa', fa.logs, 'fa');
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------

def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=C2_PORT, log_level="error")
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
