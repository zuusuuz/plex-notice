"""Minimal Plex Media Server API client (only the calls this app needs)."""
import posixpath
import time

import requests

from . import config


class PlexError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Plex:
    def __init__(self, url: str, token: str, verify: bool = True, timeout: float = 20):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "X-Plex-Token": self.token,
            "X-Plex-Product": config.APP_NAME,
            "X-Plex-Version": config.VERSION,
            "X-Plex-Client-Identifier": "plex-notice-app",
        })

    @classmethod
    def from_settings(cls, s: dict, timeout: float = 20) -> "Plex":
        if not s.get("plex_url"):
            raise PlexError("Plex URL is not set (Settings → Plex connection).")
        return cls(s["plex_url"], s.get("plex_token", ""), bool(s.get("plex_verify_ssl", True)), timeout)

    # ---------- plumbing ----------
    def _req(self, method: str, path: str, params=None, data=None, headers=None):
        url = self.url + path
        try:
            r = self.session.request(method, url, params=params, data=data, headers=headers,
                                     timeout=self.timeout, verify=self.verify)
        except requests.exceptions.SSLError as e:
            raise PlexError(f"SSL error talking to Plex ({e}). If Plex uses a self-signed "
                            "certificate, use http:// or untick 'Verify SSL'.")
        except requests.exceptions.ConnectionError:
            raise PlexError(f"Can't reach Plex at {self.url}. Is the address right and is Plex running?")
        except requests.exceptions.Timeout:
            raise PlexError(f"Plex at {self.url} didn't answer within {self.timeout:.0f}s.")
        if r.status_code == 401:
            raise PlexError("Plex rejected the token (401 Unauthorized). Check the Plex token in Settings.", 401)
        if r.status_code >= 400:
            raise PlexError(f"Plex returned HTTP {r.status_code} for {method} {path}", r.status_code)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    @staticmethod
    def _mc(js) -> dict:
        return (js or {}).get("MediaContainer", {}) or {}

    # ---------- reads ----------
    def identity(self) -> dict:
        return self._mc(self._req("GET", "/identity"))

    def server_info(self) -> dict:
        return self._mc(self._req("GET", "/"))

    def sections(self) -> list:
        return self._mc(self._req("GET", "/library/sections")).get("Directory", []) or []

    def section(self, key: str):
        for d in self.sections():
            if str(d.get("key")) == str(key):
                return d
        return None

    def prefs(self) -> dict:
        settings = self._mc(self._req("GET", "/:/prefs")).get("Setting", []) or []
        return {s.get("id"): s.get("value") for s in settings}

    def metadata(self, rating_key, check_files=False):
        """Return the item's metadata dict, or None if Plex says it doesn't exist."""
        params = {"checkFiles": 1} if check_files else None
        try:
            items = self._mc(self._req("GET", f"/library/metadata/{rating_key}", params=params)).get("Metadata", [])
        except PlexError as e:
            if e.status == 404:
                return None
            raise
        return items[0] if items else None

    def recent_items(self, section_key, size=50) -> list:
        params = {
            "type": 1,
            "sort": "addedAt:desc",
            "X-Plex-Container-Start": 0,
            "X-Plex-Container-Size": size,
        }
        return self._mc(self._req("GET", f"/library/sections/{section_key}/all", params=params)).get("Metadata", []) or []

    @staticmethod
    def item_files(item: dict) -> list:
        files = []
        for m in item.get("Media", []) or []:
            for p in m.get("Part", []) or []:
                if p.get("file"):
                    files.append(p["file"])
        return files

    def find_item_in_folders(self, section_key, plex_folders):
        """Find the item whose media file sits inside one of plex_folders (Plex's own paths).
        Returns (item, folder) or None."""
        prefixes = [(f.rstrip("/") + "/", f) for f in plex_folders]

        def match(item):
            for f in self.item_files(item):
                for prefix, folder in prefixes:
                    if f.startswith(prefix):
                        return folder
            return None

        items = self.recent_items(section_key)
        for it in items:
            folder = match(it)
            if folder:
                return it, folder
        # Some Plex versions omit Media details in listings; check the newest few in full.
        for it in items[:10]:
            if not it.get("Media"):
                full = self.metadata(it.get("ratingKey"))
                folder = match(full) if full else None
                if folder:
                    return full, folder
        return None

    # ---------- writes ----------
    def scan_path(self, section_key, plex_path: str):
        self._req("GET", f"/library/sections/{section_key}/refresh", params={"path": plex_path})

    def unmatch(self, rating_key):
        self._req("PUT", f"/library/metadata/{rating_key}/unmatch")

    def edit(self, section_key, rating_key, fields: dict, lock=True):
        params = {"type": 1, "id": rating_key}
        for k, v in fields.items():
            params[f"{k}.value"] = v
            if lock:
                params[f"{k}.locked"] = 1
        self._req("PUT", f"/library/sections/{section_key}/all", params=params)

    def lock_fields(self, section_key, rating_key, names):
        params = {"type": 1, "id": rating_key}
        for n in names:
            params[f"{n}.locked"] = 1
        self._req("PUT", f"/library/sections/{section_key}/all", params=params)

    def upload_poster(self, rating_key, image_bytes: bytes):
        self._req("POST", f"/library/metadata/{rating_key}/posters", data=image_bytes)

    def upload_art(self, rating_key, image_bytes: bytes):
        self._req("POST", f"/library/metadata/{rating_key}/arts", data=image_bytes)

    def delete_item(self, rating_key):
        self._req("DELETE", f"/library/metadata/{rating_key}")

    def empty_trash(self, section_key):
        self._req("PUT", f"/library/sections/{section_key}/emptyTrash")


def path_inside(child: str, parent: str) -> bool:
    child = posixpath.normpath(child)
    parent = posixpath.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def wait_for(fn, timeout: float, interval: float = 3.0):
    """Call fn until it returns something truthy or timeout passes."""
    deadline = time.time() + timeout
    while True:
        result = fn()
        if result:
            return result
        if time.time() >= deadline:
            return None
        time.sleep(interval)
