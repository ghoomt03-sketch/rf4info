from __future__ import annotations

import os

from public_runtime import prepare_public_runtime


runtime_database = prepare_public_runtime()
os.environ.setdefault("RF4_APP_MODE", "public")
os.environ.setdefault("RF4_DB_PATH", str(runtime_database))

import app  # noqa: E402,F401  Streamlit executes app.py at import time.
