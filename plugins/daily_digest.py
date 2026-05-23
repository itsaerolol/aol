import logging
import os
from datetime import datetime
from plugins.base import AOLPlugin

log = logging.getLogger("filter.plugins.daily_digest")

DIGEST_DIR = os.path.expanduser("~/aol-memory/digests")


class DailyDigestPlugin(AOLPlugin):
    """
    Collects HIGH-priority memories across the day and writes a plain-text
    digest to ~/aol-memory/digests/YYYY-MM-DD.txt at midnight rollover or
    clean shutdown.
    """
    name = "daily_digest"

    def __init__(self):
        self._memories: list[dict] = []
        self._date = datetime.now().date()

    def process(self, result: dict, context: str, source: str) -> None:
        today = datetime.now().date()
        if today != self._date:
            self._flush()
            self._memories = []
            self._date = today

        for mem in result.get("memories", []):
            if mem.get("priority") in ("HIGH", "MEDIUM"):
                self._memories.append(mem)

    def on_stop(self) -> None:
        self._flush()

    def _flush(self):
        if not self._memories:
            return
        os.makedirs(DIGEST_DIR, exist_ok=True)
        path = os.path.join(DIGEST_DIR, f"{self._date}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"AOL Daily Digest — {self._date}\n")
            f.write("=" * 50 + "\n\n")
            for i, m in enumerate(self._memories, 1):
                priority = m.get("priority", "?")
                domain   = m.get("domain", "?")
                project  = m.get("project") or ""
                summary  = m.get("summary", "")
                tags     = ", ".join(m.get("tags", []))
                f.write(f"{i}. [{priority}] [{domain}]{' · ' + project if project else ''}\n")
                f.write(f"   {summary}\n")
                if tags:
                    f.write(f"   tags: {tags}\n")
                f.write("\n")
        log.info(f"digest written → {path} ({len(self._memories)} entries)")
        self._memories = []
