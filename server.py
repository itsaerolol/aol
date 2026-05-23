"""
AOL C2 — Web server, REST API, WebSocket broadcast, Memory Query API.
Lifespan starts all subsystems; aol_c2.py just calls uvicorn.run(app).
"""

import asyncio
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import activity
import control_loop
import filter_controller
import screenpipe
from config import (
    BLACKOUT_START_HOUR, BLACKOUT_END_HOUR,
    C2_PORT, IDLE_THRESHOLD_MINUTES,
    MEMPALACE_KG_PATH, MEMPALACE_PATH,
)
from logs import filter_log_buffer, log, log_buffer, sp_log_buffer

# ---------------------------------------------------------------------------
# WebSocket broadcast manager
# ---------------------------------------------------------------------------


class _WSManager:
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


_ws_manager = _WSManager()


def _build_status() -> dict:
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
    }


async def _ws_push_loop():
    while True:
        if _ws_manager.has_clients():
            await _ws_manager.broadcast({
                "status":  _build_status(),
                "c2_logs": log_buffer[-50:],
                "sp_logs": sp_log_buffer[-50:],
                "fa_logs": filter_log_buffer[-50:],
            })
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Lifespan — starts all subsystems
# ---------------------------------------------------------------------------


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
    asyncio.create_task(_ws_push_loop())
    log(f"C2 dashboard live at http://localhost:{C2_PORT}")
    try:
        yield
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=_lifespan)

# ---------------------------------------------------------------------------
# Screenpipe / Filter control
# ---------------------------------------------------------------------------


@app.get("/status")
def status():
    return _build_status()


@app.post("/start")
def api_start():
    control_loop.set_override(False)
    screenpipe.start()
    return {"ok": True}


@app.post("/stop")
def api_stop():
    control_loop.set_override(True)
    screenpipe.stop("manual stop")
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


# ---------------------------------------------------------------------------
# Memory Query API
# ---------------------------------------------------------------------------


def _chroma_collection():
    import chromadb
    from mempalace.config import MempalaceConfig
    client = chromadb.PersistentClient(path=MEMPALACE_PATH)
    return client.get_or_create_collection(
        name=MempalaceConfig().collection_name,
        metadata={"hnsw:space": "cosine"},
    )


@app.get("/memory/search")
def memory_search(q: str, limit: int = 10, domain: str | None = None):
    """Semantic search over ChromaDB memories."""
    if not q.strip():
        return JSONResponse({"error": "q is required"}, status_code=400)
    try:
        col   = _chroma_collection()
        where = {"domain": domain} if domain else None
        kw    = {"where": where} if where else {}
        results = col.query(query_texts=[q], n_results=min(limit, 50), **kw)
        docs  = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        items = [
            {"summary": d, "score": round(1 - dist, 3), **m}
            for d, m, dist in zip(docs, metas, dists)
        ]
        return {"results": items, "count": len(items)}
    except ImportError:
        return JSONResponse({"error": "chromadb / mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/memory/recent")
def memory_recent(limit: int = 20, domain: str | None = None):
    """Most recent memories from ChromaDB, optionally filtered by domain."""
    try:
        col   = _chroma_collection()
        where = {"domain": domain} if domain else None
        kw    = {"where": where} if where else {}
        results = col.get(limit=min(limit, 100), include=["documents", "metadatas"], **kw)
        docs  = results.get("documents", [])
        metas = results.get("metadatas", [])
        items = [{"summary": d, **m} for d, m in zip(docs, metas)]
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return {"results": items, "count": len(items)}
    except ImportError:
        return JSONResponse({"error": "chromadb / mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/memory/kg")
def memory_kg(subject: str = "aero", limit: int = 100):
    """Query knowledge graph triples for a given subject entity."""
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        kg      = KnowledgeGraph(db_path=MEMPALACE_KG_PATH)
        triples = []
        for method in ("get_triples", "query_triples", "search_triples"):
            if hasattr(kg, method):
                try:
                    triples = getattr(kg, method)(subject=subject) or []
                    break
                except Exception:
                    pass
        return {"subject": subject, "triples": triples[:limit], "count": len(triples)}
    except ImportError:
        return JSONResponse({"error": "mempalace not installed"}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOL C2</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0a; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 2rem; }
  h1   { font-size: 1.1rem; letter-spacing: 0.2em; color: #fff; margin-bottom: 0.25rem; }
  .sub { font-size: 0.7rem; color: #444; margin-bottom: 2rem; letter-spacing: 0.1em; }
  .section-label {
    font-size: 0.6rem; color: #333; letter-spacing: 0.2em; text-transform: uppercase;
    margin: 1.8rem 0 0.8rem; border-top: 1px solid #141414; padding-top: 1.2rem;
  }

  /* status cards */
  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
  .card  { background: #111; border: 1px solid #1e1e1e; border-radius: 6px; padding: 1.1rem; }
  .card-label { font-size: 0.6rem; color: #444; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.4rem; }
  .card-value { font-size: 1.25rem; font-weight: bold; }

  .green  { color: #4ade80; }
  .red    { color: #f87171; }
  .yellow { color: #facc15; }
  .gray   { color: #444; }
  .blue   { color: #60a5fa; }
  .purple { color: #a78bfa; }

  /* actions */
  .actions { display: flex; gap: 0.6rem; margin-bottom: 1.5rem; flex-wrap: wrap; align-items: center; }
  .sep { width: 1px; height: 28px; background: #222; margin: 0 0.2rem; }
  button {
    background: #141414; border: 1px solid #2a2a2a; color: #ccc;
    padding: 0.5rem 1rem; font-family: 'Courier New', monospace;
    font-size: 0.75rem; letter-spacing: 0.08em; cursor: pointer;
    border-radius: 4px; transition: all 0.15s; text-transform: uppercase;
  }
  button:hover  { background: #1e1e1e; border-color: #444; color: #fff; }
  button.g { border-color: #14532d; color: #4ade80; }
  button.g:hover { background: #0a1c10; }
  button.r { border-color: #7f1d1d; color: #f87171; }
  button.r:hover { background: #1c0a0a; }
  button.b { border-color: #1e3a5f; color: #60a5fa; }
  button.b:hover { background: #0a1828; }

  /* log panels */
  .logs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .log-label { font-size: 0.6rem; color: #333; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.4rem; }
  .log-box {
    background: #060606; border: 1px solid #161616; border-radius: 6px;
    padding: 0.9rem; height: 260px; overflow-y: auto;
    font-size: 0.68rem; line-height: 1.65;
  }
  .e-c2   { color: #555; }  .e-c2.r   { color: #888; }
  .e-sp   { color: #3a4a3a; } .e-sp.r { color: #4a6a5a; }
  .e-fa   { color: #2a3a4a; } .e-fa.r { color: #4a6080; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; }
  .dot-g { background: #4ade80; box-shadow: 0 0 5px #4ade80; }
  .dot-r { background: #f87171; }
  .dot-b { background: #60a5fa; box-shadow: 0 0 5px #60a5fa; }
  .dot-y { background: #facc15; }
  #ws-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background: #444; margin-left: 0.5rem; vertical-align: middle; transition: background 0.3s; }
  #ws-dot.live { background: #4ade80; box-shadow: 0 0 4px #4ade80; }

  /* memory panel */
  .mem-controls {
    display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;
  }
  .mem-controls input, .mem-controls select {
    background: #111; border: 1px solid #2a2a2a; color: #ccc;
    padding: 0.45rem 0.75rem; font-family: 'Courier New', monospace;
    font-size: 0.75rem; border-radius: 4px; outline: none;
  }
  .mem-controls input { flex: 1; min-width: 200px; }
  .mem-controls input:focus, .mem-controls select:focus { border-color: #444; }
  .mem-controls select { color: #888; }
  .mem-results {
    background: #060606; border: 1px solid #161616; border-radius: 6px;
    padding: 0.9rem; min-height: 120px; max-height: 340px; overflow-y: auto;
    font-size: 0.7rem; line-height: 1.7;
  }
  .mem-card {
    border-bottom: 1px solid #111; padding: 0.6rem 0;
  }
  .mem-card:last-child { border-bottom: none; }
  .mem-meta { font-size: 0.62rem; color: #444; margin-bottom: 0.2rem; }
  .mem-meta .pri-HIGH   { color: #4ade80; }
  .mem-meta .pri-MEDIUM { color: #facc15; }
  .mem-summary { color: #bbb; }
  .mem-score { color: #555; font-size: 0.6rem; }
  .kg-triple {
    font-size: 0.68rem; color: #555; padding: 0.25rem 0; border-bottom: 1px solid #0e0e0e;
  }
  .kg-triple .subj { color: #60a5fa; }
  .kg-triple .pred { color: #a78bfa; }
  .kg-triple .obj  { color: #4ade80; }
  .mem-empty { color: #333; font-style: italic; }
  .kg-row { display: flex; gap: 1rem; }
  .kg-row input { flex: 1; }
</style>
</head>
<body>

<h1>AOL // C2 <span id="ws-dot"></span></h1>
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
  <button class="g" onclick="api('/start','POST')">&#9654; SP Start</button>
  <button class="r" onclick="api('/stop','POST')">&#9632; SP Stop</button>
  <button onclick="api('/resume','POST')">&#8635; Auto</button>
  <div class="sep"></div>
  <button class="b" onclick="api('/filter/start','POST')">&#9654; FA Start</button>
  <button class="r" onclick="api('/filter/stop','POST')">&#9632; FA Stop</button>
</div>

<div class="logs">
  <div>
    <div class="log-label">C2 Log</div>
    <div class="log-box" id="log-c2"></div>
  </div>
  <div>
    <div class="log-label">Screenpipe Output</div>
    <div class="log-box" id="log-sp"></div>
  </div>
  <div>
    <div class="log-label">Filter Agent</div>
    <div class="log-box" id="log-fa"></div>
  </div>
</div>

<!-- ── Memory Intelligence ── -->
<div class="section-label">Memory Intelligence</div>

<div class="mem-controls">
  <input id="mem-q" type="text" placeholder="semantic search memories..." onkeydown="if(event.key==='Enter')searchMem()">
  <select id="mem-domain">
    <option value="">all domains</option>
    <option>Developer</option>
    <option>AFROTC</option>
    <option>Faith</option>
    <option>Creative</option>
    <option>Finance</option>
  </select>
  <button class="b" onclick="searchMem()">&#128269; Search</button>
  <button onclick="recentMem()">&#9776; Recent</button>
</div>

<div class="kg-row mem-controls">
  <input id="kg-subject" type="text" placeholder="KG subject entity (default: aero)">
  <button class="b" onclick="lookupKG()">&#128279; Lookup KG</button>
</div>

<div class="mem-results" id="mem-results">
  <span class="mem-empty">Search memories or lookup KG triples above.</span>
</div>

<script>
// ── WebSocket ──
const dot = document.getElementById('ws-dot');
let ws, pingTimer;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    dot.classList.add('live');
    pingTimer = setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 25000);
  };

  ws.onmessage = (e) => {
    const { status: s, c2_logs, sp_logs, fa_logs } = JSON.parse(e.data);
    updateStatus(s);
    renderLog('log-c2', c2_logs, 'c2');
    renderLog('log-sp', sp_logs, 'sp');
    renderLog('log-fa', fa_logs, 'fa');
  };

  ws.onclose = () => {
    dot.classList.remove('live');
    clearInterval(pingTimer);
    setTimeout(connect, 2500);
  };

  ws.onerror = () => ws.close();
}

function updateStatus(s) {
  const spOn = s.screenpipe   === 'running';
  const faOn = s.filter_agent === 'running';

  document.getElementById('sp-status').innerHTML =
    `<span class="dot ${spOn ? 'dot-g' : 'dot-r'}"></span>` +
    `<span class="${spOn ? 'green' : 'red'}">${s.screenpipe.toUpperCase()}</span>`;

  document.getElementById('fa-status').innerHTML =
    `<span class="dot ${faOn ? 'dot-b' : 'dot-r'}"></span>` +
    `<span class="${faOn ? 'blue' : 'red'}">${s.filter_agent.toUpperCase()}</span>`;

  document.getElementById('idle').textContent     = s.idle_display;
  document.getElementById('blackout').innerHTML   = s.blackout
    ? '<span class="yellow">ACTIVE</span>' : '<span class="gray">INACTIVE</span>';
  document.getElementById('override').innerHTML   = s.override_pause
    ? '<span class="yellow">PAUSED</span>' : '<span class="green">AUTO</span>';
  document.getElementById('pids').innerHTML =
    `<span class="gray">sp:</span> ${s.sp_pid ?? '—'} &nbsp; <span class="gray">fa:</span> ${s.fa_pid ?? '—'}`;
}

function renderLog(boxId, entries, cls) {
  const box = document.getElementById(boxId);
  if (!entries || !entries.length) {
    box.innerHTML = '<span style="color:#222">no output yet</span>';
    return;
  }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.innerHTML = entries.slice().reverse().map((e, i) =>
    `<div class="e-${cls}${i < 5 ? ' r' : ''}">${escHtml(e)}</div>`
  ).join('');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// ── Actions ──
async function api(ep, method = 'GET') {
  await fetch(ep, { method });
}

// ── Memory search ──
async function searchMem() {
  const q      = document.getElementById('mem-q').value.trim();
  const domain = document.getElementById('mem-domain').value;
  if (!q) return;
  setMemLoading();
  let url = `/memory/search?q=${encodeURIComponent(q)}&limit=15`;
  if (domain) url += `&domain=${encodeURIComponent(domain)}`;
  const data = await fetch(url).then(r => r.json()).catch(err => ({ error: String(err) }));
  renderMemResults(data.results, 'search', data.error);
}

async function recentMem() {
  const domain = document.getElementById('mem-domain').value;
  setMemLoading();
  let url = '/memory/recent?limit=20';
  if (domain) url += `&domain=${encodeURIComponent(domain)}`;
  const data = await fetch(url).then(r => r.json()).catch(err => ({ error: String(err) }));
  renderMemResults(data.results, 'recent', data.error);
}

async function lookupKG() {
  const subject = document.getElementById('kg-subject').value.trim() || 'aero';
  setMemLoading();
  const data = await fetch(`/memory/kg?subject=${encodeURIComponent(subject)}&limit=100`)
    .then(r => r.json()).catch(err => ({ error: String(err) }));
  renderKGResults(data.triples, subject, data.error);
}

function setMemLoading() {
  document.getElementById('mem-results').innerHTML = '<span class="mem-empty">loading...</span>';
}

function renderMemResults(items, mode, error) {
  const box = document.getElementById('mem-results');
  if (error) { box.innerHTML = `<span style="color:#f87171">error: ${escHtml(error)}</span>`; return; }
  if (!items || !items.length) { box.innerHTML = '<span class="mem-empty">no results</span>'; return; }
  box.innerHTML = items.map(m => {
    const score = m.score != null ? `<span class="mem-score">&nbsp;· ${m.score}</span>` : '';
    return `<div class="mem-card">
      <div class="mem-meta">
        <span class="pri-${m.priority}">${m.priority || '?'}</span>
        &nbsp;·&nbsp;${escHtml(m.domain || '?')}
        ${m.project ? '&nbsp;·&nbsp;' + escHtml(m.project) : ''}
        ${m.timestamp ? '&nbsp;·&nbsp;' + escHtml(m.timestamp.slice(0,16)) : ''}
        ${score}
      </div>
      <div class="mem-summary">${escHtml(m.summary || '')}</div>
    </div>`;
  }).join('');
}

function renderKGResults(triples, subject, error) {
  const box = document.getElementById('mem-results');
  if (error) { box.innerHTML = `<span style="color:#f87171">error: ${escHtml(error)}</span>`; return; }
  if (!triples || !triples.length) { box.innerHTML = `<span class="mem-empty">no triples for "${escHtml(subject)}"</span>`; return; }
  box.innerHTML = triples.map(t => {
    const s = t.subject   || t[0] || '?';
    const p = t.predicate || t[1] || '?';
    const o = t.object    || t[2] || '?';
    const d = t.durability || t.valid_to || '';
    return `<div class="kg-triple">
      <span class="subj">${escHtml(s)}</span>
      &nbsp;<span class="pred">${escHtml(p)}</span>&nbsp;
      <span class="obj">${escHtml(o)}</span>
      ${d ? '<span style="color:#2a2a2a"> [' + escHtml(d) + ']</span>' : ''}
    </div>`;
  }).join('');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

connect();
</script>
</body>
</html>"""
