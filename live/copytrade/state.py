"""Atomic JSON state persistence + JSONL append helpers."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


def _atomic_write_json(path: str, body: Any) -> None:
    """Write JSON atomically: tmp file in same dir + os.replace."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_seen_ts": {}}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "last_seen_ts" not in data:
            return {"last_seen_ts": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corrupt state at %s, defaulting: %s", path, e)
        return {"last_seen_ts": {}}


def save_state(path: str, body: dict) -> None:
    _atomic_write_json(path, body)


def load_portfolio(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corrupt portfolio at %s, defaulting: %s", path, e)
        return {}


def save_portfolio(path: str, body: dict) -> None:
    _atomic_write_json(path, body)


def append_decision(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def append_equity(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
