"""
AOL Filter Agent
Polls Screenpipe every POLL_INTERVAL_MIN, runs a Claude LLM pass to extract
operationally significant memories AND rich knowledge graph facts, writes
HIGH/MEDIUM entries to MemPalace.

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
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
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
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENDPOINT  = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL     = "claude-opus-4-7"
ANTHROPIC_VERSION   = "2023-06-01"

POLL_INTERVAL_MIN   = 720
LOOKBACK_BUFFER_MIN = 5
SEARCH_LIMIT        = 150

# Every N cycles, run a synthesis pass that promotes repeated patterns → permanent KG facts
SYNTHESIS_EVERY_N   = 5
_cycle_count        = 0

MEMPALACE_PATH      = "~/aol-memory"
MEMPALACE_WING      = "aero-ops"
MEMPALACE_ROOM      = "daily-activity"
MEMPALACE_KG_ENTITY = "aero"

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
# Shutdown
# ---------------------------------------------------------------------------

_stop = threading.Event()


def _handle_shutdown(signum=None, frame=None):
    log.info(f"shutdown signal received ({signum}) — stopping after current cycle")
    _stop.set()


signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

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
        has_frames = data.get("total_frames", 0) > 0
        has_audio  = data.get("audio_summary", {}).get("segment_count", 0) > 0
        return data if (has_frames or has_audio) else None
    except Exception as e:
        log.warning(f"activity-summary fetch failed: {e}")
        return None


def format_activity_summary(summary: dict) -> str:
    lines = []
    apps = [a for a in summary.get("apps", []) if a.get("minutes", 0) >= 1]
    if apps:
        usage = ", ".join(f"{a['name']} ({a['minutes']:.0f}m)" for a in apps)
        lines.append(f"APP USAGE: {usage}")
    for t in summary.get("recent_texts", []):
        text = t.get("text", "").strip().replace("\n", " ")
        if text:
            lines.append(f"[SCREEN|{t.get('app_name','')}|{t.get('timestamp','')}] {text[:400]}")
    audio     = summary.get("audio_summary", {})
    seg_count = audio.get("segment_count", 0)
    if seg_count > 0:
        speakers = [s.get("name", "unknown") for s in audio.get("speakers", [])]
        spk_str  = ", ".join(speakers) if speakers else "unidentified"
        lines.append(f"AUDIO: {seg_count} segments | speakers: {spk_str}")
    return "\n".join(lines) if lines else "(empty)"

# ---------------------------------------------------------------------------
# Screenpipe — fallback: /search
# ---------------------------------------------------------------------------

def fetch_search(content_type: str, minutes_back: int) -> list[dict]:
    start, end = time_window(minutes_back)
    try:
        r = requests.get(
            f"{SCREENPIPE_API}/search",
            headers=sp_headers(),
            params={"content_type": content_type, "start_time": start,
                    "end_time": end, "limit": SEARCH_LIMIT},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.warning(f"/search fallback failed ({content_type}): {e}")
        return []


def format_raw_events(events: list[dict]) -> str:
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
    summary = fetch_activity_summary(minutes_back)
    if summary:
        log.info(
            f"activity-summary: {summary.get('total_frames', 0)} frames, "
            f"{summary.get('audio_summary', {}).get('segment_count', 0)} audio segments"
        )
        return format_activity_summary(summary), "activity-summary"
    log.info("activity-summary empty — falling back to raw /search")
    ocr   = fetch_search("ocr",   minutes_back)
    audio = fetch_search("audio", minutes_back)
    log.info(f"raw search: {len(ocr)} OCR + {len(audio)} audio")
    return format_raw_events(ocr + audio), "raw-search"

# ---------------------------------------------------------------------------
# Claude LLM pass — extracts memories AND rich KG facts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are AOL Filter, the memory intelligence layer for Aero — a systems-oriented developer, AFROTC cadet, and operator running a personal AI operating system (AOL).

Your job runs every 60 minutes. You receive raw screen + audio capture data from Screenpipe and return two things: structured memory entries for ChromaDB, and knowledge graph triples for MemPalace. You are the only thing standing between raw noise and a queryable, durable model of Aero's life and work.

---

## WHO AERO IS

Name: Aero
KG entity key: "aero"
Domains: Developer, AFROTC, Faith, Creative, Finance

Active projects:
- AOL — his personal AI operating system (this system you're part of)
- Evergreen — iOS app
- USAF Fitness Tracker — fitness tracking tool
- Whiteframe — creative/design project
- ACE ISLAND — game or simulation project

Faith frameworks: The Consecration, Christianized Shinobi Framework
Tools observed: Antigravity IDE, claude.exe, screenpipe, Vercel, Supabase

---

## INPUT FORMAT

You will receive:
- Current timestamp
- Data source: "activity-summary" or "raw-search"
- Raw context: app usage durations, OCR screen text, audio transcriptions, UI events

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
- HIGH: confirmed active work session (10+ min on tracked app), decisions made, deadlines set, commitments, milestones, blockers identified
- MEDIUM: research with clear intent, task planning, meaningful comms, learning tied to a domain

DISCARD ENTIRELY:
- Gaming, entertainment, passive video/streaming
- Aimless browsing, news, social media without clear intent
- Idle time, lock screens, screensavers
- Redundant content already captured this session

Memory schema:
{
  "priority": "HIGH" | "MEDIUM",
  "domain": "Developer" | "AFROTC" | "Faith" | "Creative" | "Finance",
  "project": "<project name or null>",
  "summary": "<1-2 sentences: what happened, what was decided, what matters>",
  "tags": ["<tag1>", "<tag2>"],
  "timestamp": "<ISO 8601>"
}

---

## PART 2 — KNOWLEDGE GRAPH FACTS

This is the most important part of your job. Extract every structural fact the session data supports. Be aggressive. 10-50 facts per session is normal. The goal is a KG that a future session can query to instantly know: what tools Aero uses, what projects are active, what's his stack, who he works with, what's his workflow, what's blocked.

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
aero → used_tool_this_session → <app>        [one per app, durability: session]
aero → session_time_in → "<app>:<N>min"      [encode as string, durability: session]
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

1. Return only valid JSON. No text outside the JSON object.
2. If nothing is worth keeping: {"memories": [], "kg_facts": []}
3. Never invent data not supported by the session input.
4. Prefer specific predicates over generic ones.
5. One kg_fact per subject-predicate-object combination — don't duplicate.
6. Evidence field is required on every kg_fact. One sentence, grounded in the input.
7. Write every fact the evidence supports. Omission is a failure mode."""


def run_claude(context: str, source: str) -> dict:
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    user_msg = f"Current time: {now_str}\nData source: {source}\n\n{context}"

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

    raw = ""
    try:
        r = requests.post(ANTHROPIC_ENDPOINT, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()

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

        # --- Drawers (ChromaDB) ---
        drawer_count = 0
        for mem in memories:
            uid = hashlib.sha1(
                f"{mem.get('timestamp', '')}:{mem['summary'][:80]}".encode()
            ).hexdigest()[:20]
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
            drawer_count += 1

        # --- KG triples ---
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

                if not subject or not predicate or not obj:
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
# Synthesis pass — every N cycles, promote repeated patterns → permanent facts
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are AOL Synthesizer. You receive a dump of recent knowledge graph triples about Aero (an operator/developer).

Your job: identify durable patterns across multiple sessions and return NEW permanent KG facts that should be added.

Look for:
- Tools or apps appearing 3+ times across session facts → promote to primary_tool / primary_ide / etc.
- Projects that consistently appear as active → currently_building (permanent)
- Predicates that have been "weekly" or "session" but have repeated enough to be permanent
- Contradictions (two values for same predicate) — pick the higher-confidence / more recent one
- Obvious gaps: if you know IDE + language but not stack, infer it if confidence is high

Return ONLY a JSON array of new permanent KG facts. Same schema as kg_facts[].
All returned facts must have durability="permanent".
If nothing to promote, return: []

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
]"""


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

        written = 0
        for fact in promoted:
            try:
                subject   = str(fact.get("subject", "")).strip()
                predicate = str(fact.get("predicate", "")).strip()
                obj       = str(fact.get("object", "")).strip()
                if not subject or not predicate or not obj:
                    continue
                kg.add_entity(subject, entity_type=fact.get("subject_type", "concept"))
                if fact.get("object_type", "value") != "value":
                    kg.add_entity(obj, entity_type=fact.get("object_type", "concept"))
                kg.add_triple(subject, predicate, obj, valid_from=_today())
                written += 1
            except Exception as e:
                log.warning(f"synthesis write failed: {e}")

        log.info(f"synthesis: promoted {written} facts → permanent KG")

    except Exception as e:
        log.error(f"synthesis pass error: {e}")

# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_env():
    missing = [k for k in ("SCREENPIPE_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        log.error(f"missing env vars: {', '.join(missing)}")
        raise SystemExit(1)
    try:
        import mempalace
        log.info(f"mempalace OK ({sys.executable})")
    except ImportError:
        log.warning(
            f"mempalace not found under {sys.executable} — "
            "set FILTER_AGENT_PYTHON in .env to the interpreter that has it installed"
        )


def check_screenpipe():
    try:
        r = requests.get(f"{SCREENPIPE_API}/health", headers=sp_headers(), timeout=5)
        r.raise_for_status()
        log.info("screenpipe health OK")
    except Exception as e:
        log.warning(f"screenpipe health check failed: {e} — will retry on next cycle")

# ---------------------------------------------------------------------------
# Main loop
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


def run():
    global _cycle_count
    check_env()
    log.info(f"AOL Filter Agent | poll: {POLL_INTERVAL_MIN}m | model: {ANTHROPIC_MODEL}")
    check_screenpipe()

    plugins = _load_plugins()

    while not _stop.is_set():
        try:
            log.info("--- cycle start ---")
            lookback        = POLL_INTERVAL_MIN + LOOKBACK_BUFFER_MIN
            context, source = fetch_context(lookback)

            if context.strip() not in ("(empty)", "(no usable data)"):
                result = run_claude(context, source)
                write_mempalace(result)

                for p in plugins:
                    try:
                        p.process(result, context, source)
                    except Exception as e:
                        log.warning(f"plugin {p.name} process error: {e}")
            else:
                log.info("no content this cycle — skipping LLM pass")

            _cycle_count += 1

            # Periodic synthesis pass
            if _cycle_count % SYNTHESIS_EVERY_N == 0:
                try:
                    from mempalace.knowledge_graph import KnowledgeGraph
                    palace_path = os.path.expanduser(MEMPALACE_PATH)
                    kg_path     = os.path.join(palace_path, "knowledge_graph.sqlite3")
                    kg          = KnowledgeGraph(db_path=kg_path)
                    run_synthesis_pass(kg)
                except Exception as e:
                    log.warning(f"could not init KG for synthesis: {e}")

            log.info(f"cycle complete — sleeping {POLL_INTERVAL_MIN}m")

        except Exception as e:
            log.error(f"unhandled error in cycle: {e} — continuing")

        _stop.wait(timeout=POLL_INTERVAL_MIN * 60)

    for p in plugins:
        try:
            p.on_stop()
        except Exception as e:
            log.warning(f"plugin {p.name} on_stop error: {e}")

    log.info("filter agent stopped cleanly")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"fatal: {e}")
        sys.exit(1)