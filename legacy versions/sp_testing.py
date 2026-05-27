"""
sp_testing.py — Screenpipe data dump
Mirrors the exact fetch_context pipeline from filter_agent.py and prints
the raw + formatted output to stdout. No LLM call, no writes.

Run: python "legacy versions/sp_testing.py" [lookback_minutes]
Default lookback: 1500m (same as FA)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import requests

load_dotenv()

SCREENPIPE_API      = "http://localhost:3030"
SCREENPIPE_API_KEY  = os.environ.get("SCREENPIPE_API_KEY", "")
LOOKBACK_MIN        = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
SEARCH_LIMIT        = 150   # OCR
AUDIO_SEARCH_LIMIT  = 300   # 25h / 5min chunks = up to 300 entries

_AUDIO_DEVICE_LABELS: dict[str, tuple[str, str]] = {
    "Microphone (Razer Seiren Mini)":              ("AUDIO_OUT", "Aero"),
    "Headphones (HyperX Cloud Flight 2 Wireless)": ("AUDIO_IN",  "Inbound"),
}


def sp_headers() -> dict:
    return {"Authorization": f"Bearer {SCREENPIPE_API_KEY}"}


def time_window(minutes_back: int) -> tuple[str, str]:
    now   = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes_back)
    fmt   = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), now.strftime(fmt)


# ---------------------------------------------------------------------------
# /activity-summary — app usage stats only
# ---------------------------------------------------------------------------

def fetch_activity_summary(minutes_back: int) -> dict | None:
    start, end = time_window(minutes_back)
    print(f"\n[fetch] /activity-summary | window: {start} → {end} ({minutes_back}m)")
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
        print(f"[raw]   {frames} frames | {seg_count} audio segments | apps: {apps or '(none)'}")

        print("\n--- RAW JSON (/activity-summary) ---")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("--- END RAW JSON ---\n")

        has_frames = frames > 0
        has_audio  = seg_count > 0
        if not has_frames and not has_audio:
            print("[info]  activity-summary empty")
            return None
        return data
    except Exception as e:
        print(f"[warn]  activity-summary fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# /search — actual OCR + audio transcriptions
# ---------------------------------------------------------------------------

def fetch_search(content_type: str, minutes_back: int, limit: int = SEARCH_LIMIT) -> list[dict]:
    start, end = time_window(minutes_back)
    print(f"\n[fetch] /search?content_type={content_type} | window: {start} → {end} | limit: {limit}")
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
        print(f"[raw]   {len(results)} results")

        print(f"\n--- RAW JSON (/search {content_type}, first 10) ---")
        print(json.dumps(results[:10], indent=2, ensure_ascii=False))
        if len(results) > 10:
            print(f"  ... ({len(results) - 10} more entries omitted)")
        print("--- END RAW JSON ---\n")

        return results
    except Exception as e:
        print(f"[warn]  /search {content_type} failed: {e}")
        return []


# ---------------------------------------------------------------------------
# format_hybrid — exact copy of FA formatter
# ---------------------------------------------------------------------------

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
# fetch_context — hybrid pipeline (mirrors filter_agent.py exactly)
# ---------------------------------------------------------------------------

def fetch_context(minutes_back: int) -> tuple[str, str]:
    summary      = fetch_activity_summary(minutes_back)
    ocr_events   = fetch_search("ocr",   minutes_back, limit=SEARCH_LIMIT)
    audio_events = fetch_search("audio", minutes_back, limit=AUDIO_SEARCH_LIMIT)
    print(f"[info]  raw totals: {len(ocr_events)} OCR | {len(audio_events)} audio")
    formatted = format_hybrid(summary, ocr_events, audio_events)
    return formatted, "hybrid"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_screenpipe() -> bool:
    try:
        r = requests.get(f"{SCREENPIPE_API}/health", headers=sp_headers(), timeout=5)
        r.raise_for_status()
        print("[ok]    screenpipe health OK")
        return True
    except Exception as e:
        print(f"[error] screenpipe unreachable: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"=== sp_testing.py | lookback: {LOOKBACK_MIN}m ({LOOKBACK_MIN/60:.1f}h) ===")
    print(f"    api: {SCREENPIPE_API}")
    print(f"    key: {'present' if SCREENPIPE_API_KEY else 'MISSING'}")

    if not check_screenpipe():
        sys.exit(1)

    context, source = fetch_context(LOOKBACK_MIN)

    print("\n=== FORMATTED CONTEXT (what FA sends to Claude) ===")
    print(f"source: {source} | {len(context)} chars (~{len(context)//4} tokens)")
    print("---")
    print(context)
    print("=== END FORMATTED CONTEXT ===")
