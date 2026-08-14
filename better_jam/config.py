from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT_ROOT / ".spotify_token.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "playlist-modify-private"
)


def load_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def client_id() -> str:
    load_env()
    value = os.getenv("SPOTIFY_CLIENT_ID")
    if not value:
        raise RuntimeError("Set SPOTIFY_CLIENT_ID in .env first.")
    return value


def float_setting(name: str, default: float) -> float:
    load_env()
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number, got {raw_value!r}") from error


def string_setting(name: str, default: str = "") -> str:
    load_env()
    return os.getenv(name, default).strip()
