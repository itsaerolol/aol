from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOL C2</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #080808; color: #d0d0d0;
    font-family: 'Courier New', monospace;
    padding: 1.2rem; min-height: 100vh;
  }

  /* header — compact single line */
  .header {
    display: flex; align-items: center; gap: 1rem;
    margin-bottom: 0.85rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid #0e0e0e;
  }
  h1 { font-size: 0.85rem; letter-spacing: 0.3em; color: #fff; display: inline; }

  /* header C2 status strip */
  .hdr-c2 {
    margin-left: auto; display: flex; align-items: center; gap: 0.55rem;
    flex-wrap: wrap;
  }
  .hdr-stat {
    font-size: 0.58rem; color: #383838; letter-spacing: 0.08em;
    display: flex; align-items: center; gap: 0.3rem;
  }
  .hdr-sep { font-size: 0.55rem; color: #252525; }
  .hdr-btn {
    background: #0d0d0d; border: 1px solid #222; color: #666;
    padding: 0.18rem 0.55rem; font-family: 'Courier New', monospace;
    font-size: 0.58rem; letter-spacing: 0.06em; cursor: pointer;
    border-radius: 3px; transition: all 0.12s; text-transform: uppercase;
    white-space: nowrap;
  }
  .hdr-btn:hover { background: #181818; color: #ccc; }
  .hdr-btn.y { border-color: #4a2a08; color: #a07010; }
  .hdr-btn.y:hover { background: #120d00; color: #facc15; }
  .hdr-btn.r { border-color: #4a1010; color: #904040; }
  .hdr-btn.r:hover { background: #120808; color: #f87171; }

  /* ws indicator */
  #ws-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: #2a2a2a; vertical-align: middle;
    transition: background 0.3s;
  }
  #ws-dot.live { background: #4ade80; box-shadow: 0 0 5px #4ade80; }

  /* top control strip — SP + FA only */
  .grid-top {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    margin-bottom: 0;
  }

  /* panel */
  .panel {
    background: #0c0c0c; border: 1px solid #181818;
    border-radius: 6px; padding: 0.9rem;
    display: flex; flex-direction: column; gap: 0.6rem;
  }

  /* panel accent border-tops — signals process type */
  .panel-sp  { border-top: 2px solid #14532d; }
  .panel-fa  { border-top: 2px solid #1e3a5f; }
  .panel-c2  { border-top: 2px solid #3f2a0a; } /* kept for log strip */
  .panel-mem { border-top: 2px solid #2d1f4a; }

  .panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding-bottom: 0.55rem; border-bottom: 1px solid #121212;
  }
  .panel-title {
    font-size: 0.60rem; letter-spacing: 0.25em; color: #555;
    text-transform: uppercase;
  }

  /* status badges */
  .badge {
    font-size: 0.7rem; font-weight: bold;
    display: flex; align-items: center; gap: 5px;
  }
  .dot {
    display: inline-block; width: 7px; height: 7px;
    border-radius: 50%; flex-shrink: 0;
  }
  .dot-g { background: #4ade80; box-shadow: 0 0 5px #4ade80; }
  .dot-r { background: #f87171; }
  .dot-b { background: #60a5fa; box-shadow: 0 0 4px #60a5fa; }
  .dot-dim { background: #2a2a2a; }

  .green  { color: #4ade80; }
  .red    { color: #f87171; }
  .yellow { color: #facc15; }
  .blue   { color: #60a5fa; }
  .gray   { color: #555; }

  /* meta rows */
  .panel-meta { display: flex; flex-direction: column; gap: 0.28rem; }
  .meta-row { font-size: 0.68rem; display: flex; gap: 0.5rem; align-items: baseline; }
  .meta-key { color: #4a4a4a; font-size: 0.62rem; letter-spacing: 0.08em; min-width: 58px; }
  .meta-val { color: #999; }

  /* buttons */
  .panel-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  button {
    background: #111; border: 1px solid #242424; color: #aaa;
    padding: 0.35rem 0.8rem; font-family: 'Courier New', monospace;
    font-size: 0.68rem; letter-spacing: 0.08em; cursor: pointer;
    border-radius: 4px; transition: all 0.12s; text-transform: uppercase;
    white-space: nowrap;
  }
  button:hover  { background: #1a1a1a; border-color: #3a3a3a; color: #eee; }
  button.g { border-color: #14532d; color: #4ade80; }
  button.g:hover { background: #0a1c10; }
  button.r { border-color: #7f1d1d; color: #f87171; }
  button.r:hover { background: #1c0a0a; }
  button.b { border-color: #1e3a5f; color: #60a5fa; }
  button.b:hover { background: #0a1828; }
  button.y { border-color: #713f12; color: #facc15; }
  button.y:hover { background: #1c1200; }

  /* collapsible log strip */
  .log-strip { display: flex; flex-direction: column; gap: 2px; margin: 0.6rem 0 0.75rem; }
  .log-section { border-radius: 4px; overflow: hidden; }
  .log-toggle {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.38rem 0.75rem; cursor: pointer;
    background: #0a0a0a; border: 1px solid #131313;
    user-select: none;
  }
  .log-toggle:hover { background: #0f0f0f; }
  .log-label {
    font-size: 0.58rem; color: #3a3a3a;
    letter-spacing: 0.18em; text-transform: uppercase;
  }
  .log-chevron { margin-left: auto; font-size: 0.58rem; color: #3a3a3a; }
  .log-badge {
    font-size: 0.56rem; color: #4ade80; background: #0a1c10;
    border: 1px solid #14532d; padding: 0.02rem 0.35rem;
    border-radius: 2px; display: none;
  }

  /* log boxes */
  .log-box {
    border: 1px solid #131313; border-top: none;
    padding: 0.6rem 0.75rem;
    height: 140px; overflow-y: auto;
    font-size: 0.65rem; line-height: 1.7;
    scrollbar-width: thin; scrollbar-color: #1a1a1a transparent;
  }
  #log-c2 { height: 160px; }
  .log-box::-webkit-scrollbar { width: 4px; }
  .log-box::-webkit-scrollbar-track { background: transparent; }
  .log-box::-webkit-scrollbar-thumb { background: #1a1a1a; }
  .log-empty { color: #222; font-style: italic; }

  /* tinted log backgrounds per service */
  #log-sp { background: #030d05; border-color: #0a1a0d; }
  #log-fa { background: #03070f; border-color: #0a1020; }
  #log-c2 { background: #0a0a0a; border-color: #141414; }

  /* log entry colors */
  .e-c2    { color: #3a3a3a; }
  .e-c2.r  { color: #888; }
  .e-sp    { color: #2a3a2e; }
  .e-sp.r  { color: #4a8a5a; }
  .e-fa    { color: #243040; }
  .e-fa.r  { color: #4a7aaa; }

  /* memory panel */
  .mem-inner { display: flex; flex-direction: column; gap: 0.5rem; }
  .mem-row-controls { display: flex; gap: 0.45rem; flex-wrap: wrap; align-items: center; }
  .mem-row-controls input {
    background: #0c0c0c; border: 1px solid #1e1e1e; color: #ccc;
    padding: 0.35rem 0.65rem; font-family: 'Courier New', monospace;
    font-size: 0.68rem; border-radius: 4px; outline: none;
  }
  .mem-row-controls input:focus { border-color: #333; }
  #mem-q { flex: 1; min-width: 160px; }
  #mem-tag { width: 200px; font-size: 0.65rem; }

  .mem-results {
    background: #050505; border: 1px solid #131313; border-radius: 4px;
    padding: 0.7rem; min-height: 80px;
    max-height: calc(100vh - 380px);
    overflow-y: auto;
    font-size: 0.68rem; line-height: 1.7;
    scrollbar-width: thin; scrollbar-color: #1a1a1a #050505;
  }
  .mem-results::-webkit-scrollbar { width: 4px; }
  .mem-results::-webkit-scrollbar-thumb { background: #1a1a1a; }

  .mem-card { border-bottom: 1px solid #0e0e0e; padding: 0.5rem 0; }
  .mem-card:last-child { border-bottom: none; }
  .mem-meta { font-size: 0.60rem; color: #3a3a3a; margin-bottom: 0.2rem; display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  .mem-summary { color: #aaa; }
  .mem-empty { color: #2a2a2a; font-style: italic; }

  /* score bar */
  .score-bar {
    display: inline-block; width: 36px; height: 5px; border-radius: 2px;
    background: linear-gradient(to right, var(--bar-color) var(--w), #1a1a1a var(--w));
    vertical-align: middle; margin: 0 3px 1px 2px;
  }
  .score-hi  { --bar-color: #4ade80; color: #4ade80; }
  .score-mid { --bar-color: #facc15; color: #facc15; }
  .score-lo  { --bar-color: #555;    color: #555; }

  .pri-HIGH   { color: #4ade80; }
  .pri-MEDIUM { color: #facc15; }
  .pri-LOW    { color: #555; }

  .tag {
    display: inline-block; background: #111; border: 1px solid #1e1e1e;
    color: #4a5a6a; font-size: 0.56rem; padding: 0.05rem 0.35rem;
    border-radius: 3px; letter-spacing: 0.04em;
  }

  /* responsive */
  @media (max-width: 580px) {
    .grid-top { grid-template-columns: 1fr; }
    .hdr-c2 { display: none; } /* hide on very small screens */
    body { padding: 0.75rem; }
  }
</style>
</head>
<body>

<div class="header">
  <h1>AOL // C2</h1>
  <span id="ws-dot"></span>
  <div class="hdr-c2">
    <span class="hdr-stat">Idle <span id="idle" class="meta-val">—</span></span>
    <span class="hdr-sep">·</span>
    <span class="hdr-stat">Blackout <span id="blackout"><span class="gray">—</span></span></span>
    <span class="hdr-sep">·</span>
    <span class="hdr-stat">Override <span id="override"><span class="gray">—</span></span></span>
    <span class="hdr-sep">·</span>
    <button class="hdr-btn y" onclick="confirmApi('/restart','POST','Restart C2?')">&#8635; Restart</button>
    <button class="hdr-btn r" onclick="confirmApi('/shutdown','POST','Shutdown C2?')">&#9211; Shutdown</button>
  </div>
</div>

<div class="grid-top">

  <!-- SCREENPIPE -->
  <div class="panel panel-sp">
    <div class="panel-header">
      <span class="panel-title">Screenpipe</span>
      <span class="badge" id="sp-status"><span class="dot dot-dim"></span><span class="gray">—</span></span>
    </div>
    <div class="panel-meta">
      <div class="meta-row"><span class="meta-key">PID</span><span class="meta-val" id="sp-pid">—</span></div>
    </div>
    <div class="panel-actions">
      <button class="g" onclick="api('/start','POST')">&#9654; Start</button>
      <button class="r" onclick="api('/stop','POST')">&#9632; Stop</button>
      <button onclick="api('/resume','POST')">&#8635; Auto</button>
    </div>
  </div>

  <!-- FILTER AGENT -->
  <div class="panel panel-fa">
    <div class="panel-header">
      <span class="panel-title">Filter Agent</span>
      <span class="badge" id="fa-status"><span class="dot dot-dim"></span><span class="gray">—</span></span>
    </div>
    <div class="panel-meta">
      <div class="meta-row"><span class="meta-key">PID</span><span class="meta-val" id="fa-pid">—</span></div>
      <div class="meta-row"><span class="meta-key">Scheduled</span><span class="meta-val" id="fa-next">—</span></div>
    </div>
    <div class="panel-actions">
      <button class="b" onclick="api('/filter/start','POST')">&#9654; Run Now</button>
    </div>
  </div>


</div>

<!-- COLLAPSIBLE LOG STRIP -->
<div class="log-strip">

  <div class="log-section panel-sp" data-log="sp" data-open="false">
    <div class="log-toggle" onclick="toggleLog('sp')">
      <span class="log-label">Screenpipe Output</span>
      <span class="log-badge" id="log-badge-sp"></span>
      <span class="log-chevron" id="log-chev-sp">&#9654;</span>
    </div>
    <div class="log-box" id="log-sp" style="display:none"></div>
  </div>

  <div class="log-section panel-fa" data-log="fa" data-open="false">
    <div class="log-toggle" onclick="toggleLog('fa')">
      <span class="log-label">Filter Agent Output</span>
      <span class="log-badge" id="log-badge-fa"></span>
      <span class="log-chevron" id="log-chev-fa">&#9654;</span>
    </div>
    <div class="log-box" id="log-fa" style="display:none"></div>
  </div>

  <div class="log-section panel-c2" data-log="c2" data-open="true">
    <div class="log-toggle" onclick="toggleLog('c2')">
      <span class="log-label">C2 Log</span>
      <span class="log-badge" id="log-badge-c2"></span>
      <span class="log-chevron" id="log-chev-c2">&#9660;</span>
    </div>
    <div class="log-box" id="log-c2"></div>
  </div>

</div>

<!-- MEMORY INTELLIGENCE PANEL -->
<div class="panel panel-mem">
  <div class="panel-header">
    <span class="panel-title">Memory Intelligence</span>
  </div>
  <div class="mem-inner">
    <div class="mem-row-controls">
      <input id="mem-q" type="text" placeholder="semantic search memories..."
        onkeydown="if(event.key==='Enter')searchMem()">
      <input id="mem-tag" type="text" placeholder="tag filter (e.g. afrotc-deadline)"
        onkeydown="if(event.key==='Enter')searchMem()">
      <button class="b" onclick="searchMem()">&#128269; Search</button>
      <button onclick="recentMem()">&#9776; Recent</button>
    </div>
    <div class="mem-results" id="mem-results">
      <span class="mem-empty">Search memories above.</span>
    </div>
  </div>
</div>

<script>
const wsDot = document.getElementById('ws-dot');
let ws, pingTimer;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    wsDot.classList.add('live');
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
    wsDot.classList.remove('live');
    clearInterval(pingTimer);
    setTimeout(connect, 2500);
  };
  ws.onerror = () => ws.close();
}

function updateStatus(s) {
  const spOn = s.screenpipe === 'running';
  const faOn = s.filter_agent === 'running';

  document.getElementById('sp-status').innerHTML =
    `<span class="dot ${spOn?'dot-g':'dot-r'}"></span><span class="${spOn?'green':'red'}">${s.screenpipe.toUpperCase()}</span>`;
  document.getElementById('fa-status').innerHTML =
    `<span class="dot ${faOn?'dot-b':'dot-dim'}"></span><span class="${faOn?'blue':'gray'}">${faOn?'RUNNING':'IDLE'}</span>`;

  document.getElementById('sp-pid').textContent  = s.sp_pid ?? '—';
  document.getElementById('fa-pid').textContent  = s.fa_pid ?? '—';
  document.getElementById('fa-next').textContent = s.fa_next_run ?? '—';

  document.getElementById('idle').textContent = s.idle_display ?? '—';
  document.getElementById('blackout').innerHTML = s.blackout
    ? '<span class="yellow">ACTIVE</span>' : '<span class="gray">INACTIVE</span>';
  document.getElementById('override').innerHTML = s.override_pause
    ? '<span class="yellow">PAUSED</span>' : '<span class="green">AUTO</span>';
}

// Newest entries at top. Pin to top when user is already at top.
// When section is collapsed, update badge count instead of rendering.
function renderLog(boxId, entries, cls) {
  const box = document.getElementById(boxId);
  const sec = document.querySelector(`[data-log="${cls}"]`);
  const badge = document.getElementById(`log-badge-${cls}`);

  if (!entries?.length) {
    if (sec && sec.dataset.open !== 'false') {
      box.innerHTML = '<span class="log-empty">no output yet</span>';
    }
    return;
  }

  if (sec && sec.dataset.open === 'false') {
    badge.textContent = `+${entries.length}`;
    badge.style.display = 'inline';
    return;
  }

  const atTop = box.scrollTop <= 4;
  box.innerHTML = entries.slice().reverse().map((e, i) =>
    `<div class="e-${cls}${i < 5 ? ' r' : ''}">${escHtml(e)}</div>`
  ).join('');
  if (atTop) box.scrollTop = 0;
}

function toggleLog(name) {
  const sec = document.querySelector(`[data-log="${name}"]`);
  const box = document.getElementById(`log-${name}`);
  const open = sec.dataset.open === 'true';
  sec.dataset.open = String(!open);
  box.style.display = open ? 'none' : 'block';
  document.getElementById(`log-chev-${name}`).innerHTML = open ? '&#9654;' : '&#9660;';
  if (!open) document.getElementById(`log-badge-${name}`).style.display = 'none';
}

async function api(ep, method = 'GET') {
  await fetch(ep, { method }).catch(() => {});
}

async function confirmApi(ep, method, msg) {
  if (!confirm(msg)) return;
  await fetch(ep, { method }).catch(() => {});
}

async function searchMem() {
  const q = document.getElementById('mem-q').value.trim();
  const tag = document.getElementById('mem-tag').value.trim();
  if (!q) return;
  document.getElementById('mem-results').innerHTML = '<span class="mem-empty">searching...</span>';
  let url = `/memory/search?q=${encodeURIComponent(q)}&limit=15`;
  if (tag) url += `&domain=${encodeURIComponent(tag)}`;
  const data = await fetch(url).then(r => r.json()).catch(e => ({ error: String(e) }));
  renderMemResults(data.results, data.error);
}

async function recentMem() {
  const tag = document.getElementById('mem-tag').value.trim();
  document.getElementById('mem-results').innerHTML = '<span class="mem-empty">loading...</span>';
  let url = '/memory/recent?limit=20';
  if (tag) url += `&domain=${encodeURIComponent(tag)}`;
  const data = await fetch(url).then(r => r.json()).catch(e => ({ error: String(e) }));
  renderMemResults(data.results, data.error);
}

function scoreHtml(s) {
  if (s == null) return '';
  const pct = Math.round(s * 100);
  const cls = s >= 0.75 ? 'score-hi' : s >= 0.55 ? 'score-mid' : 'score-lo';
  return `<span class="score-bar ${cls}" style="--w:${pct}%" title="${pct}% match"></span><span class="${cls}">${pct}%</span>`;
}

function renderMemResults(items, error) {
  const box = document.getElementById('mem-results');
  if (error) { box.innerHTML = `<span style="color:#f87171">error: ${escHtml(error)}</span>`; return; }
  if (!items?.length) { box.innerHTML = '<span class="mem-empty">no results</span>'; return; }
  box.innerHTML = items.map(m => {
    let tags = [];
    try { tags = JSON.parse(m.tags || '[]'); } catch(e) {}
    const tagHtml = tags.map(t => `<span class="tag">${escHtml(t)}</span>`).join(' ');
    const ts = m.timestamp ? `<span>${escHtml(m.timestamp.slice(0,16))}</span>` : '';
    const sc = m.score != null ? scoreHtml(m.score) : '';
    return `<div class="mem-card">
      <div class="mem-meta">
        <span class="pri-${m.priority||'LOW'}">${m.priority||'?'}</span>
        ${ts}${sc}
        ${tagHtml}
      </div>
      <div class="mem-summary">${escHtml(m.summary || '')}</div>
    </div>`;
  }).join('');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

connect();
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=HTML)
