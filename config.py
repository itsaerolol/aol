import os
import sys

IDLE_THRESHOLD_MINUTES = 10
BLACKOUT_START_HOUR    = 4
BLACKOUT_END_HOUR      = 10
CHECK_INTERVAL_SECONDS = 30

FILTER_AGENT_HOUR   = 21   # daily FA run time
FILTER_AGENT_MINUTE = 30
C2_PORT                = 45139
LOG_CAP                = 200

SCREENPIPE_CMD = ["npx", "screenpipe@latest", "record", "--audio-chunk-duration", "300", "--filter-music"]
SCREENPIPE_API = "http://localhost:3030"

SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
FILTER_AGENT_SCRIPT = os.path.join(SCRIPT_DIR, "filter_agent.py")
FILTER_AGENT_PYTHON = os.environ.get("FILTER_AGENT_PYTHON") or sys.executable

MEMPALACE_PATH      = os.path.expanduser("~/aol-memory")
MEMPALACE_KG_PATH   = os.path.join(MEMPALACE_PATH, "knowledge_graph.sqlite3")
MEMPALACE_WING      = "aero-ops"
MEMPALACE_ROOM      = "daily-activity"
MEMPALACE_KG_ENTITY = "aero"
