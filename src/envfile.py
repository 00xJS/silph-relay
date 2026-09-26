"""Load a local .env for runs on your own machine.

CI passes real environment variables; this only exists so `python3 src/main.py`
works locally without installing python-dotenv. Existing environment variables
always win over the file.
"""
import os
from pathlib import Path

_loaded = False


def load(path=".env"):
    """Read KEY=VALUE lines from `path` into os.environ (once per process)."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # trailing comment
        if key:
            os.environ.setdefault(key, value)
