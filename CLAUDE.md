# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Screenpipe AOL (Autonomous Operational Layer) is a personal memory and activity monitoring system. It captures screen/audio data via the external Screenpipe service, then uses an LLM to extract operationally significant memories and store them in a vector database and knowledge graph.

## Running the System

```bash
# Start the full C2 dashboard (orchestrates all subprocesses)
python aol_c2.py
# Dashboard available at http://localhost:45139

# Run filter agent standalone (for testing memory extraction)
python filter_agent.py
```

The C2 service manages two child processes:
- **Screenpipe recorder**: `npx screenpipe@latest record` (screen/audio capture)
- **Filter Agent**: `python filter_agent.py` (memory extraction, polls every 60 min)

## Environment Setup

Requires a `.env` file with:
```
GROQ_API_KEY=...
SCREENPIPE_API_KEY=...   # optional, for Screenpipe cloud
```

Dependencies: `fastapi`, `uvicorn`, `psutil`, `pynput`, `requests`, `python-dotenv`, `chromadb`, `mempalace`

## Architecture

### `aol_c2.py` — Command & Control Server
- FastAPI server on port `45139`
- Tracks user activity via `pynput` mouse/keyboard listeners
- Idle detection threshold: 10 minutes
- Blackout window (no recording): 4:00–10:00 AM by default
- Maintains three circular log buffers (200-item cap each): C2, Screenpipe, Filter Agent
- REST endpoints: `/start`, `/stop`, `/resume`, `/status`, `/logs`
- Serves an HTML dashboard with 3-panel real-time log viewer

### `filter_agent.py` — Memory Extraction Agent
- Polls Screenpipe API every 60 minutes
- Uses Groq LLM (`compound` model) to extract memories
- Data pipeline: `/activity-summary` endpoint → fallback to raw `/search` OCR+audio
- Tracked memory domains: Developer, AFROTC, Faith, Creative, Finance
- Filters out: gaming, streaming, idle time, duplicate content
- Writes HIGH/MEDIUM priority memories to:
  - **ChromaDB** (vector store) for semantic search
  - **Knowledge Graph** (SQLite via mempalace) for entity relationships
  - **JSONL fallback** if libraries unavailable

### Process Management Pattern
Both subprocesses use a thread-safe lock + polling loop with interruptible sleep. Auto-restart on crash. Graceful shutdown via signal handlers.

## Key Design Decisions

- The C2 server is the single entry point — don't run Screenpipe or filter_agent manually in production.
- Activity tracking pauses recording during idle/blackout windows to avoid storing irrelevant data.
- The filter agent uses a two-stage fallback: structured activity summary first, raw OCR/audio second.
- `legacy versions/` contains historical v1 and v3 — kept for reference, not active.
