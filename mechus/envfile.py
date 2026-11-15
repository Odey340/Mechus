from __future__ import annotations

import os


def _apply_provider_env() -> None:
    try:
        from mechus.openai_compat import apply_provider_env

        apply_provider_env()
    except Exception:
        pass


def _load_local_override(path: str) -> None:
    """Load `.env.local` next to `.env` (gitignored; keep API keys and DB URLs there)."""
    if os.path.basename(path) != ".env":
        return
    local = os.path.join(os.path.dirname(path) or ".", ".env.local")
    if os.path.exists(local):
        _read_into_environ(local)


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        _load_local_override(path)
        _apply_provider_env()
        return
    _read_into_environ(path)
    _load_local_override(path)
    _apply_provider_env()


def _read_into_environ(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        for raw in f.readlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
                value = value[1:-1]
            os.environ[key] = value
