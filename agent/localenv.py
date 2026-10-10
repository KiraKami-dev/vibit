"""
Local runs read secrets and switches from agent/.env (see .env.example): KEY=VALUE lines, `#` comments, an optional
`export ` prefix and optional quotes. Variables already set in the environment win, empty values are skipped, and a
missing file is fine. Test and scored runs never read it: they get CHALLENGE_RUN_TOKEN and their keys as team
secrets, and .env is gitignored, so it is never packaged.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")


def parse(text: str) -> dict[str, str]:
    """The KEY=VALUE pairs of a .env text, in order; malformed lines are ignored."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load(path: Path = ENV_FILE, environ=os.environ) -> list[str]:
    """Set the variables from `path` that the environment lacks; returns their names. A no-op in a platform run."""
    if environ.get("CHALLENGE_RUN_TOKEN"):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    loaded = []
    for key, value in parse(text).items():
        if value and key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


load()  # on import: the entry point imports this module before any module that reads its settings
