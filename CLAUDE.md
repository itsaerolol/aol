# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Screenpipe AOL (Autonomous Operational Layer) is a personal memory and activity monitoring system. It captures screen/audio data via the external Screenpipe service, then uses an LLM to extract operationally significant memories and store them in a vector database and knowledge graph.

## Running the System

```bash
# Start the full system (entry point — orchestrates all subprocesses)
python aol_c2.py
# Dashboard available at http://localhost:45139

# Run filter agent standalone (for testing memory extraction)
python filter_agent.py
```

The C2 service manages two child processes:
- **Screenpipe recorder**: `npx screenpipe@latest record` (screen/audio capture)
- **Filter Agent**: `python filter_agent.py` (memory extraction, polls every 12 hours)

Filter Agent runs in lockstep with Screenpipe — it starts and stops together with it across all conditions (idle, blackout, manual override).

## Environment Setup

Requires a `.env` file with:
```
ANTHROPIC_API_KEY=...
SCREENPIPE_API_KEY=...        # optional, for Screenpipe cloud
FILTER_AGENT_PYTHON=...       # optional: full path to Python interpreter that has mempalace installed
                              # defaults to sys.executable (same Python running C2)
```

Dependencies: `fastapi`, `uvicorn`, `psutil`, `pynput`, `requests`, `python-dotenv`, `chromadb`, `mempalace`, `anthropic`

## Module Structure

```
aol_c2.py              # Entry point — 12 lines, just runs uvicorn
server.py              # FastAPI app + lifespan + control/log routes — mounts 3 routers
├── ws.py              # WSManager, build_status(), push_loop(), /ws WebSocket endpoint
├── memory_api.py      # APIRouter at /memory — /search, /recent, /kg
└── dashboard.py       # Dashboard HTML string + / route
config.py              # All constants: ports, thresholds, paths, model names
logs.py                # Three circular log buffers (200-item cap) + log functions
activity.py            # pynput mouse/keyboard listeners, idle_seconds(), in_blackout()
screenpipe.py          # Screenpipe subprocess lifecycle: start(), stop(), running(), pid()
filter_controller.py   # Filter Agent subprocess lifecycle (same API shape as screenpipe.py)
control_loop.py        # Orchestration loop: idle/blackout/override → start/stop both processes
filter_agent.py        # Memory extraction agent (runs as subprocess, also standalone)
plugins/
├── base.py            # AOLPlugin ABC: process(), on_start(), on_stop()
├── __init__.py        # load_plugins() — auto-discovers .py files in plugins/
└── daily_digest.py    # Writes nightly HIGH/MEDIUM memory digest to ~/aol-memory/digests/
legacy versions/       # Historical v1 and v3 — kept for reference, not active
```

## Architecture

### Entry & Server Layer
- `aol_c2.py` — runs `uvicorn` on port `45139`, nothing else
- `server.py` — lifespan starts activity listeners + control loop thread + WebSocket push task; mounts ws/memory_api/html routers; owns control endpoints (`/start`, `/stop`, `/resume`, `/filter/start`, `/filter/stop`, `/status`) and raw log endpoints (`/logs`, `/screenpipe-logs`, `/filter-logs`)
- `ws.py` — WebSocket at `/ws`; pushes `{status, c2_logs, sp_logs, fa_logs}` to all clients every 1 second; dashboard polls this instead of HTTP
- `dashboard.py` — self-contained dashboard: status cards, action buttons, 3 live log panels, memory search panel, KG triple viewer
- `memory_api.py` — `/memory/search` (semantic ChromaDB), `/memory/recent`, `/memory/kg` (KG triple lookup by entity)

### Control Layer
- `control_loop.py` — runs on a daemon thread; checks idle/blackout/override every 30 seconds; stops/starts both Screenpipe and Filter Agent together
- `activity.py` — tracks last input timestamp via pynput; exposes `idle_seconds()`, `in_blackout()`, `last_activity_time()`
- `screenpipe.py` / `filter_controller.py` — each owns one subprocess with a thread-safe lock, stderr/stdout drain thread, and `start()` / `stop()` / `running()` / `pid()` interface

### Filter Agent (`filter_agent.py`)
- Polls Screenpipe `/activity-summary` every 12 hours; falls back to raw `/search` OCR+audio
- Uses Claude (`claude-opus-4-7`) to extract structured memories and knowledge graph facts
- Writes HIGH/MEDIUM memories to ChromaDB and KG triples to SQLite via mempalace; JSONL fallback if libraries unavailable
- Every 5 cycles runs a synthesis pass: promotes repeated session facts to permanent KG entries
- Loads plugins from `plugins/` on startup; calls `plugin.process(result, context, source)` after each cycle

### Plugin System
Drop a `.py` file in `plugins/`, subclass `AOLPlugin`, implement `process()`. Loaded automatically on filter agent startup. `on_start()` and `on_stop()` called at agent lifecycle boundaries.

## Key Design Decisions

- **FA follows SP** — Filter Agent starts and stops in lockstep with Screenpipe. No point running FA during idle/blackout since no new data is being recorded.
- **WebSocket over polling** — dashboard uses a persistent WebSocket connection instead of HTTP polling; 1-second push latency vs. 3-second poll.
- **FILTER_AGENT_PYTHON env var** — if mempalace is installed under a different Python interpreter than the one running C2, set this in `.env`. Filter agent logs which interpreter it's using and warns on startup if mempalace is not importable.
- **Single entry point** — always start via `aol_c2.py`. Don't run Screenpipe or filter_agent manually in production.
- **Two-stage fallback** — filter agent tries `/activity-summary` first (pre-compressed), falls back to raw `/search` OCR+audio if empty.
- **Memory domains** — Developer, AFROTC, Faith, Creative, Finance. Gaming, streaming, idle, duplicate content are discarded.
