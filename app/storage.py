"""Tiny JSON-file storage. Everything the app remembers lives in DATA_DIR.

Files:
  settings.json  - app settings (Plex, Cloudflare, password)
  presets.json   - saved notice presets
  media.json     - index of uploaded clips and images
  state.json     - what is active right now, job progress, activity log
  secret.key     - random key used to sign login cookies
  clips/         - uploaded placeholder clips
  images/        - uploaded images (poster backgrounds / full posters)
  posters/       - rendered poster/art of the active notice
"""
import json
import os
import secrets
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
NOTICE_DIR = Path(os.environ.get("NOTICE_DIR", "/notice"))

CLIPS_DIR = DATA_DIR / "clips"
IMAGES_DIR = DATA_DIR / "images"
POSTERS_DIR = DATA_DIR / "posters"

_lock = threading.RLock()


def ensure_dirs():
    for d in (DATA_DIR, CLIPS_DIR, IMAGES_DIR, POSTERS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            uid, gid = os.getuid(), os.getgid()
            raise SystemExit(
                f"\n[ERROR] Can't create {d}: the app runs as UID {uid}, GID {gid}, and isn't allowed to write "
                f"to the host folder mounted at {DATA_DIR}.\n"
                f"Fix: remove the 'user:' line from compose and set PUID/PGID instead (the container then fixes "
                f"ownership itself), or on the host run:  chown -R {uid}:{gid} <the folder mapped to {DATA_DIR}>\n")


def _path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def load(name: str, default):
    with _lock:
        p = _path(name)
        if not p.exists():
            return json.loads(json.dumps(default))
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            # Keep the broken file for inspection and start fresh.
            p.replace(p.with_suffix(".json.corrupt"))
            return json.loads(json.dumps(default))


def save(name: str, data) -> None:
    with _lock:
        p = _path(name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)


def update(name: str, default, fn):
    """Load, let fn modify (or return a new value), save. Returns the new data."""
    with _lock:
        data = load(name, default)
        result = fn(data)
        if result is not None:
            data = result
        save(name, data)
        return data


def secret_key() -> bytes:
    p = DATA_DIR / "secret.key"
    with _lock:
        if not p.exists():
            p.write_text(secrets.token_hex(32))
        return p.read_text().strip().encode()
