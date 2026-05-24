"""AOL C2 — entry point. Run this file to start the full system."""

import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.path.expanduser("~/aol_c2.log"), "a", buffering=1)
    sys.stderr = sys.stdout

import uvicorn
from server import app
from config import C2_PORT

if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=C2_PORT, log_level="error")
    except KeyboardInterrupt:
        pass
