"""Short-lived server-side cache for uploaded files (keeps large payloads out of the browser stores)."""

from __future__ import annotations

import base64
import threading
import time
import uuid

_lock = threading.Lock()
_files: dict[str, tuple[float, str, bytes]] = {}
TTL = 3600.0


def put_data_url(filename: str, contents: str) -> str:
    """Store a ``dcc.Upload`` data URL; returns a token."""
    _header, _, payload = contents.partition(",")
    data = base64.b64decode(payload)
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _lock:
        for k in [k for k, (exp, _, _) in _files.items() if exp <= now]:
            _files.pop(k, None)
        _files[token] = (now + TTL, filename, data)
    return token


def get(token: str | None) -> tuple[str, bytes] | None:
    if not token:
        return None
    with _lock:
        entry = _files.get(token)
    if entry is None or entry[0] <= time.monotonic():
        return None
    return entry[1], entry[2]


def drop(token: str | None) -> None:
    if token:
        with _lock:
            _files.pop(token, None)
