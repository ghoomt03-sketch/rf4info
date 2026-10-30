from __future__ import annotations

import json
from typing import Any

from .config import PUBLIC_MODE, USER_SETTINGS_PATH


def load_user_settings() -> dict[str, Any]:
    if PUBLIC_MODE:
        return {}
    try:
        value = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_settings(**values: Any) -> None:
    if PUBLIC_MODE:
        return
    current = load_user_settings()
    current.update(values)
    USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = USER_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(USER_SETTINGS_PATH)
