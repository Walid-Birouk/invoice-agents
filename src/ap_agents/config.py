"""Paths and config loading."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=value lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")
DATA = Path(os.environ.get("AP_DATA", ROOT / "data"))
STATE = DATA / "state"  # runtime output (payment queue, email drafts, audit log); safe to delete


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "agents.yaml").read_text())
