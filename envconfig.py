"""Tiny .env loader — no external dependency (no python-dotenv required).

Reads KEY=VALUE lines from a .env file next to this module and merges them
into os.environ (without overwriting variables already set in the real
environment). Safe to import multiple times.
"""
import os

_LOADED = False


def load_env(path=None):
    global _LOADED
    if _LOADED:
        return
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    _LOADED = True


def get(key, default=None):
    load_env()
    return os.environ.get(key, default)


def get_float(key, default):
    try:
        return float(get(key, default))
    except (TypeError, ValueError):
        return default


def get_int(key, default):
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        return default
