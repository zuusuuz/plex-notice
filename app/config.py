"""Settings, presets, media index and runtime state, with their defaults."""
import os
import time
import uuid

from . import storage

APP_NAME = "Plex Notice"
VERSION = "1.0.0"
PORT = int(os.environ.get("PORT", "8742"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))
DISABLE_AUTH = os.environ.get("DISABLE_AUTH", "").strip().lower() in ("1", "true", "yes")

DEFAULT_STATUS_TITLE = "Temporarily unavailable"
DEFAULT_STATUS_MESSAGE = (
    "The server is offline right now, either for planned maintenance or an "
    "unexpected outage. Everything will be back as soon as possible."
)

DEFAULT_SETTINGS = {
    # Plex connection
    "plex_url": "",
    "plex_token": "",
    "plex_verify_ssl": True,
    "section_key": "",
    "notice_folder_name": "",       # blank = detect from the /notice mount
    "plex_path_override": "",       # blank = library folder + notice folder name
    # Notice behaviour
    "pin_date": "2037-01-01",
    "sort_to_top": True,
    "pin_release_date": True,       # also first in "Recently Released"
    "watchdog_minutes": 10,
    "scan_timeout_seconds": 120,
    "empty_trash_fallback": False,
    # Cloudflare status page
    "cf_enabled": False,
    "cf_account_id": "",
    "cf_namespace_id": "",
    "cf_api_token": "",
    "cf_expiry_hours": 24,
    "cf_default_title": DEFAULT_STATUS_TITLE,
    "cf_default_message": DEFAULT_STATUS_MESSAGE,
    # Web UI password
    "auth_enabled": False,
    "auth_hash": "",
}

THEMES = {
    "amber": {"label": "Hazard amber", "top": "#2b1d06", "bottom": "#0f0a03", "accent": "#ffb020"},
    "red": {"label": "Alert red", "top": "#2c0a0d", "bottom": "#0e0405", "accent": "#ff4d4f"},
    "blue": {"label": "Calm blue", "top": "#0a1a33", "bottom": "#040a15", "accent": "#4ea8ff"},
    "green": {"label": "All clear green", "top": "#0a2618", "bottom": "#03100a", "accent": "#3ddc84"},
    "purple": {"label": "Midnight purple", "top": "#1f1036", "bottom": "#0a0613", "accent": "#b18cff"},
    "mono": {"label": "Monochrome", "top": "#1d1f24", "bottom": "#0a0b0d", "accent": "#e8e8e8"},
}

DEFAULT_PRESET = {
    "id": "",
    "name": "",
    "title": "Server Maintenance",
    "summary": "",
    "tagline": "",
    "clip_id": "",
    "poster_mode": "generated",
    "theme": "amber",
    "kicker": "SERVER NOTICE",
    "headline": "PLEX OUTAGE",
    "subline": "",
    "footer": "",
    "stripes": True,
    "bg_image_id": "",
    "image_id": "",
    "status_title": "",
    "status_message": "",
    "updated": 0,
}

SAMPLE_PRESETS = [
    {
        "name": "Planned maintenance",
        "title": "⚠ Planned Maintenance",
        "summary": (
            "Heads up! Plex will be offline for planned maintenance.\n\n"
            "When: <day, date and time here>\n"
            "Expected back: <time here>\n\n"
            "During the outage, check our status page for updates."
        ),
        "tagline": "Plex will be offline soon",
        "theme": "amber",
        "kicker": "SERVER NOTICE",
        "headline": "PLANNED MAINTENANCE",
        "subline": "Saturday · 9am – 3pm",
        "footer": "",
        "status_title": "Planned maintenance in progress",
        "status_message": "Plex is offline for planned maintenance and should be back soon.",
    },
    {
        "name": "Electrical work",
        "title": "⚡ Power Outage – Electrical Work",
        "summary": (
            "Electrical work is happening at the house, so the whole server will be "
            "switched off.\n\nWhen: <day, date and time here>\n"
            "Expected back: <time here>"
        ),
        "tagline": "The server will be powered off",
        "theme": "red",
        "kicker": "HEADS UP",
        "headline": "PLEX OUTAGE",
        "subline": "Electrical work at the house",
        "footer": "",
        "status_title": "Power is off for electrical work",
        "status_message": "The house (and the server) is powered off for electrical work. Plex will be back once the power is on.",
    },
]

DEFAULT_STATE = {
    "active": None,      # see notices.activate for the shape
    "job": None,         # current/last background job {kind, status, steps, error, started, finished}
    "leftovers": [],     # Plex rating keys of old notices Plex wouldn't delete
    "last_watchdog": 0,
    "watchdog_note": "",
    "log": [],
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------- settings ----------
def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(storage.load("settings", {}))
    return s


def save_settings(s: dict) -> None:
    clean = {k: s.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    storage.save("settings", clean)


def auth_required() -> bool:
    if DISABLE_AUTH:
        return False
    s = get_settings()
    return bool(s.get("auth_enabled") and s.get("auth_hash"))


# ---------- presets ----------
def get_presets() -> list:
    out = []
    for p in storage.load("presets", []):
        full = dict(DEFAULT_PRESET)
        full.update(p)
        out.append(full)
    out.sort(key=lambda p: p["name"].lower())
    return out


def get_preset(pid: str):
    for p in get_presets():
        if p["id"] == pid:
            return p
    return None


def save_preset(preset: dict) -> dict:
    preset = {k: preset.get(k, v) for k, v in DEFAULT_PRESET.items()}
    if not preset["id"]:
        preset["id"] = new_id()
    preset["updated"] = time.time()

    def fn(items):
        items[:] = [p for p in items if p.get("id") != preset["id"]]
        items.append(preset)

    storage.update("presets", [], fn)
    return preset


def delete_preset(pid: str) -> None:
    storage.update("presets", [], lambda items: [p for p in items if p.get("id") != pid])


def seed_samples() -> None:
    """Create example presets the very first time the app starts."""
    if (storage.DATA_DIR / "presets.json").exists():
        return
    media = get_media()
    default_clip = media["clips"][0]["id"] if media["clips"] else ""
    for sample in SAMPLE_PRESETS:
        p = dict(DEFAULT_PRESET)
        p.update(sample)
        p["clip_id"] = default_clip
        save_preset(p)


# ---------- media ----------
def get_media() -> dict:
    m = storage.load("media", {"clips": [], "images": []})
    m.setdefault("clips", [])
    m.setdefault("images", [])
    return m


def save_media(m: dict) -> None:
    storage.save("media", m)


def find_clip(cid: str):
    return next((c for c in get_media()["clips"] if c["id"] == cid), None)


def find_image(iid: str):
    return next((i for i in get_media()["images"] if i["id"] == iid), None)


# ---------- state ----------
def get_state() -> dict:
    st = dict(DEFAULT_STATE)
    st.update(storage.load("state", {}))
    return st


def update_state(fn) -> dict:
    def wrapped(data):
        for k, v in DEFAULT_STATE.items():
            data.setdefault(k, v if not isinstance(v, list) else list(v))
        return fn(data)

    return storage.update("state", {}, wrapped)


def log(message: str, level: str = "info") -> None:
    print(f"[{level.upper()}] {message}", flush=True)

    def fn(st):
        st["log"].insert(0, {"t": time.time(), "level": level, "msg": message})
        del st["log"][150:]

    update_state(fn)
