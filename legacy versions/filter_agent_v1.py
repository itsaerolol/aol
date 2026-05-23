"""
AOL Filter Agent
Primary source: /activity-summary (pre-compressed, ~200-500 tokens/cycle).
Fallback: raw /search OCR + audio if activity summary is empty.
LLM: Groq compound model.
Memory: ChromaDB (via MempalaceConfig) + KnowledgeGraph for entity tracking.

Env vars required: SCREENPIPE_API_KEY, GROQ_API_KEY
Run: py digest_agent.py
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
load_dotenv()  # reads .env from current directory

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCREENPIPE_API      = "http://localhost:3030"
SCREENPIPE_API_KEY  = os.environ.get("SCREENPIPE_API_KEY", "")
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
GROQ_ENDPOINT       = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL          = "groq/compound"

POLL_INTERVAL_MIN   = 60        # how often to run a cycle
LOOKBACK_BUFFER_MIN = 5         # overlap window to avoid gaps at cycle boundaries
SEARCH_LIMIT        = 150       # max items when falling back to raw /search

MEMPALACE_PATH      = "~/aol-memory"
MEMPALACE_WING      = "aero-ops"
MEMPALACE_ROOM      = "daily-activity"
MEMPALACE_KG_ENTITY = "aero"   # operator identity node in the knowledge graph

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("digest")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sp_headers() -> dict:
    return {"Authorization": f"Bearer {SCREENPIPE_API_KEY}"}


def time_window(minutes_back: int) -> tuple[str, str]:
    now   = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes_back)
    fmt   = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), now.strftime(fmt)

# ---------------------------------------------------------------------------
# Screenpipe — primary: /activity-summary
# ---------------------------------------------------------------------------

def fetch_activity_summary(minutes_back: int) -> dict | None:
    start, end = time_window(minutes_back)
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/activity-summary",
            headers=sp_headers(),
            params={"start_time": start, "end_time": end},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        # non-empty if there are frames or audio
        if data.get("total_frames", 0) > 0 or data.get("audio_summary", {}).get("segment_count", 0) > 0:
            return data
        return None
    except Exception as e:
        log.warning(f"activity-summary fetch failed: {e}")
        return None


def format_activity_summary(summary: dict) -> str:
    """
    Convert /activity-summary response into a dense prompt block.
    Schema: apps[], recent_texts[], audio_summary{segment_count, speakers[]}, total_frames
    """
    lines = []

    # App usage — name + minutes (skip sub-1-min entries, they're noise)
    apps = [a for a in summary.get("apps", []) if a.get("minutes", 0) >= 1]
    if apps:
        usage = ", ".join(f"{a['name']} ({a['minutes']:.0f}m)" for a in apps)
        lines.append(f"APP USAGE: {usage}")

    # Recent screen texts
    for t in summary.get("recent_texts", []):
        text = t.get("text", "").strip().replace("\n", " ")
        app  = t.get("app_name", "")
        ts   = t.get("timestamp", "")
        if text:
            lines.append(f"[SCREEN|{app}|{ts}] {text[:400]}")

    # Audio summary — segment count + speakers (no raw transcription here)
    audio = summary.get("audio_summary", {})
    seg_count = audio.get("segment_count", 0)
    speakers  = [s.get("name", "unknown") for s in audio.get("speakers", [])]
    if seg_count > 0:
        spk_str = ", ".join(speakers) if speakers else "unidentified"
        lines.append(f"AUDIO: {seg_count} segments | speakers: {spk_str}")

    return "\n".join(lines) if lines else "(empty)"

# ---------------------------------------------------------------------------
# Screenpipe — fallback: /search raw OCR + audio
# ---------------------------------------------------------------------------

def fetch_search(content_type: str, minutes_back: int) -> list[dict]:
    start, end = time_window(minutes_back)
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/search",
            headers=sp_headers(),
            params={
                "content_type": content_type,
                "start_time":   start,
                "end_time":     end,
                "limit":        SEARCH_LIMIT,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.warning(f"/search fallback failed ({content_type}): {e}")
        return []


def format_raw_events(events: list[dict]) -> str:
    """
    Format raw /search results.
    OCR items:   type=OCR,   content.{text, app_name, window_name, timestamp}
    Audio items: type=Audio, content.{transcription, timestamp, device_name}
    UI items:    type=UI,    content.{text, app_name, window_name, timestamp}
    """
    lines = []
    for e in events:
        etype = e.get("type", "")
        c     = e.get("content", {})

        if etype == "OCR":
            text = c.get("text", "").strip().replace("\n", " ")
            if text:
                lines.append(f"[OCR|{c.get('app_name','')}|{c.get('timestamp','')}] {text[:400]}")

        elif etype == "Audio":
            text = c.get("transcription", "").strip().replace("\n", " ")
            if text:
                lines.append(f"[AUDIO|{c.get('device_name','')}|{c.get('timestamp','')}] {text[:400]}")

        elif etype == "UI":
            text = c.get("text", "").strip().replace("\n", " ")
            if text:
                lines.append(f"[UI|{c.get('app_name','')}|{c.get('timestamp','')}] {text[:400]}")

    return "\n".join(lines) if lines else "(no usable data)"


def fetch_context(minutes_back: int) -> tuple[str, str]:
    """
    Returns (formatted_context, source_label).
    Tries /activity-summary first. Falls back to raw /search.
    """
    summary = fetch_activity_summary(minutes_back)
    if summary:
        log.info(
            f"activity-summary: {summary.get('total_frames',0)} frames, "
            f"{summary.get('audio_summary',{}).get('segment_count',0)} audio segments"
        )
        return format_activity_summary(summary), "activity-summary"

    log.info("activity-summary empty — falling back to raw /search")
    ocr   = fetch_search("ocr",   minutes_back)
    audio = fetch_search("audio", minutes_back)
    log.info(f"raw search: {len(ocr)} OCR + {len(audio)} audio")
    return format_raw_events(ocr + audio), "raw-search"

# ---------------------------------------------------------------------------
# Groq LLM pass
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are AOL Digest — a precision memory filter for a disciplined, systems-oriented operator named Aero.

MISSION: Parse raw screen/audio capture context. Extract only what is operationally significant. Discard noise.

DOMAINS TO TRACK:
- Developer: AOL system, Evergreen iOS app, USAF Fitness Tracker, Whiteframe, ACE ISLAND
- AFROTC: tasks, deadlines, duty work, training requirements
- Faith: The Consecration, Christianized Shinobi Framework, devotional work, theology
- Creative: photography jobs, camera work, visual projects
- Finance: financial decisions, budget reviews, tracking activity

DISCARD WITHOUT ENTRY:
- Gaming, entertainment, passive video/streaming
- Aimless social media, news browsing
- Idle time, screensavers, lock screens
- Duplicate or redundant content within the same session

PRIORITY RULES:
- HIGH: active work session confirmed (10+ min on a tracked app), decisions made, deadlines, commitments, milestones
- MEDIUM: research with clear intent, task planning, meaningful comms, learning tied to a domain
- Anything below MEDIUM: discard

OUTPUT: Return ONLY a valid JSON array. No preamble, no markdown, no explanation.

Each element must match this schema exactly:
{
  "priority": "HIGH" | "MEDIUM",
  "domain": "Developer" | "AFROTC" | "Faith" | "Creative" | "Finance",
  "project": "<specific project name, or null if domain-level>",
  "summary": "<1–2 sentence factual description of what occurred>",
  "tags": ["<tag>"],
  "timestamp": "<ISO 8601 timestamp, best estimate from context>"
}

If nothing is worth keeping, return exactly: []"""


def run_groq(context: str, source: str) -> list[dict]:
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    user_msg = f"Current time: {now_str}\nData source: {source}\n\n{context}"

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "model":                 GROQ_MODEL,
        "temperature":           0.2,
        "max_completion_tokens": 4096,
        "top_p":                 1,
        "stream":                False,
        "stop":                  None,
    }

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    try:
        r = requests.post(GROQ_ENDPOINT, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()

        # strip accidental markdown fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        memories = json.loads(raw)
        if not isinstance(memories, list):
            log.error(f"Groq returned non-list ({type(memories).__name__})")
            return []

        log.info(f"Groq returned {len(memories)} memory entries")
        return memories

    except json.JSONDecodeError:
        log.error(f"Groq response was not valid JSON:\n{raw[:300]}")
        return []
    except Exception as e:
        log.error(f"Groq request failed: {e}")
        return []

# ---------------------------------------------------------------------------
# MemPalace write
# ---------------------------------------------------------------------------

def write_mempalace(memories: list[dict]):
    """
    ChromaDB direct write + KnowledgeGraph entity tracking.

    palace_path: MEMPALACE_PATH (~/aol-memory) — set explicitly because
    MempalaceConfig.palace_path defaults to ~/.mempalace/palace, which won't
    match a palace initialized at a custom location.

    KnowledgeGraph db_path: co-located with the palace so everything stays
    under ~/aol-memory. Default KG path (~/.mempalace/knowledge_graph.sqlite3)
    would be a separate location.

    Embeddings: ChromaDB default for now. When Ollama is live, swap in:
      from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
      ef = OllamaEmbeddingFunction(url="http://localhost:11434", model_name="nomic-embed-text")
      collection = chroma.get_or_create_collection(..., embedding_function=ef)
    """
    if not memories:
        log.info("no memories to write")
        return

    try:
        import chromadb
        from mempalace.config import MempalaceConfig
        from mempalace.knowledge_graph import KnowledgeGraph

        palace_path = os.path.expanduser(MEMPALACE_PATH)
        kg_path     = os.path.join(palace_path, "knowledge_graph.sqlite3")
        collection_name = MempalaceConfig().collection_name  # mempalace_drawers

        today      = datetime.now().strftime("%Y-%m-%d")
        chroma     = chromadb.PersistentClient(path=palace_path)
        collection = chroma.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        kg = KnowledgeGraph(db_path=kg_path)

        for mem in memories:
            # stable ID: sha1 of timestamp + first 80 chars of summary
            uid = hashlib.sha1(
                f"{mem.get('timestamp','')}:{mem['summary'][:80]}".encode()
            ).hexdigest()[:20]

            # --- ChromaDB (vector layer) ---
            collection.upsert(
                documents=[mem["summary"]],
                metadatas=[{
                    "wing":      MEMPALACE_WING,
                    "room":      MEMPALACE_ROOM,
                    "priority":  mem["priority"],
                    "domain":    mem["domain"],
                    "project":   mem.get("project") or "",
                    "tags":      json.dumps(mem.get("tags", [])),
                    "timestamp": mem.get("timestamp", datetime.now().isoformat()),
                }],
                ids=[uid],
            )

            # --- KnowledgeGraph (temporal entity layer) ---
            domain  = mem["domain"]
            project = mem.get("project")

            kg.add_entity(domain, entity_type="domain")
            kg.add_triple(MEMPALACE_KG_ENTITY, "active_in", domain, valid_from=today)

            if project:
                kg.add_entity(project, entity_type="project")
                kg.add_triple(MEMPALACE_KG_ENTITY, "worked_on", project, valid_from=today)

        log.info(
            f"wrote {len(memories)} entries → "
            f"ChromaDB ({collection_name}) + KG"
        )

    except ImportError as e:
        log.warning(f"MemPalace/ChromaDB import failed ({e}) — writing to fallback JSONL")
        _write_fallback(memories)
    except Exception as e:
        log.error(f"MemPalace write error: {e} — writing to fallback JSONL")
        _write_fallback(memories)


def _write_fallback(memories: list[dict]):
    path = os.path.join(os.path.expanduser(MEMPALACE_PATH), "digest_fallback.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for mem in memories:
            f.write(json.dumps(mem, ensure_ascii=False) + "\n")
    log.info(f"fallback: wrote {len(memories)} entries → {path}")

# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_env():
    missing = [k for k in ("SCREENPIPE_API_KEY", "GROQ_API_KEY") if not os.environ.get(k)]
    if missing:
        log.error(f"missing env vars: {', '.join(missing)}")
        raise SystemExit(1)


def check_screenpipe():
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/health",
            headers=sp_headers(),
            timeout=5,
        )
        r.raise_for_status()
        log.info("screenpipe health OK")
    except Exception as e:
        log.warning(f"screenpipe health check failed: {e} — will retry on next cycle")

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    check_env()
    log.info(f"AOL Digest Agent | poll: {POLL_INTERVAL_MIN}m | model: {GROQ_MODEL}")
    check_screenpipe()

    while True:
        log.info("--- cycle start ---")

        lookback          = POLL_INTERVAL_MIN + LOOKBACK_BUFFER_MIN
        context, source   = fetch_context(lookback)

        if context.strip() not in ("(empty)", "(no usable data)"):
            memories = run_groq(context, source)
            write_mempalace(memories)
        else:
            log.info("no content this cycle, skipping LLM pass")

        log.info(f"cycle complete — sleeping {POLL_INTERVAL_MIN}m")
        time.sleep(POLL_INTERVAL_MIN * 60)


if __name__ == "__main__":
    run()
