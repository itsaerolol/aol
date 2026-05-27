"""
AOL Filter Agent
Runs once per day at 21:30, launched by control_loop.py. Fetches the past 24 hours of
Screenpipe data, runs a Claude LLM pass to extract operationally significant memories
AND rich knowledge graph facts, writes HIGH/MEDIUM entries to MemPalace. Exits cleanly
after one cycle — no internal loop or sleep.

Primary source : /activity-summary (~200-500 tokens, pre-compressed)
Fallback        : raw /search OCR + audio
LLM             : Claude (Anthropic Messages API)
Memory          : ChromaDB (drawers) + KnowledgeGraph (structured triples)

Env vars: SCREENPIPE_API_KEY, ANTHROPIC_API_KEY  (loaded from .env automatically)
Run     : py filter_agent.py
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCREENPIPE_API      = "http://localhost:3030"
SCREENPIPE_API_KEY  = os.environ.get("SCREENPIPE_API_KEY", "")
C2_API              = "http://localhost:45139"
SP_BOOT_WAIT_SECONDS = 60  # how long to wait for screenpipe to boot after FA starts it
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENDPOINT  = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL     = "claude-sonnet-4-6"
ANTHROPIC_VERSION   = "2023-06-01"

POLL_INTERVAL_MIN   = 1440  # 24 h — used for lookback calc; scheduling is handled externally
LOOKBACK_BUFFER_MIN = 60    # ~25-hour window to handle slight timing drift
SEARCH_LIMIT        = 150   # OCR
AUDIO_SEARCH_LIMIT  = 300   # 25h / 5min chunks = up to 300 entries

# Synthesis pass runs every N days (tracked durably in filter_metadata.json)
SYNTHESIS_INTERVAL_DAYS = 5

# Learn pass (room rewrite) runs every N days
LEARN_INTERVAL_DAYS = 7
LEARN_LOOKBACK_DAYS = 14
LEARN_ROOMS = ["working-style", "projects-active", "tech-environment", "workspace-map"]

MEMPALACE_PATH      = "~/aol-memory"
MEMPALACE_WING      = "aero-ops"
MEMPALACE_ROOM      = "daily-activity"
MEMPALACE_KG_ENTITY = "aero"

_AUDIO_DEVICE_LABELS: dict[str, tuple[str, str]] = {
    "Microphone (Razer Seiren Mini)":              ("AUDIO_OUT", "Aero"),
    "Headphones (HyperX Cloud Flight 2 Wireless)": ("AUDIO_IN",  "Inbound"),
}

# ---------------------------------------------------------------------------
# Logging — stdout so C2 can capture via PIPE
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("filter")

# ---------------------------------------------------------------------------
# Metadata helpers — durable state across invocations
# ---------------------------------------------------------------------------

def _meta_path() -> str:
    return os.path.join(os.path.expanduser(MEMPALACE_PATH), "filter_metadata.json")


def _load_metadata() -> dict:
    path = _meta_path()
    try:
        with open(path) as f:
            data = json.load(f)
        log.info(f"metadata loaded: {data}")
        return data
    except FileNotFoundError:
        log.info(f"metadata file not found ({path}) — starting fresh")
        return {}
    except Exception as e:
        log.warning(f"metadata load error: {e} — starting fresh")
        return {}


def _save_metadata(data: dict):
    path = _meta_path()
    log.info(f"saving metadata: {data} → {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info("metadata saved")


def _synthesis_due() -> bool:
    meta = _load_metadata()
    last = meta.get("last_synthesis_date")
    if not last:
        log.info("synthesis: no last_synthesis_date — synthesis due")
        return True
    try:
        delta = (date.today() - date.fromisoformat(last)).days
        due = delta >= SYNTHESIS_INTERVAL_DAYS
        log.info(f"synthesis: last run {last}, {delta} days ago — {'DUE' if due else f'not due (next in {SYNTHESIS_INTERVAL_DAYS - delta}d)'}")
        return due
    except Exception as e:
        log.warning(f"synthesis due check error: {e} — defaulting to due")
        return True


def _learn_due() -> bool:
    meta = _load_metadata()
    last = meta.get("last_learn_date")
    if not last:
        log.info("learn: no last_learn_date — learn due")
        return True
    try:
        delta = (date.today() - date.fromisoformat(last)).days
        due = delta >= LEARN_INTERVAL_DAYS
        log.info(f"learn: last run {last}, {delta} days ago — {'DUE' if due else f'not due (next in {LEARN_INTERVAL_DAYS - delta}d)'}")
        return due
    except Exception as e:
        log.warning(f"learn due check error: {e} — defaulting to due")
        return True

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
    log.info(f"fetching /activity-summary | window: {start} → {end} ({minutes_back}m)")
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/activity-summary",
            headers=sp_headers(),
            params={"start_time": start, "end_time": end},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        frames    = data.get("total_frames", 0)
        apps      = [a["name"] for a in data.get("apps", []) if a.get("minutes", 0) >= 1]
        seg_count = data.get("audio_summary", {}).get("segment_count", 0)
        log.info(f"activity-summary response: {frames} frames | {seg_count} audio segments | apps: {apps or '(none)'}")
        has_frames = frames > 0
        has_audio  = seg_count > 0
        if not has_frames and not has_audio:
            log.info("activity-summary has no frames and no audio — treating as empty")
            return None
        return data
    except Exception as e:
        log.warning(f"activity-summary fetch failed: {e}")
        return None


def format_hybrid(summary: dict | None, ocr_events: list, audio_events: list) -> str:
    lines = []
    if summary:
        apps = [a for a in summary.get("apps", []) if a.get("minutes", 0) >= 30]
        if apps:
            usage = ", ".join(f"{a['name']} ({a['minutes']:.0f}m)" for a in apps)
            lines.append(f"APP USAGE (≥30m): {usage}")
    for e in ocr_events:
        c = e.get("content", {})
        text = c.get("text", "").strip().replace("\n", " ")
        if text:
            lines.append(f"[OCR|{c.get('app_name','')}|{c.get('timestamp','')}] {text[:400]}")
    for e in audio_events:
        c = e.get("content", {})
        text = c.get("transcription", "").strip().replace("\n", " ")
        if text:
            device = c.get("device_name", "")
            tag, label = _AUDIO_DEVICE_LABELS.get(device, ("AUDIO", device))
            lines.append(f"[{tag}|{label}|{c.get('timestamp','')}] {text[:400]}")
    return "\n".join(lines) if lines else "(empty)"

# ---------------------------------------------------------------------------
# Screenpipe — /search (OCR + audio)
# ---------------------------------------------------------------------------

def fetch_search(content_type: str, minutes_back: int, limit: int = SEARCH_LIMIT) -> list[dict]:
    start, end = time_window(minutes_back)
    log.info(f"fetching /search?content_type={content_type} | window: {start} → {end} | limit: {limit}")
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/search",
            headers=sp_headers(),
            params={"content_type": content_type, "start_time": start,
                    "end_time": end, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("data", [])
        log.info(f"/search {content_type}: {len(results)} results")
        return results
    except Exception as e:
        log.warning(f"/search {content_type} failed: {e}")
        return []


def fetch_context(minutes_back: int) -> tuple[str, str]:
    log.info(f"fetch_context: lookback={minutes_back}m")
    summary      = fetch_activity_summary(minutes_back)
    ocr_events   = fetch_search("ocr",   minutes_back, limit=SEARCH_LIMIT)
    audio_events = fetch_search("audio", minutes_back, limit=AUDIO_SEARCH_LIMIT)
    log.info(f"raw totals: {len(ocr_events)} OCR | {len(audio_events)} audio")
    formatted = format_hybrid(summary, ocr_events, audio_events)
    log.info(f"hybrid context: {len(formatted)} chars")
    return formatted, "hybrid"

# ---------------------------------------------------------------------------
# Claude LLM pass — extracts memories AND rich KG facts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are AOL Filter, the memory intelligence layer for Aero — a systems-oriented developer, AFROTC cadet, and operator running a personal AI operating system (AOL).

Your job runs once per day at 21:30. The capture window covers the past 24 hours of the operator's activity. You receive raw screen + audio capture data from Screenpipe and return two things: structured memory entries for ChromaDB, and knowledge graph triples for MemPalace. You are the only thing standing between raw noise and a queryable, durable model of Aero's life and work.

Identify what mattered most today — decisions made, things learned, work completed, patterns noticed. Prefer entries that will still be meaningful a week from now over entries that only describe what was on screen at a given moment.

---

## WHO AERO IS

Name: Aero
KG entity key: "aero"

Current context about Aero — his active projects, tools, and state — is provided in the EXISTING MEMORY and EXISTING KG FACTS sections at the top of each request. Use that grounding to stay consistent with established facts and to understand what's worth capturing.

---

## INPUT FORMAT

You will receive:
- EXISTING MEMORY: recent memories already stored — use to pattern-match signal quality and avoid duplication
- EXISTING KG FACTS: current stable facts about Aero — do not re-assert these
- Current timestamp
- Data source: "activity-summary" or "raw-search"
- NEW CAPTURE DATA: app usage durations, OCR screen text, audio transcriptions, UI events

Audio direction tags:
- AUDIO_OUT = Aero's own voice (microphone). Attribute directly to Aero.
- AUDIO_IN  = audio Aero is receiving — Discord calls, videos, phone audio. These are
  OTHER PEOPLE speaking, not Aero. Do not attribute AUDIO_IN content to Aero's decisions,
  plans, or beliefs unless a corresponding AUDIO_OUT response confirms Aero engaged with it.

---

## OUTPUT FORMAT

Return ONLY valid JSON. No preamble, no markdown fences, no explanation outside the JSON.

{
  "memories": [ ... ],
  "kg_facts": [ ... ]
}

---

## PART 1 — MEMORIES

Extract only what is operationally significant. Signal-to-noise is everything.

KEEP:
- HIGH: decisions made, commitments given, milestones, deadlines, blockers confirmed — supported by transcription or OCR evidence
- MEDIUM: research with clear intent visible in OCR/audio, task planning, meaningful comms, learning tied to a domain

DISCARD ENTIRELY:
- No transcription or OCR content to support it — app usage time alone is not evidence
- Hedged or vague: could not state what was actually happening from the content
- Already captured: EXISTING MEMORY already covers this event
- Passive consumption with no visible artifact, decision, or output
- AUDIO_IN content with no AUDIO_OUT response — pure inbound audio Aero was passively hearing

APP USAGE (≥30m) is contextual signal only — it tells you what was open, not what happened. Do not write memories or KG facts based solely on time-in-app. Transcription and OCR are the primary evidence sources.

Tag what you observe faithfully. If activity doesn't fit a known pattern, describe it and tag it.
Novel things get tagged "new-pattern" plus the most descriptive label available.
Tags are lowercase, hyphenated. Examples: aol-system, faith-devotional, afrotc-deadline,
gaming-session, fitness-workout, consecration, developer-work, new-pattern

Memory schema:
{
  "priority": "HIGH" | "MEDIUM",
  "summary": "<1-2 sentences: what happened, what was decided, what matters>",
  "tags": ["<lowercase-hyphenated-tag>", "<tag2>"],
  "timestamp": "<ISO 8601>"
}

If nothing is worth keeping: {"memories": [], "kg_facts": []}

Extract at most 2 memories per cycle. A 3rd memory is valid only if classified HIGH priority — otherwise drop it. Never pad to fill a count.
If you are uncertain whether something qualifies, omit it. An empty memories array is correct output.

---

## PART 2 — KNOWLEDGE GRAPH FACTS

This is the most important part of your job. Extract every structural fact the session data supports. Be aggressive. 10-50 facts per session is normal. The goal is a KG that a future session can query to instantly know: what tools Aero uses, what projects are active, what's his stack, who he works with, what's his workflow, what's blocked.

IMPORTANT: EXISTING KG FACTS at the top of this request shows what is already known. Do NOT re-assert facts already present in that list. Only write facts that are new, updated, or not yet captured.

Use snake_case predicates. Be specific: "primary_ide" not "uses". "runs_on_port" not "runs_on".

KG fact schema:
{
  "subject": "<entity name>",
  "subject_type": "person" | "project" | "tool" | "service" | "domain" | "concept" | "organization",
  "predicate": "<snake_case relationship>",
  "object": "<entity name or scalar value>",
  "object_type": "person" | "project" | "tool" | "service" | "domain" | "concept" | "value" | "organization",
  "durability": "permanent" | "weekly" | "session",
  "confidence": "high" | "medium",
  "evidence": "<one sentence: what in the session data supports this inference>"
}

Durability rules:
- permanent: won't change — primary tools, project platforms, stack decisions, AFROTC unit, faith framework identity
- weekly: current focus, active tasks, what's in progress this sprint
- session: one-off observation, happened today, may not recur

---

### KG CATEGORIES — cover every applicable one:

TOOL & ENVIRONMENT
aero → primary_ide → <app>
aero → secondary_ide → <app>
aero → primary_browser → <app>
aero → terminal_emulator → <app>
aero → operating_system → <value>
aero → shell → <value>
aero → package_manager → <value>
aero → version_control → <value>

PROJECT
<project> → domain → Developer | AFROTC | Faith | Creative | Finance
<project> → platform → iOS | web | desktop | API | cross-platform
<project> → language → <value>
<project> → framework → <value>
<project> → database → <value>
<project> → hosting → Vercel | Supabase | AWS | self-hosted | etc.
<project> → status → active | paused | blocked | shipped
<project> → repo_url → <url>
<project> → deployment_url → <url>
<project> → collaborator → <person>
aero → currently_building → <project>        [durability: weekly]
aero → worked_on_today → <project>           [durability: session, one per project]

INFRASTRUCTURE
<service> → runs_on_port → <port>
<service> → runs_on_host → localhost | <hostname>
<service> → tech_stack → <value>
<service> → depends_on → <other service>
aero → local_service_running → <service>     [durability: session]
<project> → env_var_present → <KEY_NAME>     [when env vars visible in screen]

WORKFLOW & PATTERNS
aero → typical_work_start → <HH:MM>
aero → context_switches_between → "<app1>:<app2>"
aero → focuses_on → <domain>                 [when 30+ min single-domain]
aero → workflow_pattern → <observed pattern>
aero → prefers → <tool or approach>

AFROTC
aero → afrotc_rank → <value>
aero → afrotc_unit → <value>
aero → afrotc_detachment → <value>
aero → afrotc_deadline → "<event>:<YYYY-MM-DD>"
aero → afrotc_requirement → "<name>:<status>"
aero → afrotc_role → <value>

FAITH & FRAMEWORK
aero → active_spiritual_practice → <name>
aero → active_framework → <name>
<framework> → principle → <name>
aero → faith_domain_active → "true"          [durability: session]

PEOPLE & RELATIONSHIPS
aero → collaborates_with → <person>
aero → reports_to → <person>
aero → mentored_by → <person>
<person> → role → <value>
<person> → project → <project>
<person> → contacted_today → "true"          [durability: session]

CREATIVE
aero → active_photography_project → <name>
<project> → client → <name>
<project> → shoot_date → <YYYY-MM-DD>
<project> → deliverable → <value>

STATUS & TEMPORAL
aero → currently_focused_on → <project or area>   [durability: weekly]
aero → blocked_on → <issue>                        [durability: weekly]
<project> → deadline → <YYYY-MM-DD>
<project> → last_active → <YYYY-MM-DD>             [durability: session]
<task> → status → in_progress | blocked | done
aero → milestone_reached → <description>           [durability: session]

---

## SYNTHESIS AWARENESS

You are also capable of pattern recognition across facts. If the session data clearly shows a repeated behavior (e.g. Antigravity IDE used heavily across multiple references), you may write that pattern as a permanent fact even if this is the first session you are processing it. Evidence must be visible in the current data — don't fabricate patterns.

---

## RULES

1. Output ONLY the JSON object. No preamble, no markdown fences, no explanation outside the braces.
2. If nothing is worth keeping: {"memories": [], "kg_facts": []}
3. Never invent data not supported by the session input.
4. Prefer specific predicates over generic ones.
5. One kg_fact per subject-predicate-object combination — don't duplicate.
6. Do not re-assert KG facts already listed in EXISTING KG FACTS.
7. Evidence field is required on every kg_fact. One sentence, grounded in the input. If you cannot write it from the input text, do not emit the fact.
8. Write every fact the evidence supports. Omission is a failure mode.
9. When in doubt, omit. Accuracy over coverage at every level.
10. For tags: prefer tags from EXISTING TAGS before creating new ones. Only create a new tag if nothing in EXISTING TAGS fits.

Valid output example (2 memories, 2 facts):
{"memories":[{"priority":"HIGH","summary":"Pushed first working AOL cycle. Layer 2 confirmed live.","tags":["aol-system","milestone"],"timestamp":"2026-05-22T21:30:00"}],"kg_facts":[{"subject":"aol-c2","subject_type":"service","predicate":"runs_on_port","object":"45139","object_type":"value","durability":"permanent","confidence":"high","evidence":"Dashboard URL localhost:45139 confirmed in OCR."}]}"""


def run_claude(context: str, source: str) -> dict:
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M")
    palace_path = os.path.expanduser(MEMPALACE_PATH)

    # Predicates that are per-session snapshots — excluded from stable KG grounding
    _KG_SESSION_NOISE = {
        "session_time_in", "used_tool_this_session", "context_switches_between",
        "focuses_on", "worked_on", "typical_work_start", "hardware_brand",
        "communication_platform", "shell", "system_monitoring_tool",
        "primary_ai_assistant", "primary_ai_tool", "workflow_pattern",
    }

    # --- Grounding block 1: L0 identity one-liner ---
    log.info("grounding: loading L0 identity from MemoryStack.wake_up()")
    L0_line = "(no identity context)"
    try:
        from mempalace.layers import MemoryStack
        stack    = MemoryStack(palace_path=palace_path)
        wake_raw = stack.wake_up(wing=MEMPALACE_WING)
        L0_line  = wake_raw.split("\n")[0].strip()
        log.info(f"grounding L0: {len(L0_line)} chars — '{L0_line[:80]}...'")
    except Exception as e:
        log.warning(f"wake_up failed: {e} — using empty L0")

    # --- Grounding block 2: full ChromaDB room content (untruncated) ---
    log.info("grounding: loading full room content from ChromaDB")
    _GROUNDING_ROOMS = [
        "identity", "projects-active", "aol-system",
        "tech-environment", "working-style", "workspace-map",
    ]
    room_docs   = {}
    all_drawers = []
    try:
        import chromadb
        from mempalace.config import MempalaceConfig
        chroma  = chromadb.PersistentClient(path=palace_path)
        col     = chroma.get_or_create_collection(
            name=MempalaceConfig().collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        total_entries = col.count()
        log.info(f"ChromaDB: {total_entries} total entries in collection")
        res = col.get(limit=500, include=["documents", "metadatas"])
        for doc, meta in zip(res.get("documents", []), res.get("metadatas", [])):
            room = meta.get("room", "")
            if room in _GROUNDING_ROOMS and room not in room_docs:
                room_docs[room] = doc
                log.info(f"  room loaded: [{room}] ({len(doc)} chars)")
            if meta.get("timestamp") and meta.get("priority"):
                all_drawers.append({"doc": doc, "meta": meta})
        missing = [r for r in _GROUNDING_ROOMS if r not in room_docs]
        if missing:
            log.warning(f"  rooms not found in ChromaDB: {missing}")
    except Exception as e:
        log.warning(f"ChromaDB grounding fetch failed: {e}")

    full_rooms = [f"[{r}]\n{room_docs[r]}" for r in _GROUNDING_ROOMS if r in room_docs]
    full_room_block = "\n\n".join(full_rooms) if full_rooms else "(no room content)"
    log.info(f"grounding rooms: {len(full_rooms)}/{len(_GROUNDING_ROOMS)} loaded | {len(all_drawers)} drawers found | block: {len(full_room_block)} chars")

    memory_context = f"{L0_line}\n\n{full_room_block}" if L0_line else full_room_block

    # --- Grounding block 3: recent extraction examples (pattern/tag reference) ---
    log.info("grounding: building recent extraction examples")
    all_drawers.sort(key=lambda x: x["meta"].get("timestamp", ""), reverse=True)
    recent_lines = []
    for entry in all_drawers[:5]:
        doc      = entry["doc"]
        meta     = entry["meta"]
        ts       = meta.get("timestamp", "?")
        pri      = meta.get("priority", "?")
        tags_raw = meta.get("tags", "[]")
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
        tag_str = ", ".join(tags) if tags else "(none)"
        recent_lines.append(f"[{ts}] [{pri}] tags=[{tag_str}]\n  {doc[:200]}")

    _BOOTSTRAP = (
        "[BOOTSTRAP — vault empty. Target memory schema:]\n"
        'Example HIGH:   {"priority":"HIGH","summary":"First working AOL cycle completed. Layer 2 now live.","tags":["aol-system","developer","milestone"],"timestamp":"2026-05-22T21:30:00"}\n'
        'Example MEDIUM: {"priority":"MEDIUM","summary":"~45m debugging /activity-summary endpoint. OCR fallback confirmed.","tags":["aol-system","developer","debugging"],"timestamp":"2026-05-22T15:00:00"}\n'
        "Tag conventions: lowercase, hyphenated — aol-system, developer, afrotc-deadline, "
        "faith-devotional, fitness-workout, housing-research, creative-work, new-pattern"
    )
    recent_block = "\n\n".join(recent_lines) if recent_lines else _BOOTSTRAP
    log.info(f"grounding recent: {len(recent_lines)} examples {'(real)' if recent_lines else '(bootstrap fallback)'}")

    # --- Grounding block 4: stable KG facts (session noise filtered, deduped) ---
    log.info("grounding: loading stable KG facts from SQLite")
    kg_context = "(none yet)"
    try:
        import sqlite3 as _sqlite3
        kg_path  = os.path.join(palace_path, "knowledge_graph.sqlite3")
        con      = _sqlite3.connect(kg_path)
        cur      = con.cursor()
        tri_cols = [r[1] for r in cur.execute("PRAGMA table_info(triples)").fetchall()]
        rows     = cur.execute(
            "SELECT * FROM triples ORDER BY subject, predicate, object"
        ).fetchall()
        seen     = set()
        kg_lines = []
        for row in rows:
            d    = dict(zip(tri_cols, row))
            pred = d.get("predicate", "")
            obj  = d.get("object", "")
            vto  = d.get("valid_to", "")
            if pred in _KG_SESSION_NOISE:
                continue
            is_perm = not vto
            key = (pred, obj)
            if is_perm and key in seen:
                continue
            if is_perm:
                seen.add(key)
            expiry = f" (until {vto})" if vto else ""
            kg_lines.append(f"  {pred} → {obj}{expiry}")
        con.close()
        kg_context = "\n".join(kg_lines) if kg_lines else "(none yet)"
        log.info(f"KG grounding: {len(kg_lines)} stable facts loaded")
    except Exception as e:
        log.warning(f"KG grounding failed: {e} — proceeding without KG context")

    # --- Grounding block 5: all existing tags (prevents tag duplication) ---
    tag_context = "(none yet)"
    try:
        import chromadb as _chroma_tags
        from mempalace.config import MempalaceConfig as _MPC
        _chroma_t  = _chroma_tags.PersistentClient(path=palace_path)
        _coll_t    = _chroma_t.get_or_create_collection(
            name=_MPC().collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        _all_meta  = _coll_t.get(limit=2000, include=["metadatas"])["metadatas"] or []
        _tag_set   = set()
        for _m in _all_meta:
            _raw = _m.get("tags", "[]")
            try:
                _tags = json.loads(_raw) if isinstance(_raw, str) else _raw
                if isinstance(_tags, list):
                    _tag_set.update(t for t in _tags if isinstance(t, str))
            except Exception:
                pass
        if _tag_set:
            tag_context = ", ".join(sorted(_tag_set))
        log.info(f"grounding tags: {len(_tag_set)} unique tags loaded")
    except Exception as e:
        log.warning(f"tag grounding failed: {e} — proceeding without tag context")

    user_msg = (
        f"EXISTING MEMORY (what you already know about this operator):\n{memory_context}\n\n"
        f"EXISTING KG FACTS (stable — do not duplicate):\n{kg_context}\n\n"
        f"EXISTING TAGS (use these before creating new ones):\n{tag_context}\n\n"
        f"RECENT EXTRACTIONS (for pattern/tag reference):\n{recent_block}\n\n"
        f"---\nCurrent time: {now_str}\nData source: {source}\n\nNEW CAPTURE DATA:\n{context}"
    )
    log.info(
        f"user_msg assembled: {len(user_msg)} chars (~{len(user_msg)//4} tokens) | "
        f"memory_context={len(memory_context)}c kg_context={len(kg_context)}c "
        f"recent_block={len(recent_block)}c capture_data={len(context)}c"
    )

    payload = {
        "model":      ANTHROPIC_MODEL,
        "max_tokens": 8192,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": user_msg}],
    }
    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    log.info(f"calling Claude ({ANTHROPIC_MODEL}) — max_tokens=8192 | system={len(SYSTEM_PROMPT)}c")
    raw = ""
    try:
        r = requests.post(ANTHROPIC_ENDPOINT, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        log.info(f"Claude response: {len(raw)} chars")

        # strip accidental markdown fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        result = json.loads(raw)
        if not isinstance(result, dict):
            log.error(f"Claude returned non-dict ({type(result).__name__})")
            return {"memories": [], "kg_facts": []}

        log.info(
            f"Claude: {len(result.get('memories', []))} memories + "
            f"{len(result.get('kg_facts', []))} KG facts"
        )
        return result

    except json.JSONDecodeError:
        log.error(f"Claude response was not valid JSON:\n{raw[:300]}")
        return {"memories": [], "kg_facts": []}
    except requests.HTTPError as e:
        log.error(f"Claude HTTP error {e.response.status_code}: {e.response.text[:200]}")
        return {"memories": [], "kg_facts": []}
    except Exception as e:
        log.error(f"Claude request failed: {e}")
        return {"memories": [], "kg_facts": []}

# ---------------------------------------------------------------------------
# Durability → date helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _valid_to(durability: str) -> str | None:
    today = datetime.now()
    if durability == "session":
        return today.strftime("%Y-%m-%d")
    if durability == "weekly":
        return (today + timedelta(days=7)).strftime("%Y-%m-%d")
    return None  # permanent = no expiry

# ---------------------------------------------------------------------------
# MemPalace write — drawers (ChromaDB) + KG triples
# ---------------------------------------------------------------------------

def write_mempalace(result: dict):
    memories = result.get("memories", [])
    kg_facts = result.get("kg_facts", [])

    log.info(f"write_mempalace: {len(memories)} memories + {len(kg_facts)} KG facts to write")
    if not memories and not kg_facts:
        log.info("nothing to write this cycle")
        return

    try:
        import chromadb
        from mempalace.config import MempalaceConfig
        from mempalace.knowledge_graph import KnowledgeGraph

        palace_path     = os.path.expanduser(MEMPALACE_PATH)
        kg_path         = os.path.join(palace_path, "knowledge_graph.sqlite3")
        collection_name = MempalaceConfig().collection_name

        chroma     = chromadb.PersistentClient(path=palace_path)
        collection = chroma.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        kg = KnowledgeGraph(db_path=kg_path)

        # --- Pre-write memory validation ---
        _TAG_RE = re.compile(r'^[a-z][a-z0-9-]*$')
        valid_memories = []
        for _m in memories:
            _pri = str(_m.get("priority", "")).strip()
            _sum = str(_m.get("summary", "")).strip()
            _tags = _m.get("tags", [])
            if _pri not in ("HIGH", "MEDIUM"):
                log.warning(f"  memory dropped: invalid priority {_pri!r} — {_sum[:60]}")
                continue
            if not _sum:
                log.warning(f"  memory dropped: empty summary")
                continue
            if not isinstance(_tags, list):
                log.warning(f"  memory dropped: tags is not a list (got {type(_tags).__name__}) — {_sum[:60]}")
                continue
            _bad_tags = [t for t in _tags if not _TAG_RE.match(str(t))]
            if _bad_tags:
                log.warning(f"  memory tags fixed: removed malformed tags {_bad_tags} from [{_sum[:60]}]")
                _m["tags"] = [t for t in _tags if _TAG_RE.match(str(t))]
            valid_memories.append(_m)

        # Output count enforcement: max 2, 3rd allowed only if HIGH
        if len(valid_memories) > 2:
            high_count = sum(1 for m in valid_memories if m.get("priority") == "HIGH")
            if high_count >= 1:
                valid_memories = valid_memories[:3]
                log.info(f"  memory count capped at 3 (has HIGH entry)")
            else:
                valid_memories = valid_memories[:2]
                log.info(f"  memory count capped at 2 (no HIGH entry to justify 3rd)")

        memories = valid_memories
        log.info(f"  {len(memories)} memories passed validation")

        # --- Drawers (ChromaDB) ---
        drawer_count = 0
        for mem in memories:
            uid = hashlib.sha1(
                f"{mem.get('timestamp', '')}:{mem['summary'][:80]}".encode()
            ).hexdigest()[:20]
            tags_str = ", ".join(mem.get("tags", [])) or "(none)"
            log.info(f"  drawer [{uid}] [{mem['priority']}] tags=[{tags_str}] — {mem['summary'][:80]}")
            collection.upsert(
                documents=[mem["summary"]],
                metadatas=[{
                    "wing":      MEMPALACE_WING,
                    "room":      MEMPALACE_ROOM,
                    "priority":  mem["priority"],
                    "tags":      json.dumps(mem.get("tags", [])),
                    "timestamp": mem.get("timestamp", datetime.now().isoformat()),
                }],
                ids=[uid],
            )
            drawer_count += 1

        # --- KG triples ---
        _PRED_RE   = re.compile(r'^[a-z][a-z0-9_]*$')
        _VALID_DUR = {"permanent", "weekly", "session"}
        kg_written = 0
        kg_skipped = 0
        for fact in kg_facts:
            try:
                subject      = str(fact.get("subject", "")).strip()
                subject_type = str(fact.get("subject_type", "concept")).strip()
                predicate    = str(fact.get("predicate", "")).strip()
                obj          = str(fact.get("object",  "")).strip()
                obj_type     = str(fact.get("object_type", "value")).strip()
                durability   = str(fact.get("durability", "session")).strip()
                evidence     = str(fact.get("evidence", "")).strip()

                if not subject or not predicate or not obj:
                    log.warning(f"  KG fact dropped: empty field (subject={subject!r} predicate={predicate!r} obj={obj!r})")
                    kg_skipped += 1
                    continue
                if not _PRED_RE.match(predicate):
                    log.warning(f"  KG fact dropped: predicate not snake_case ({predicate!r})")
                    kg_skipped += 1
                    continue
                if durability not in _VALID_DUR:
                    log.warning(f"  KG fact dropped: invalid durability ({durability!r}) for {subject}→{predicate}")
                    kg_skipped += 1
                    continue
                if not evidence:
                    log.warning(f"  KG fact dropped: empty evidence for {subject}→{predicate}→{obj}")
                    kg_skipped += 1
                    continue

                # Register entities in KG
                kg.add_entity(subject, entity_type=subject_type)
                if obj_type != "value":
                    kg.add_entity(obj, entity_type=obj_type)

                # Write triple with validity window
                valid_from = _today()
                valid_to   = _valid_to(durability)

                try:
                    if valid_to:
                        kg.add_triple(subject, predicate, obj,
                                      valid_from=valid_from, valid_to=valid_to)
                    else:
                        kg.add_triple(subject, predicate, obj, valid_from=valid_from)
                except TypeError:
                    # KG impl doesn't support valid_to — degrade gracefully
                    kg.add_triple(subject, predicate, obj, valid_from=valid_from)

                log.info(f"  KG triple: {subject} → {predicate} → {obj} [{durability}]")
                kg_written += 1

            except Exception as e:
                log.warning(f"KG fact write failed [{fact.get('predicate','?')}]: {e}")
                kg_skipped += 1

        log.info(
            f"wrote {drawer_count} drawers → ChromaDB | "
            f"{kg_written} triples → KG | {kg_skipped} skipped"
        )

    except ImportError as e:
        log.warning(f"MemPalace/ChromaDB import failed ({e}) — writing to fallback JSONL")
        _write_fallback(result)
    except Exception as e:
        log.error(f"MemPalace write error: {e} — writing to fallback JSONL")
        _write_fallback(result)


def _write_fallback(result: dict):
    fallback_dir = os.path.expanduser(MEMPALACE_PATH)
    os.makedirs(fallback_dir, exist_ok=True)
    path = os.path.join(fallback_dir, "filter_fallback.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for mem in result.get("memories", []):
            f.write(json.dumps({"type": "memory",  **mem}, ensure_ascii=False) + "\n")
        for fact in result.get("kg_facts", []):
            f.write(json.dumps({"type": "kg_fact", **fact}, ensure_ascii=False) + "\n")
    log.info(
        f"fallback: {len(result.get('memories',[]))} memories + "
        f"{len(result.get('kg_facts',[]))} KG facts → {path}"
    )

# ---------------------------------------------------------------------------
# Synthesis pass — every SYNTHESIS_INTERVAL_DAYS, promote repeated patterns → permanent facts
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are AOL Synthesizer. You receive a dump of recent knowledge graph triples about Aero (an operator/developer).

Your job: identify durable patterns across multiple sessions and return NEW permanent KG facts that should be added.

Look for:
- Tools or apps appearing 3+ times across session facts → promote to primary_tool / primary_ide / etc.
- Projects that consistently appear as active → currently_building (permanent)
- Predicates that have been "weekly" or "session" but have repeated enough to be permanent
- Contradictions (two values for same predicate) — pick the higher-confidence / more recent one

Return ONLY a JSON array of new permanent KG facts. Same schema as kg_facts[].
All returned facts must have durability="permanent".
If nothing qualifies, return: []

Rules:
1. Output ONLY the JSON array. No preamble, no markdown fences, no text outside the brackets.
2. Only promote a fact if it appears 3+ times in the input. Do not infer or extrapolate.
3. Every fact must have a non-empty evidence field grounded in the input triples.
4. If you are uncertain whether a pattern is real, omit it. Return [] rather than guess.

Schema:
[
  {
    "subject": "...",
    "subject_type": "...",
    "predicate": "...",
    "object": "...",
    "object_type": "...",
    "durability": "permanent",
    "confidence": "high" | "medium",
    "evidence": "..."
  }
]

Valid output example:
[{"subject":"aero","subject_type":"person","predicate":"primary_ide","object":"VS Code","object_type":"tool","durability":"permanent","confidence":"high","evidence":"VS Code appears as primary_ide in 5 separate session triples."}]"""


def run_synthesis_pass(kg):
    log.info("--- synthesis pass ---")
    try:
        # Pull recent triples — try common KG method names defensively
        triples = []
        for method in ("get_triples", "query_triples", "search_triples"):
            if hasattr(kg, method):
                try:
                    result = getattr(kg, method)(subject=MEMPALACE_KG_ENTITY)
                    triples = result or []
                    break
                except Exception:
                    pass

        if not triples:
            log.info("synthesis: no triples retrievable from KG — skipping")
            return

        log.info(f"synthesis: {len(triples)} triples retrieved from KG")
        if len(triples) < 8:
            log.info(f"synthesis: only {len(triples)} triples, need 8+ — skipping")
            return

        lines = []
        for t in triples[:300]:  # cap context size
            s = t.get("subject", "?")
            p = t.get("predicate", "?")
            o = t.get("object", "?")
            d = t.get("durability") or t.get("valid_to") or "?"
            lines.append(f"{s} → {p} → {o}  [{d}]")

        context = "Recent KG triples:\n" + "\n".join(lines)
        log.info(f"synthesis: sending {len(lines)} triples to Claude | context={len(context)}c | system={len(SYNTHESIS_PROMPT)}c")

        payload = {
            "model":      ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "system":     SYNTHESIS_PROMPT,
            "messages":   [{"role": "user", "content": context}],
        }
        headers = {
            "Content-Type":      "application/json",
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
        }

        r = requests.post(ANTHROPIC_ENDPOINT, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()

        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        promoted = json.loads(raw)
        if not isinstance(promoted, list):
            log.warning("synthesis: Claude returned non-list — skipping")
            return

        _PRED_RE_S  = re.compile(r'^[a-z][a-z0-9_]*$')
        written = 0
        skipped = 0
        for fact in promoted:
            try:
                subject   = str(fact.get("subject", "")).strip()
                predicate = str(fact.get("predicate", "")).strip()
                obj       = str(fact.get("object", "")).strip()
                evidence  = str(fact.get("evidence", "")).strip()
                if not subject or not predicate or not obj:
                    log.warning(f"synthesis fact dropped: empty field")
                    skipped += 1
                    continue
                if not _PRED_RE_S.match(predicate):
                    log.warning(f"synthesis fact dropped: predicate not snake_case ({predicate!r})")
                    skipped += 1
                    continue
                if not evidence:
                    log.warning(f"synthesis fact dropped: empty evidence for {subject}→{predicate}")
                    skipped += 1
                    continue
                kg.add_entity(subject, entity_type=fact.get("subject_type", "concept"))
                if fact.get("object_type", "value") != "value":
                    kg.add_entity(obj, entity_type=fact.get("object_type", "concept"))
                kg.add_triple(subject, predicate, obj, valid_from=_today())
                written += 1
            except Exception as e:
                log.warning(f"synthesis write failed: {e}")

        log.info(f"synthesis: promoted {written} facts → permanent KG | {skipped} dropped")

    except Exception as e:
        log.error(f"synthesis pass error: {e}")

# ---------------------------------------------------------------------------
# Learn pass — every LEARN_INTERVAL_DAYS, rewrite stable MemPalace rooms
# ---------------------------------------------------------------------------

LEARN_PROMPT = """You are AOL Learn, the self-model update layer for the AOL system.

Your job: synthesize recent memory evidence and KG facts to rewrite Aero's stable MemPalace rooms. These rooms are injected as grounding into every future daily memory extraction pass. Their accuracy directly determines how well the system captures what matters.

You receive:
- RECENT MEMORIES: last 14 days of extracted memory drawers
- KG FACTS: all current knowledge graph triples
- CURRENT ROOMS: what each room currently contains

Return ONLY valid JSON with this exact structure — nothing else, no markdown fences:
{
  "working-style": "<updated content>",
  "projects-active": "<updated content>",
  "tech-environment": "<updated content>",
  "workspace-map": "<updated content>"
}

---

ROOM CONTENT RULES:
- Plain text, use ## headers and bullet points. Dense and specific — these are machine-read grounding, not prose.
- Preserve correct existing content. Update stale content. Add new content supported by evidence.
- Remove anything that contradicts the evidence window or hasn't appeared in 14+ days.
- If evidence for a room is thin, keep the existing content and note "(unconfirmed this window)".

---

WORKING-STYLE
What to capture: typical work schedule (times observed), focus patterns, context-switching habits, session length, how Aero approaches problems, communication patterns.
What not to capture: one-off events, specific task content (those go in other rooms).

PROJECTS-ACTIVE
What to capture: every project Aero worked on in the evidence window. For each project:
  - Name, domain, current status (active/blocked/paused)
  - Tech stack (language, framework, database, hosting)
  - What's in progress this week, what's blocked
  - Collaborators if any
Mark projects not seen in 14+ days as "(last seen YYYY-MM-DD, status unclear)".

TECH-ENVIRONMENT
What to capture: confirmed tools only — IDEs, terminal, browser, shell, OS, package managers, version control, AI tools, hardware, key services used.
Prefer specific names over categories. Evidence required for every entry.

WORKSPACE-MAP
What to capture: local running services with ports, how they connect to each other, deployment targets, key API endpoints.
Format: service → port → dependencies.
Include AOL system components and their relationships.

---

Rules:
1. Output ONLY the JSON object. No preamble, no markdown fences, no text outside the braces.
2. All four rooms must be present in the output, even if unchanged.
3. If a room would be entirely empty due to lack of evidence, keep the current content and append "(last updated: <today's date>, no new evidence this window)".
4. Do not fabricate facts not present in the evidence. If you are uncertain, keep the existing content unchanged.
5. Rewrite only with evidence present in the input. Do not fill gaps with assumptions."""


def run_learn_pass():
    log.info("--- learn pass ---")
    try:
        import chromadb
        from mempalace.config import MempalaceConfig

        palace_path     = os.path.expanduser(MEMPALACE_PATH)
        collection_name = MempalaceConfig().collection_name
        chroma          = chromadb.PersistentClient(path=palace_path)
        collection      = chroma.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        # Fetch all entries once — ids always returned by col.get()
        res      = collection.get(limit=1000, include=["documents", "metadatas"])
        all_ids  = res.get("ids", [])
        all_docs = res.get("documents", [])
        all_metas = res.get("metadatas", [])

        # --- 1. Recent drawers (last LEARN_LOOKBACK_DAYS) ---
        cutoff = (datetime.now() - timedelta(days=LEARN_LOOKBACK_DAYS)).isoformat()
        drawer_entries = []   # (timestamp, priority, tags_list, doc)
        room_id_map    = {r: [] for r in LEARN_ROOMS}   # room_name → [ids to replace]

        for eid, doc, meta in zip(all_ids, all_docs, all_metas):
            room = meta.get("room", "")
            ts   = meta.get("timestamp", "")
            if room == MEMPALACE_ROOM and ts >= cutoff:
                pri      = meta.get("priority", "?")
                tags_raw = meta.get("tags", "[]")
                try:
                    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
                except Exception:
                    tags = []
                drawer_entries.append((ts, pri, tags, doc))
            if room in room_id_map:
                room_id_map[room].append(eid)

        drawer_entries.sort(key=lambda x: x[0], reverse=True)
        log.info(f"learn: {len(drawer_entries)} drawers from last {LEARN_LOOKBACK_DAYS} days")

        if len(drawer_entries) < 3:
            log.info(f"learn: only {len(drawer_entries)} drawers — need 3+ for meaningful update, skipping")
            return

        # --- 2. KG facts (all, no noise filter — learn needs full picture) ---
        kg_lines = []
        try:
            import sqlite3 as _sqlite3
            kg_path  = os.path.join(palace_path, "knowledge_graph.sqlite3")
            con      = _sqlite3.connect(kg_path)
            cur      = con.cursor()
            tri_cols = [r[1] for r in cur.execute("PRAGMA table_info(triples)").fetchall()]
            rows     = cur.execute("SELECT * FROM triples ORDER BY subject, predicate, object").fetchall()
            for row in rows:
                d    = dict(zip(tri_cols, row))
                subj = d.get("subject", "?")
                pred = d.get("predicate", "?")
                obj  = d.get("object",   "?")
                vto  = d.get("valid_to", "")
                expiry = f" (until {vto})" if vto else ""
                kg_lines.append(f"  {subj} → {pred} → {obj}{expiry}")
            con.close()
            log.info(f"learn: {len(kg_lines)} KG facts loaded")
        except Exception as e:
            log.warning(f"learn: KG read failed: {e}")

        # --- 3. Current room content ---
        current_rooms: dict[str, str] = {}
        for doc, meta in zip(all_docs, all_metas):
            room = meta.get("room", "")
            if room in LEARN_ROOMS and room not in current_rooms:
                current_rooms[room] = doc

        # --- 4. Build prompt and call Claude ---
        drawer_lines = []
        for ts, pri, tags, doc in drawer_entries[:120]:  # cap context size
            tag_str = ", ".join(tags) if tags else "(none)"
            drawer_lines.append(f"[{ts}] [{pri}] tags=[{tag_str}]\n  {doc[:300]}")

        rooms_block = ""
        for room in LEARN_ROOMS:
            content = current_rooms.get(room, "(empty)")
            rooms_block += f"\n\n[{room}]\n{content}"

        user_msg = (
            f"RECENT MEMORIES (last {LEARN_LOOKBACK_DAYS} days, {len(drawer_entries)} entries):\n"
            + "\n\n".join(drawer_lines)
            + f"\n\nKG FACTS ({len(kg_lines)} total):\n"
            + ("\n".join(kg_lines) if kg_lines else "(none)")
            + f"\n\nCURRENT ROOMS:{rooms_block}"
            + f"\n\nToday: {_today()}"
        )
        log.info(
            f"learn: calling Claude | drawers={len(drawer_lines)} kg={len(kg_lines)} | "
            f"msg={len(user_msg)}c (~{len(user_msg)//4} tokens)"
        )

        payload = {
            "model":      ANTHROPIC_MODEL,
            "max_tokens": 8192,
            "system":     LEARN_PROMPT,
            "messages":   [{"role": "user", "content": user_msg}],
        }
        headers = {
            "Content-Type":      "application/json",
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
        }

        r = requests.post(ANTHROPIC_ENDPOINT, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        log.info(f"learn: Claude response {len(raw)} chars")

        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        updated_rooms = json.loads(raw)
        if not isinstance(updated_rooms, dict):
            log.warning("learn: Claude returned non-dict — skipping write")
            return

        # --- 5. Replace old room entries, upsert updated content ---
        written = 0
        for room_name in LEARN_ROOMS:
            new_content = updated_rooms.get(room_name, "").strip()
            if not new_content:
                log.warning(f"learn: no content returned for [{room_name}] — skipping")
                continue

            old_ids = room_id_map.get(room_name, [])
            if old_ids:
                try:
                    collection.delete(ids=old_ids)
                    log.info(f"learn: deleted {len(old_ids)} old entries for [{room_name}]")
                except Exception as e:
                    log.warning(f"learn: delete failed for [{room_name}]: {e}")

            room_doc_id = f"room:{MEMPALACE_WING}:{room_name}"
            collection.upsert(
                documents=[new_content],
                metadatas=[{
                    "wing":       MEMPALACE_WING,
                    "room":       room_name,
                    "updated_by": "learn",
                    "updated_at": datetime.now().isoformat(),
                }],
                ids=[room_doc_id],
            )
            log.info(f"learn: updated [{room_name}] → {len(new_content)} chars")
            written += 1

        log.info(f"learn: {written}/{len(LEARN_ROOMS)} rooms updated")

    except ImportError as e:
        log.warning(f"learn: ChromaDB/MemPalace import failed ({e}) — skipping")
    except Exception as e:
        log.error(f"learn pass error: {e}")


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_env():
    log.info(f"check_env: python = {sys.executable}")
    for key in ("SCREENPIPE_API_KEY", "ANTHROPIC_API_KEY"):
        val = os.environ.get(key, "")
        if val:
            log.info(f"  {key}: present ({len(val)} chars)")
        else:
            log.warning(f"  {key}: MISSING")
    for key in ("FILTER_AGENT_PYTHON",):
        val = os.environ.get(key, "")
        log.info(f"  {key}: {'set → ' + val if val else '(not set — using sys.executable)'}")

    missing = [k for k in ("SCREENPIPE_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        log.error(f"aborting: missing required env vars: {', '.join(missing)}")
        raise SystemExit(1)
    try:
        import mempalace
        log.info(f"mempalace: OK (version={getattr(mempalace, '__version__', 'unknown')})")
    except ImportError:
        log.warning(
            f"mempalace not found under {sys.executable} — "
            "set FILTER_AGENT_PYTHON in .env to the interpreter that has it installed"
        )


def _sp_health_check() -> bool:
    """Returns True if Screenpipe /health responds OK."""
    try:
        r = requests.get(f"{SCREENPIPE_API}/health", headers=sp_headers(), timeout=5)
        r.raise_for_status()
        return True
    except Exception:
        return False


def _sp_data_ready() -> bool:
    """
    Returns True if Screenpipe's data API is actually ready to serve queries.
    /health comes up fast but the search/OCR pipeline takes longer to initialize.
    We probe /search with a minimal query — a 200 (even empty results) means ready.
    """
    try:
        now = datetime.utcnow()
        start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.get(
            f"{SCREENPIPE_API}/search",
            params={"content_type": "ocr", "start_time": start, "end_time": end, "limit": 1},
            headers=sp_headers(),
            timeout=8,
        )
        r.raise_for_status()
        return True
    except Exception:
        return False


def ensure_screenpipe_running() -> tuple[bool, bool]:
    """
    Ensure Screenpipe is up AND its data API is ready before FA runs.
    Returns (available, fa_started_it).
    - If SP already up + data ready: (True, False)
    - If SP down → starts it via C2, waits SP_BOOT_WAIT_SECONDS: (True, True) or (False, False)

    Two-phase readiness check:
      Phase 1: poll /health until it responds (process is up)
      Phase 2: poll /search until it responds (data pipeline is ready)
    Screenpipe's HTTP server comes up quickly but its SQLite/OCR pipeline
    takes 30-60s more — querying too early causes ConnectionReset then crash.
    """
    if _sp_health_check() and _sp_data_ready():
        log.info("screenpipe health OK (process + data API ready)")
        return True, False

    if _sp_health_check():
        # Process is up but data API not ready yet — wait for it
        log.info("screenpipe process up — waiting for data API to become ready")
    else:
        log.warning("screenpipe not running — attempting to start via C2")
        try:
            r = requests.post(f"{C2_API}/start", timeout=5)
            r.raise_for_status()
            log.info(f"C2 /start called — waiting up to {SP_BOOT_WAIT_SECONDS}s for screenpipe to boot")
        except Exception as e:
            log.error(f"C2 /start failed: {e} — cannot start screenpipe, aborting FA cycle")
            return False, False

    deadline = time.time() + SP_BOOT_WAIT_SECONDS
    phase = "health"
    while time.time() < deadline:
        time.sleep(5)
        remaining = max(0, deadline - time.time())
        if phase == "health":
            if _sp_health_check():
                log.info(f"screenpipe process up — waiting for data API ({remaining:.0f}s remaining)")
                phase = "data"
            else:
                log.info(f"screenpipe process not yet up — {remaining:.0f}s remaining")
        if phase == "data":
            if _sp_data_ready():
                log.info("screenpipe data API ready — proceeding with FA cycle")
                return True, True
            else:
                log.info(f"screenpipe data API not yet ready — {remaining:.0f}s remaining")

    log.error(f"screenpipe did not become fully ready within {SP_BOOT_WAIT_SECONDS}s — aborting FA cycle")
    return False, False

# ---------------------------------------------------------------------------
# Plugin loader
# ---------------------------------------------------------------------------

def _load_plugins():
    try:
        from plugins import load_plugins
        instances = load_plugins()
        log.info(f"loaded {len(instances)} plugin(s): {[p.name for p in instances]}")
        for p in instances:
            try:
                p.on_start()
            except Exception as e:
                log.warning(f"plugin {p.name} on_start error: {e}")
        return instances
    except Exception as e:
        log.warning(f"plugin load failed: {e} — running without plugins")
        return []

# ---------------------------------------------------------------------------
# Main — single cycle, then exit
# ---------------------------------------------------------------------------

def run():
    check_env()
    log.info(f"AOL Filter Agent | daily | model: {ANTHROPIC_MODEL}")

    sp_available, fa_started_sp = ensure_screenpipe_running()
    if not sp_available:
        log.error("aborting — screenpipe unavailable and could not be started")
        return
    if fa_started_sp:
        log.info("FA started screenpipe — will stop it after cycle completes")

    plugins = _load_plugins()

    try:
        log.info("--- daily cycle start ---")
        lookback        = POLL_INTERVAL_MIN + LOOKBACK_BUFFER_MIN
        log.info(f"lookback window: {lookback}m ({lookback/60:.1f}h) [{POLL_INTERVAL_MIN}m poll + {LOOKBACK_BUFFER_MIN}m buffer]")
        context, source = fetch_context(lookback)
        log.info(f"capture context: {len(context)} chars | source: {source}")

        if context.strip() not in ("(empty)", "(no usable data)"):
            result = run_claude(context, source)
            mem_count = len(result.get("memories", []))
            kg_count  = len(result.get("kg_facts", []))
            log.info(f"LLM result: {mem_count} memories | {kg_count} KG facts")
            write_mempalace(result)

            for p in plugins:
                try:
                    log.info(f"running plugin: {p.name}")
                    p.process(result, context, source)
                except Exception as e:
                    log.warning(f"plugin {p.name} process error: {e}")
        else:
            log.info("no content this cycle — skipping LLM pass")

        # Synthesis pass — date-based, every SYNTHESIS_INTERVAL_DAYS days
        if _synthesis_due():
            log.info(f"synthesis is due — running promotion pass (interval: every {SYNTHESIS_INTERVAL_DAYS} days)")
            try:
                from mempalace.knowledge_graph import KnowledgeGraph
                palace_path = os.path.expanduser(MEMPALACE_PATH)
                kg_path     = os.path.join(palace_path, "knowledge_graph.sqlite3")
                log.info(f"synthesis: opening KG at {kg_path}")
                kg          = KnowledgeGraph(db_path=kg_path)
                run_synthesis_pass(kg)
                meta = _load_metadata()
                meta["last_synthesis_date"] = str(date.today())
                _save_metadata(meta)
                log.info(f"synthesis: last_synthesis_date updated to {date.today()}")
            except Exception as e:
                log.warning(f"could not run synthesis: {e}")
        else:
            log.info("synthesis not due this cycle — skipping")

        # Learn pass — every LEARN_INTERVAL_DAYS, rewrite stable MemPalace rooms
        if _learn_due():
            log.info(f"learn is due — running room rewrite pass (interval: every {LEARN_INTERVAL_DAYS} days)")
            run_learn_pass()
            meta = _load_metadata()
            meta["last_learn_date"] = str(date.today())
            _save_metadata(meta)
            log.info(f"learn: last_learn_date updated to {date.today()}")
        else:
            log.info("learn not due this cycle — skipping")

        # Record that FA ran today so C2 restarts don't double-fire
        meta = _load_metadata()
        meta["last_run_date"] = str(date.today())
        _save_metadata(meta)
        log.info(f"last_run_date updated to {date.today()}")

        log.info("daily cycle complete — exiting")

    except Exception as e:
        log.error(f"unhandled error in cycle: {e}")

    finally:
        for p in plugins:
            try:
                p.on_stop()
            except Exception as e:
                log.warning(f"plugin {p.name} on_stop error: {e}")

        if fa_started_sp:
            log.info("FA started screenpipe — stopping it now via C2")
            try:
                requests.post(f"{C2_API}/screenpipe/stop", timeout=5)
                log.info("screenpipe stopped via C2 /screenpipe/stop")
            except Exception as e:
                log.warning(f"screenpipe stop via C2 failed: {e}")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"fatal: {e}")
        sys.exit(1)
