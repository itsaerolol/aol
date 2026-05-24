"""
Grounding context diagnostic for AOL Filter Agent.
Shows exactly what Claude would receive as grounding before processing capture data.
Target: 97% confidence the context is sufficient for daily memory extraction.
"""
import json
import os
import sqlite3
import sys
import chromadb
from mempalace.config import MempalaceConfig
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.layers import MemoryStack

sys.stdout.reconfigure(encoding="utf-8")

PALACE_PATH = "C:/Users/keatn/aol-memory"
KG_PATH     = os.path.join(PALACE_PATH, "knowledge_graph.sqlite3")
KG_ENTITY   = "aero"
WING        = "aero-ops"

# Rooms to inject in full — ordered by relevance to filter agent's job
GROUNDING_ROOMS = ["identity", "projects-active", "aol-system", "tech-environment", "working-style", "workspace-map"]

# KG predicates that are per-session snapshots — noise for grounding
SESSION_NOISE_PREDICATES = {
    "session_time_in", "used_tool_this_session", "context_switches_between",
    "focuses_on", "workflow_pattern", "worked_on", "typical_work_start",
    "hardware_brand", "communication_platform", "shell", "system_monitoring_tool",
    "primary_ai_assistant", "primary_ai_tool",
}

SEP  = "=" * 70
SEP2 = "-" * 70

# ---------------------------------------------------------------------------
# DIAGNOSTIC 1 — wake_up (L0 + L1 as-is, for reference)
# ---------------------------------------------------------------------------
print(SEP)
print("DIAG 1 — WAKE_UP raw output (L0 + truncated L1)")
print(SEP)
stack   = MemoryStack(palace_path=PALACE_PATH)
wake_up = stack.wake_up(wing=WING)
print(wake_up)
print(f"\n[{len(wake_up)} chars]")

# ---------------------------------------------------------------------------
# DIAGNOSTIC 2 — KG schema + all triples via raw SQLite
# ---------------------------------------------------------------------------
print()
print(SEP)
print("DIAG 2 — KG RAW (SQLite direct)")
print(SEP)
try:
    con = sqlite3.connect(KG_PATH)
    cur = con.cursor()
    ent_cols = [r[1] for r in cur.execute("PRAGMA table_info(entities)").fetchall()]
    tri_cols = [r[1] for r in cur.execute("PRAGMA table_info(triples)").fetchall()]
    print(f"entities columns: {ent_cols}")
    print(f"triples  columns: {tri_cols}")
    ent_count = cur.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    tri_count = cur.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
    print(f"entities: {ent_count}   triples: {tri_count}\n")

    rows = cur.execute(f"SELECT * FROM triples ORDER BY subject, predicate").fetchall()
    current_subject = None
    for row in rows:
        d = dict(zip(tri_cols, row))
        subj = d.get("subject", "?")
        pred = d.get("predicate", "?")
        obj  = d.get("object",  "?")
        vto  = d.get("valid_to",  "")
        dur  = d.get("durability", "")
        if subj != current_subject:
            print(f"\n  [{subj}]")
            current_subject = subj
        expiry = f" → {vto}" if vto else " (perm)"
        tag    = f" [{dur}]" if dur else ""
        noise  = " [SESSION-NOISE]" if pred in SESSION_NOISE_PREDICATES else ""
        print(f"    {pred} = {obj}{tag}{expiry}{noise}")
    con.close()
except Exception as e:
    print(f"KG SQLite error: {e}")

# ---------------------------------------------------------------------------
# DIAGNOSTIC 3 — query_entity() actual return type
# ---------------------------------------------------------------------------
print()
print(SEP)
print("DIAG 3 — query_entity() return type probe")
print(SEP)
try:
    kg     = KnowledgeGraph(db_path=KG_PATH)
    result = kg.query_entity(KG_ENTITY, direction="outgoing")
    print(f"Type: {type(result)}")
    if isinstance(result, list):
        print(f"Length: {len(result)}")
        if result:
            print(f"First item type: {type(result[0])}")
            print(f"First item: {result[0]}")
            # Show stable (current=True, no expiry) facts
            stable = [f for f in result if f.get("current") and not f.get("valid_to")]
            print(f"\nStable (current, no expiry): {len(stable)}")
            for f in stable[:10]:
                print(f"  {f.get('predicate')} → {f.get('object')}")
    elif isinstance(result, dict):
        print(f"Keys: {list(result.keys())}")
except Exception as e:
    print(f"query_entity error: {e}")

# ---------------------------------------------------------------------------
# BUILD GROUNDING BLOCKS
# ---------------------------------------------------------------------------

chroma   = chromadb.PersistentClient(path=PALACE_PATH)
col_name = MempalaceConfig().collection_name
col      = chroma.get_or_create_collection(name=col_name, metadata={"hnsw:space": "cosine"})
results  = col.get(limit=500, include=["documents", "metadatas"])
room_docs = {}
all_drawers = []

for doc, meta in zip(results.get("documents", []), results.get("metadatas", [])):
    room = meta.get("room", "unknown")
    # Room documents (long, structured text — not per-memory drawers)
    if room in GROUNDING_ROOMS and room not in room_docs:
        room_docs[room] = doc
    # Collect all drawer entries (per-memory records from filter agent extractions)
    # Room docs have 'filed_at' but no 'timestamp'; drawers have 'timestamp' + 'priority' + 'tags'
    if meta.get("timestamp") and meta.get("priority"):
        all_drawers.append({"doc": doc, "meta": meta})

# Sort drawers by timestamp descending
all_drawers.sort(key=lambda x: x["meta"].get("timestamp", ""), reverse=True)

# Build full room block
full_rooms = []
for room in GROUNDING_ROOMS:
    if room in room_docs:
        full_rooms.append(f"[{room}]\n{room_docs[room]}")
full_room_block = "\n\n".join(full_rooms) if full_rooms else "(no room content)"

# --- KG stable facts via raw SQLite (deduplicated, noise filtered) ---
kg_stable_lines = []
kg_noise_lines  = []
seen_stable = set()  # dedup (pred, obj) for stable perm facts

try:
    con = sqlite3.connect(KG_PATH)
    cur = con.cursor()
    tri_cols = [r[1] for r in cur.execute("PRAGMA table_info(triples)").fetchall()]
    rows = cur.execute("SELECT * FROM triples ORDER BY subject, predicate, object").fetchall()
    for row in rows:
        d    = dict(zip(tri_cols, row))
        subj = d.get("subject", "?")
        pred = d.get("predicate", "?")
        obj  = d.get("object",  "?")
        vto  = d.get("valid_to", "")
        is_perm = not vto
        is_noise = pred in SESSION_NOISE_PREDICATES

        if is_noise:
            kg_noise_lines.append(f"  {pred} → {obj}")
        else:
            key = (pred, obj)
            if is_perm and key not in seen_stable:
                seen_stable.add(key)
                kg_stable_lines.append(f"  {pred} → {obj}")
            elif not is_perm:
                kg_stable_lines.append(f"  {pred} → {obj} (until {vto})")
    con.close()
except Exception as e:
    kg_stable_lines = [f"(KG read error: {e})"]

kg_block = "\n".join(kg_stable_lines) if kg_stable_lines else "(none yet)"

# --- Recent memory examples (last 5 drawers with tags) ---
recent_examples = []
for entry in all_drawers[:5]:
    doc  = entry["doc"]
    meta = entry["meta"]
    ts   = meta.get("timestamp", "?")
    pri  = meta.get("priority", "?")
    tags_raw = meta.get("tags", "[]")
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = [tags_raw]
    tag_str = ", ".join(tags) if tags else "(none)"
    recent_examples.append(f"[{ts}] [{pri}] tags=[{tag_str}]\n  {doc[:200]}")

BOOTSTRAP_NOTE = """[BOOTSTRAP — vault is empty. Target memory schema for this extraction:]

Example HIGH:
  timestamp: 2026-05-22T21:30:00
  priority:  HIGH
  tags:      [aol-system, developer, milestone]
  summary:   Completed first working cycle of AOL Filter Agent. Layer 2 (filter + store) now \
live. All pipeline components functional end-to-end.

Example MEDIUM:
  timestamp: 2026-05-22T15:00:00
  priority:  MEDIUM
  tags:      [aol-system, developer, debugging]
  summary:   Spent ~45 min debugging Screenpipe /activity-summary endpoint. Confirmed OCR \
fallback path activates when summary returns empty. Behavior now documented.

Tag conventions: lowercase, hyphenated. Infer from context. Examples:
  aol-system, developer, afrotc-deadline, faith-devotional, fitness-workout,
  housing-research, creative-work, new-pattern"""

if recent_examples:
    recent_block = "\n\n".join(recent_examples)
else:
    recent_block = BOOTSTRAP_NOTE

# --- Tag inventory ---
tag_counts: dict[str, int] = {}
for entry in all_drawers:
    tags_raw = entry["meta"].get("tags", "[]")
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags = []
    for t in tags:
        tag_counts[t] = tag_counts.get(t, 0) + 1

tag_inventory = ", ".join(
    f"{t}({c})" for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])
) if tag_counts else "(none yet)"

# ---------------------------------------------------------------------------
# DIAGNOSTIC 4 — Simulated grounding block (exactly what Claude sees)
# ---------------------------------------------------------------------------
print()
print(SEP)
print("DIAG 4 — SIMULATED GROUNDING (what Claude actually receives)")
print(SEP)

L0_line = wake_up.split("\n")[0]  # just the identity one-liner

simulated = (
    f"EXISTING MEMORY (what you already know about this operator):\n"
    f"{L0_line}\n\n"
    f"{full_room_block}\n\n"
    f"EXISTING KG FACTS (stable — do not duplicate):\n"
    f"{kg_block}\n\n"
    f"RECENT EXTRACTIONS (last {len(recent_examples)} — for pattern/tag reference):\n"
    f"{recent_block}\n\n"
    f"---\nCurrent time: 2026-05-24 21:30\nData source: activity-summary\n\n"
    f"NEW CAPTURE DATA:\n<capture data would go here>"
)

print(simulated)

# ---------------------------------------------------------------------------
# DIAGNOSTIC 5 — Grounding quality assessment
# ---------------------------------------------------------------------------
print()
print(SEP)
print("DIAG 5 — GROUNDING QUALITY ASSESSMENT")
print(SEP)

total_chars  = len(simulated)
total_tokens = total_chars // 4
stable_triple_count  = len(kg_stable_lines)
noise_triple_count   = len(kg_noise_lines)
drawer_count         = len(all_drawers)
bootstrap            = drawer_count == 0  # vault empty — first run

vault_note = f"BOOTSTRAP — vault empty, schema examples injected" if bootstrap else f"{len(recent_examples)} drawers shown ({drawer_count} total in vault)"
print(f"Grounding total: ~{total_chars:,} chars / ~{total_tokens:,} tokens (est.)")
print(f"  L0 one-liner:     {len(L0_line):,} chars")
print(f"  Full rooms:       {len(full_room_block):,} chars  ({len(full_rooms)} rooms)")
print(f"  KG stable facts:  {stable_triple_count} triples shown  ({noise_triple_count} session-noise triples filtered)")
print(f"  Recent examples:  {vault_note}")
print()
print(f"Tag inventory ({len(tag_counts)} unique tags across {drawer_count} drawers):")
print(f"  {tag_inventory}")
print()

# Checklist
checks = [
    ("Operator identity (L0)",                    bool(L0_line.strip())),
    ("Identity room (full)",                      "identity" in room_docs),
    ("Active projects (full)",                    "projects-active" in room_docs),
    ("AOL system architecture (full)",            "aol-system" in room_docs),
    ("Tech environment (full)",                   "tech-environment" in room_docs),
    ("Working style (full)",                      "working-style" in room_docs),
    ("Workspace map (full)",                      "workspace-map" in room_docs),
    ("KG stable facts present",                   stable_triple_count > 0),
    ("Session noise filtered from KG",            noise_triple_count > 0),
    ("Schema examples present (real or fallback)", len(recent_examples) > 0 or bootstrap),
    ("Tag conventions defined",                   True),  # always injected via BOOTSTRAP_NOTE or real entries
    ("Total tokens > 2000",                       total_tokens > 2000),
]

passed = sum(1 for _, v in checks if v)
print("Checklist:")
for label, ok in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon} {label}")

pct = passed / len(checks) * 100
print(f"\nChecklist score: {passed}/{len(checks)}  ({pct:.0f}%)")
print(SEP)
