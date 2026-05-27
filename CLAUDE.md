# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Screenpipe AOL (Autonomous Operational Layer) is a personal memory and activity monitoring system. It captures screen/audio data via the external Screenpipe service, then uses Claude (Anthropic API) to extract operationally significant memories and store them in a vector database and knowledge graph.

## Running the System

```bash
# Start the full system (entry point — orchestrates all subprocesses)
python aol_c2.py
# Dashboard available at http://localhost:45139

# Run filter agent standalone (one-shot, for testing)
python filter_agent.py
```

The C2 service manages one persistent child process and one scheduled daily process:
- **Screenpipe recorder**: `npx screenpipe@latest record` (screen/audio capture)
- **Filter Agent**: `python filter_agent.py` — runs once per day at 21:30, launched by `control_loop.py`, exits after one cycle

## Environment Setup

Requires a `.env` file with:
```
ANTHROPIC_API_KEY=...
SCREENPIPE_API_KEY=...        # optional, for Screenpipe cloud
FILTER_AGENT_PYTHON=...       # optional: full path to Python interpreter that has mempalace installed
                              # defaults to sys.executable (same Python running C2)
```

Dependencies: `fastapi`, `uvicorn`, `psutil`, `pynput`, `requests`, `python-dotenv`, `chromadb`, `mempalace`

## Architecture

### `aol_c2.py` — Entry Point
Launches uvicorn on port `45139`. Imports `server.py`.

### `server.py` — FastAPI App
- Lifespan starts activity listeners + control loop thread
- REST endpoints: `/start`, `/stop`, `/resume`, `/status`, `/logs`
- `/shutdown` — graceful shutdown (stops children, exits process)
- `/restart` — graceful shutdown + detached relaunch after 2s port-free delay
- Serves dashboard at `/`

### `control_loop.py` — Orchestration
- Runs every 30 seconds (`CHECK_INTERVAL_SECONDS`)
- Screenpipe: starts on boot (if not blackout), stops on idle >10 min, resumes on activity
- Filter Agent: **time-based trigger** — fires at 21:30 daily regardless of idle state
  - `_fa_due()` checks current time vs `FILTER_AGENT_HOUR/MINUTE` from config
  - `_fa_last_run_date` persisted in `filter_metadata.json` so C2 restarts don't double-fire
  - Blackout (4–10 AM) kills a mid-run FA; idle state does NOT block the scheduled run

### `filter_agent.py` — Memory Extraction (One-Shot)
- **Execution model**: runs one cycle, writes output, exits (code 0). No internal loop or sleep.
- **Schedule**: launched by `control_loop.py` at 21:30. Can also be triggered manually via dashboard FA Start button.
- **LLM**: Claude (`claude-sonnet-4-6`) via Anthropic Messages API
- **Lookback window**: 1500 min (~25 hours) to handle slight timing drift
- **Data pipeline**: `/activity-summary` endpoint → fallback to raw `/search` OCR+audio
- **Grounding context** (injected into each LLM call before capture data):
  1. `MemoryStack.wake_up(wing="aero-ops")` — L0 identity + L1 room snapshots
  2. Current KG facts for entity `aero` — prevents re-asserting known stable facts
  3. All existing tags from ChromaDB metadata — prevents tag duplication (same pattern as KG dedup)
- **Anti-hallucination policy** (small/local model compatibility):
  - Output bounded: max 2 memories per cycle; 3rd only if one is HIGH priority
  - Rejection policy in all 3 prompts: "if uncertain, omit — an empty result is valid"
  - Format lock in all 3 prompts: output ONLY the JSON structure, no prose or fences
  - Pre-write validation drops any memory/KG fact that fails schema (wrong priority value, non-list tags, malformed tag format `^[a-z][a-z0-9-]*$`, empty evidence, non-snake_case predicate, invalid durability)
  - All drops logged with field name + value for debugging model output
- **Memory schema**: `priority`, `summary`, `tags[]`, `timestamp` — no domain enum, no project field. Tags are free-form, lowercase, hyphenated (e.g. `aol-system`, `afrotc-deadline`, `new-pattern`).
- **Writes to**:
  - **ChromaDB** (vector store at `~/aol-memory`) for semantic search
  - **KnowledgeGraph** (SQLite at `~/aol-memory/knowledge_graph.sqlite3`) — stable facts only, no per-cycle snapshots
  - **JSONL fallback** (`~/aol-memory/filter_fallback.jsonl`) if libraries unavailable
- **Synthesis pass**: every 5 days (`SYNTHESIS_INTERVAL_DAYS`), promotes repeated session/weekly KG facts to permanent. Date tracked in `filter_metadata.json` (`last_synthesis_date`).
- **Learn pass**: every 7 days (`LEARN_INTERVAL_DAYS`), rewrites the 4 stable MemPalace rooms (`working-style`, `projects-active`, `tech-environment`, `workspace-map`) by feeding Claude the last 14 days of drawers + all KG facts. Output replaces existing room ChromaDB entries via delete+upsert with deterministic IDs (`room:aero-ops:<room-name>`). Date tracked in `filter_metadata.json` (`last_learn_date`).
- **Metadata file**: `~/aol-memory/filter_metadata.json` — persists `last_run_date` and `last_synthesis_date` across invocations.

### `activity.py` — Input Tracking
- `pynput` listeners for mouse clicks and keyboard presses only (mouse movement ignored)
- `idle_seconds()`, `is_idle()`, `in_blackout()` used by control loop

### `filter_controller.py` — Subprocess Lifecycle
- `start()` spawns FA as a subprocess, drains stdout to FA log buffer
- `stop()` terminates with 10s timeout then kill
- `running()` uses `poll()` — returns False as soon as FA exits naturally
- No auto-restart after natural exit (exit code 0); restart only comes from next `_fa_due()` trigger

### `memory_api.py` — Memory Query Endpoints
- `/memory/search?q=...&domain=...` — semantic search over ChromaDB
- `/memory/recent?domain=...` — most recent entries
- `/memory/kg?subject=aero` — KG triples for an entity
- `domain` filter matches both old `domain` metadata field (legacy) and new `tags` array (current schema)

### `dashboard.py` — Web UI
- WebSocket-pushed status (1s latency), 3-panel sectioned layout
- **Screenpipe panel**: Start / Stop / Auto buttons + SP output terminal
- **Filter Agent panel**: Run Now only (no Stop — FA is one-shot). Shows next scheduled run time + FA output terminal
- **C2 panel**: Idle / Blackout / Override status + Restart / Shutdown buttons + C2 log terminal
- **Memory panel**: semantic search + tag filter (free-form, not domain enum) + KG lookup. Results show tags as pills, not legacy domain/project fields
- Log terminals newest-at-top; view pins to top unless user has scrolled down
- Restart/Shutdown require browser confirm dialog

## MemPalace Setup

- Wing: `aero-ops`
- Rooms: `daily-activity`, `identity`, `working-style`, `projects-active`, `tech-environment`, `workspace-map`, `aol-system`
- Identity file (L0): `~/.mempalace/identity.txt` (not in the palace directory)
- Palace path: `~/aol-memory`

## Key Design Decisions

- **FA is one-shot** — FA exits after each daily run. No sleeping subprocess. Scheduling is `control_loop.py`'s responsibility.
- **FA decoupled from SP** — Screenpipe still follows idle/blackout. FA fires at 21:30 regardless of idle state (24h window always has data).
- **No FA Stop button** — FA is one-shot; it exits naturally. Dashboard exposes only "Run Now" for manual trigger.
- **Free-form tags over domain enum** — novel activity gets tagged and stored, not discarded. `new-pattern` tag for unrecognized activity.
- **KG is stable facts only** — per-cycle domain/project triples removed. Drawers handle episodic memory; KG handles durable entity facts.
- **Grounding before generation** — each FA cycle injects existing memory context + KG facts into the prompt before processing new data.
- **WebSocket over polling** — dashboard uses persistent WebSocket; 1s push latency.
- **FILTER_AGENT_PYTHON env var** — if mempalace is on a different Python interpreter than C2, set this in `.env`.
- **Two-stage fallback** — FA tries `/activity-summary` first, falls back to raw `/search` OCR+audio.
- **Single entry point** — always start via `aol_c2.py`. Don't run Screenpipe or filter_agent manually in production.

## TODO
[ ] [AUTO] There is no fallback incase the C2 is not running, and it misses a day. It should be able to catch up. propose solutions