"""The engine: activates/deactivates notices in Plex, runs the watchdog and setup checks."""
import os
import posixpath
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import cloudflare, config, poster, storage
from .cloudflare import CloudflareError
from .plex import Plex, PlexError, path_inside, wait_for

JOB_LOCK = threading.Lock()
FOLDER_RE = re.compile(r"^notice-\d{8}-\d{6}$")
DEMOTE_EPOCH = 946684800          # 1 Jan 2000: sinks an item out of Recently Added
MAX_SAFE_EPOCH = 2147483647       # 19 Jan 2038: 32-bit limit some clients still have
ACTIVE_POSTER = "active-poster.jpg"
ACTIVE_ART = "active-art.jpg"


class NoticeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def pin_epoch(s: dict) -> int:
    try:
        dt = datetime.strptime(str(s.get("pin_date") or "2037-01-01"), "%Y-%m-%d")
    except ValueError:
        dt = datetime(2037, 1, 1)
    return min(int(dt.timestamp()), MAX_SAFE_EPOCH - 86400)


def pin_date_str(s: dict) -> str:
    return datetime.fromtimestamp(pin_epoch(s)).strftime("%Y-%m-%d")


def fmt_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%a %-d %b %Y, %-I:%M %p")


def _step(msg: str, level: str = "info"):
    def fn(st):
        if st.get("job"):
            st["job"]["steps"].append({"t": time.time(), "msg": msg, "level": level})
    config.update_state(fn)
    config.log(msg, level)


SENTINEL = ".plex-notice-keep"
SENTINEL_TEXT = (
    "Created by Plex Notice. It keeps this folder from being removed when Plex deletes a notice\n"
    "and tidies up empty folders. Plex ignores hidden files, so it never shows up in your library.\n"
)
MISSING_DIR_HELP = (
    "The notice folder {d} is gone inside the container. This happens if its folder was deleted on the host "
    "(e.g. Plex removing empty folders) while the container was running. Recreate the container so Docker "
    "mounts it again: docker compose up -d --force-recreate plex-notice"
)


def ensure_sentinel():
    """Keep a hidden file in the notice folder so it's never empty, and so never deleted as an empty folder."""
    f = storage.NOTICE_DIR / SENTINEL
    try:
        if storage.NOTICE_DIR.is_dir() and not f.exists():
            f.write_text(SENTINEL_TEXT)
    except OSError:
        pass


def _check_notice_dir():
    d = storage.NOTICE_DIR
    if not d.is_dir():
        raise NoticeError(MISSING_DIR_HELP.format(d=d))
    test = d / ".write-test"
    try:
        test.write_text("ok")
        test.unlink()
    except OSError as e:
        raise NoticeError(f"Can't write to the notice folder {d} ({e.strerror}). It must be owned by the "
                          f"app's user (UID {os.getuid()}, GID {os.getgid()}, from PUID/PGID). Restart the container "
                          "so it can fix the owner, or run chown on that folder on the host.")
    ensure_sentinel()


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def detect_folder_name() -> str:
    """Name of the host folder mounted at /notice, read from the container's mount table."""
    target = str(storage.NOTICE_DIR).rstrip("/")
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 4 and _unescape(parts[4]).rstrip("/") == target:
                    return posixpath.basename(_unescape(parts[3]).rstrip("/"))
    except OSError:
        pass
    return ""


def folder_name(s: dict) -> tuple[str, str]:
    """(name, where it came from): the notice folder's name inside the library."""
    name = (s.get("notice_folder_name") or "").strip().strip("/")
    if name:
        return name, "setting"
    detected = detect_folder_name()
    if detected:
        return detected, "detected"
    return "_ServerNotice", "default"


def library_locations(sec: dict) -> list:
    return [l.get("path", "") for l in sec.get("Location", []) or [] if l.get("path")]


def plex_candidates(s: dict, sec: dict) -> list:
    """Where Plex should find the notice folder. Normally one path per library folder."""
    override = (s.get("plex_path_override") or s.get("plex_notice_path") or "").strip()
    if override:
        return [override.rstrip("/")]
    name, _ = folder_name(s)
    return [posixpath.join(loc.rstrip("/") or "/", name) for loc in library_locations(sec)]


def _section(plex: Plex, s: dict) -> dict:
    key = str(s.get("section_key") or "")
    if not key:
        raise NoticeError("No Plex library selected (Settings → Plex library & folders).")
    sec = plex.section(key)
    if not sec:
        raise NoticeError(f"Plex has no library with key {key}. Pick the library again in Settings.")
    if sec.get("type") != "movie":
        raise NoticeError(f"Library '{sec.get('title')}' is a {sec.get('type')} library. Only Movies and "
                          "Other Videos libraries are supported.")
    locations = library_locations(sec)
    override = (s.get("plex_path_override") or s.get("plex_notice_path") or "").strip()
    if override and not any(path_inside(override, loc) for loc in locations):
        raise NoticeError(f"The custom Plex path {override} isn't inside the '{sec.get('title')}' library's "
                          f"folders ({', '.join(locations) or 'none'}).")
    return sec


def _remove_local(folder: str):
    if not folder or not FOLDER_RE.match(folder):
        return
    target = (storage.NOTICE_DIR / folder)
    if target.resolve().parent != storage.NOTICE_DIR.resolve():
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def _remove_active_images():
    for name in (ACTIVE_POSTER, ACTIVE_ART):
        try:
            (storage.POSTERS_DIR / name).unlink()
        except FileNotFoundError:
            pass


def _cleanup_leftovers(plex: Plex):
    st = config.get_state()
    remaining = []
    for rk in st.get("leftovers", []):
        try:
            if plex.metadata(rk) is None:
                config.log(f"Leftover Plex item {rk} is gone now.")
                continue
            plex.delete_item(rk)
            config.log(f"Deleted leftover Plex item {rk}.")
        except PlexError:
            remaining.append(rk)

    def fn(state):
        state["leftovers"] = remaining
    config.update_state(fn)


# ---------------------------------------------------------------- jobs
def _job_begin(kind: str, label: str):
    def fn(st):
        st["job"] = {"kind": kind, "label": label, "status": "running", "steps": [],
                     "error": "", "started": time.time(), "finished": 0}
    config.update_state(fn)


def _job_finish(error: str = ""):
    def fn(st):
        if st.get("job"):
            st["job"]["status"] = "failed" if error else "done"
            st["job"]["error"] = error
            st["job"]["finished"] = time.time()
    config.update_state(fn)


def start_job(kind: str, label: str, target, *args):
    """Run target(*args) in the background. Returns (started, message)."""
    if not JOB_LOCK.acquire(blocking=False):
        return False, "Another action is still running. Wait for it to finish."
    _job_begin(kind, label)

    def runner():
        try:
            target(*args)
            _job_finish()
        except Exception as e:  # noqa: BLE001 - surface everything to the UI
            msg = str(e) or e.__class__.__name__
            _step(msg, "error")
            _job_finish(msg)
        finally:
            JOB_LOCK.release()

    threading.Thread(target=runner, daemon=True).start()
    return True, ""


def is_busy() -> bool:
    return JOB_LOCK.locked()


def recover_after_restart():
    def fn(st):
        job = st.get("job")
        if job and job.get("status") == "running":
            job["status"] = "failed"
            job["error"] = "Interrupted because the app restarted. Check the dashboard and try again."
            job["finished"] = time.time()
    config.update_state(fn)


# ---------------------------------------------------------------- activate
def activate(preset_id: str, auto_remove_at, push_status: bool):
    s = config.get_settings()
    preset = config.get_preset(preset_id)
    if not preset:
        raise NoticeError("That preset no longer exists.")
    clip = config.find_clip(preset.get("clip_id", ""))
    if not clip:
        raise NoticeError(f"Preset '{preset['name']}' has no placeholder clip. Edit the preset and pick one.")
    clip_path = storage.CLIPS_DIR / clip["filename"]
    if not clip_path.exists():
        raise NoticeError(f"The clip file for '{clip['name']}' is missing from the data folder. Upload it again.")
    if auto_remove_at and auto_remove_at <= time.time():
        raise NoticeError("The auto-remove time is in the past.")

    plex = Plex.from_settings(s)
    _step("Checking Plex connection, library and notice folder")
    sec = _section(plex, s)
    sec_key = str(sec["key"])
    candidates = plex_candidates(s, sec)
    if not candidates:
        raise NoticeError(f"Plex didn't report any folders for library '{sec.get('title')}'.")
    _check_notice_dir()
    _cleanup_leftovers(plex)

    if config.get_state().get("active"):
        _step("Another notice is active, removing it first")
        _deactivate(plex, s)

    _step("Rendering the poster and background art")
    poster_bytes = poster.render(preset, "poster")
    art_bytes = poster.render(preset, "art")

    folder = "notice-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = Path(clip["filename"]).suffix.lower()
    local_dir = storage.NOTICE_DIR / folder
    local_dir.mkdir(mode=0o775)
    final = local_dir / f"{folder}{ext}"
    tmp = local_dir / f"{folder}{ext}.part"
    _step(f"Copying clip '{clip['name']}' into the notice folder")
    shutil.copyfile(clip_path, tmp)
    os.replace(tmp, final)
    try:
        os.chmod(final, 0o664)
    except OSError:
        pass

    plex_dirs = [posixpath.join(c, folder) for c in candidates]
    rk = None
    plex_dir = plex_dirs[0]
    try:
        for d in plex_dirs:
            _step(f"Asking Plex to scan {d}")
            plex.scan_path(sec_key, d)
        _step("Waiting for Plex to add the item")
        timeout = max(20, int(s.get("scan_timeout_seconds") or 120))
        found = wait_for(lambda: plex.find_item_in_folders(sec_key, plex_dirs), timeout=timeout, interval=3)
        if not found:
            name, source = folder_name(s)
            raise NoticeError(
                f"Plex didn't add the clip within {timeout}s. The app wrote it to {final} and asked Plex to "
                f"look in {' or '.join(plex_dirs)}. Usually this means the folder mounted at /notice in compose "
                f"isn't the folder '{name}' directly inside your library's folder. Check the compose volume and "
                "Settings → Plex library & folders (see Guide → Library in a different place).")
        item, plex_dir = found
        rk = str(item["ratingKey"])
        _step(f"Plex added it as item {rk}. Unmatching so no real film's details get applied")
        try:
            plex.unmatch(rk)
            time.sleep(2)
        except PlexError as e:
            _step(f"Unmatch skipped ({e})", "warn")

        apply_metadata(plex, s, sec_key, rk, preset, poster_bytes, art_bytes)
    except Exception:
        _step("Rolling back: removing the clip again", "warn")
        if rk:
            try:
                plex.edit(sec_key, rk, {"addedAt": DEMOTE_EPOCH})
                plex.delete_item(rk)
            except PlexError:
                config.update_state(lambda st: st["leftovers"].append(rk))
        _remove_local(folder)
        raise

    (storage.POSTERS_DIR / ACTIVE_POSTER).write_bytes(poster_bytes)
    (storage.POSTERS_DIR / ACTIVE_ART).write_bytes(art_bytes)

    active = {
        "preset_id": preset["id"],
        "preset_name": preset["name"],
        "title": preset["title"],
        "clip_name": clip["name"],
        "rating_key": rk,
        "section_key": sec_key,
        "folder": folder,
        "plex_dir": plex_dir,
        "activated_at": time.time(),
        "auto_remove_at": auto_remove_at or None,
        "pin": pin_epoch(s),
        "cf_planned": False,
    }

    if s.get("cf_enabled"):
        try:
            if push_status and (preset.get("status_message") or preset.get("status_title")):
                cloudflare.push_planned(s, preset.get("status_title") or s.get("cf_default_title"),
                                        preset.get("status_message") or s.get("cf_default_message"),
                                        auto_remove_at)
                active["cf_planned"] = True
                _step("Set this preset's message on the Cloudflare status page")
            else:
                cloudflare.clear_planned(s)
                _step("Cleared any old planned message on Cloudflare (the default message applies)")
        except CloudflareError as e:
            _step(f"Cloudflare update failed: {e}. The Plex notice is still live.", "warn")

    def fn(st):
        st["active"] = active
        st["watchdog_note"] = ""
    config.update_state(fn)
    _step(f"Notice '{preset['name']}' is live in Plex")


def apply_metadata(plex: Plex, s: dict, sec_key: str, rk: str, preset: dict, poster_bytes: bytes, art_bytes: bytes):
    pin = pin_epoch(s)
    title = preset.get("title") or "Server Notice"
    fields = {
        "title": title,
        "summary": preset.get("summary", ""),
        "tagline": preset.get("tagline", ""),
        "year": datetime.now().year,
        "addedAt": pin,
    }
    if s.get("sort_to_top"):
        fields["titleSort"] = "!" + title
    if s.get("pin_release_date"):
        fields["originallyAvailableAt"] = pin_date_str(s)
    _step("Setting the title, description and pinned date")
    plex.edit(sec_key, rk, fields)
    _step("Uploading the poster and background art")
    plex.upload_poster(rk, poster_bytes)
    plex.upload_art(rk, art_bytes)
    plex.lock_fields(sec_key, rk, ["thumb", "art"])
    time.sleep(3)
    md = plex.metadata(rk) or {}
    if int(md.get("addedAt") or 0) != pin or md.get("title") != title:
        _step("Plex hadn't kept every change yet, applying them again")
        plex.edit(sec_key, rk, {"title": title, "addedAt": pin})


# ---------------------------------------------------------------- deactivate
def deactivate():
    s = config.get_settings()
    plex = Plex.from_settings(s)
    _deactivate(plex, s)
    _cleanup_leftovers(plex)


def _deactivate(plex: Plex, s: dict):
    a = config.get_state().get("active")
    if not a:
        _step("No notice is active")
        return
    sec_key, rk = a["section_key"], a["rating_key"]
    deleted = False
    ensure_sentinel()  # so Plex's empty-folder cleanup can't take the notice folder with it
    md = plex.metadata(rk)  # raises if Plex is unreachable, keeping the notice marked active
    if md is None:
        _step("The Plex item is already gone")
    else:
        _step("Moving the notice out of Recently Added")
        plex.edit(sec_key, rk, {"addedAt": DEMOTE_EPOCH})
        try:
            plex.delete_item(rk)
            deleted = True
            _step("Deleted the notice item from Plex")
        except PlexError as e:
            _step(f"Plex wouldn't delete the item ({e}). Turn on 'Allow media deletion' in Plex "
                  "(Settings → Library) for clean removal.", "warn")

    _remove_local(a.get("folder", ""))
    _step("Removed the clip from the notice folder")
    if not storage.NOTICE_DIR.is_dir():
        _step(MISSING_DIR_HELP.format(d=storage.NOTICE_DIR), "warn")

    if md is not None and not deleted:
        _step("Rescanning so Plex notices the file is gone")
        plex.scan_path(sec_key, posixpath.dirname(a["plex_dir"]))
        time.sleep(5)
        if s.get("empty_trash_fallback"):
            plex.empty_trash(sec_key)
            _step("Emptied the Movies library trash")
        else:
            config.update_state(lambda st: st["leftovers"].append(rk))
            _step("A hidden 'unavailable' leftover remains (dated 2000, so it's out of Recently Added). "
                  "It's removed automatically once Plex allows media deletion.", "warn")

    def fn(st):
        st["active"] = None
        st["watchdog_note"] = ""
    config.update_state(fn)
    _remove_active_images()

    if s.get("cf_enabled"):
        try:
            cloudflare.clear_planned(s)
            _step("Cleared the planned message on Cloudflare")
        except CloudflareError as e:
            _step(f"Couldn't clear the Cloudflare message: {e}. It will still expire on its own.", "warn")
    _step(f"Notice '{a.get('preset_name')}' removed")


def forget():
    """Drop the active notice from the app's memory without needing Plex (e.g. Plex is gone)."""
    s = config.get_settings()
    a = config.get_state().get("active")
    if not a:
        return
    _remove_local(a.get("folder", ""))

    def fn(st):
        st["active"] = None
        if a.get("rating_key") and a["rating_key"] not in st["leftovers"]:
            st["leftovers"].append(a["rating_key"])
    config.update_state(fn)
    _remove_active_images()
    if s.get("cf_enabled"):
        try:
            cloudflare.clear_planned(s)
        except CloudflareError:
            pass
    config.log(f"Forgot notice '{a.get('preset_name')}' (clip removed; Plex item queued for cleanup).", "warn")


def retry_cleanup():
    s = config.get_settings()
    plex = Plex.from_settings(s)
    before = len(config.get_state().get("leftovers", []))
    _cleanup_leftovers(plex)
    after = len(config.get_state().get("leftovers", []))
    _step(f"Cleanup: {before - after} removed, {after} still waiting")


# ---------------------------------------------------------------- watchdog
def _watch(s: dict):
    note = "OK"
    ensure_sentinel()
    if not storage.NOTICE_DIR.is_dir():
        note = MISSING_DIR_HELP.format(d=storage.NOTICE_DIR)
    try:
        plex = Plex.from_settings(s, timeout=15)
        a = config.get_state().get("active")
        if a:
            md = plex.metadata(a["rating_key"])
            if md is None:
                note = ("The notice item is missing from Plex (removed outside this app?). "
                        "Use 'Forget' and activate it again.")
            else:
                pin = pin_epoch(s)
                fixes = {}
                if int(md.get("addedAt") or 0) != pin:
                    fixes["addedAt"] = pin
                if md.get("title") != a.get("title"):
                    fixes["title"] = a.get("title")
                if s.get("pin_release_date") and md.get("originallyAvailableAt") != pin_date_str(s):
                    fixes["originallyAvailableAt"] = pin_date_str(s)
                if fixes:
                    plex.edit(a["section_key"], a["rating_key"], fixes)
                    config.log(f"Watchdog re-applied: {', '.join(fixes)}", "warn")
        _cleanup_leftovers(plex)
    except Exception as e:  # noqa: BLE001
        note = f"Check failed: {e}"
    if not storage.NOTICE_DIR.is_dir():
        note = MISSING_DIR_HELP.format(d=storage.NOTICE_DIR)

    prev = config.get_state().get("watchdog_note")
    if note != prev and note != "OK":
        config.log(f"Watchdog: {note}", "warn")

    def fn(st):
        st["last_watchdog"] = time.time()
        st["watchdog_note"] = note
    config.update_state(fn)


def watchdog_loop():
    last_check = 0.0
    while True:
        time.sleep(30)
        try:
            a = config.get_state().get("active")
            if a and a.get("auto_remove_at") and time.time() >= a["auto_remove_at"] and not is_busy():
                start_job("deactivate", "Scheduled auto-remove", deactivate)
                continue
            s = config.get_settings()
            minutes = int(s.get("watchdog_minutes") or 0)
            if minutes <= 0 or time.time() - last_check < minutes * 60:
                continue
            if not JOB_LOCK.acquire(blocking=False):
                continue
            try:
                last_check = time.time()
                _watch(s)
            finally:
                JOB_LOCK.release()
        except Exception as e:  # noqa: BLE001
            print(f"[watchdog] {e}", flush=True)


def start_watchdog():
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()


# ---------------------------------------------------------------- setup checks
def run_checks() -> list:
    s = config.get_settings()
    out = []

    def add(name, status, detail, fix=""):
        out.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 1. notice folder
    d = storage.NOTICE_DIR
    try:
        _check_notice_dir()
        if os.path.ismount(d):
            add("Notice folder", "ok", f"{d} is mounted and writable.")
        else:
            add("Notice folder", "warn", f"{d} is writable but doesn't look like a mounted volume.",
                "Make sure the compose file maps your real Movies sub-folder to /notice, otherwise "
                "clips are written inside the container where Plex can't see them.")
    except NoticeError as e:
        add("Notice folder", "fail", str(e))

    # 2-6. Plex
    plex = None
    try:
        plex = Plex.from_settings(s, timeout=8)
        ident = plex.identity()
        add("Plex reachable", "ok", f"Plex {ident.get('version', '?')} at {s['plex_url']}")
    except PlexError as e:
        add("Plex reachable", "fail", str(e), "Check 'Plex URL' in Settings.")
        plex = None

    sec = None
    if plex:
        try:
            plex.sections()
            add("Plex token", "ok", "Token accepted.")
        except PlexError as e:
            add("Plex token", "fail", str(e), "Paste a fresh token in Settings (see the Guide for how to find it).")
            plex = None
    if plex:
        try:
            sec = _section(plex, s)
            name, source = folder_name(s)
            where = " or ".join(plex_candidates(s, sec))
            if (s.get("plex_path_override") or s.get("plex_notice_path") or "").strip():
                add("Library & notice folder", "ok",
                    f"Library '{sec.get('title')}'. Plex will look for notices in {where} (custom path).")
            elif source == "default":
                add("Library & notice folder", "warn",
                    f"Library '{sec.get('title')}'. Couldn't detect the notice folder's name, so assuming "
                    f"'{name}': Plex will look in {where}.",
                    "If your folder has a different name, enter it in Settings → Notice folder name.")
            else:
                how = "detected from your compose file" if source == "detected" else "from Settings"
                add("Library & notice folder", "ok",
                    f"Library '{sec.get('title')}'. Folder '{name}' ({how}), so Plex will look in {where}.")
        except (NoticeError, PlexError) as e:
            add("Library & notice folder", "fail", str(e))
        try:
            prefs = plex.prefs()
            val = str(prefs.get("allowMediaDeletion", "")).lower()
            if val in ("1", "true"):
                add("Allow media deletion", "ok", "Enabled, so notices are removed cleanly.")
            elif val in ("0", "false"):
                add("Allow media deletion", "warn",
                    "Off. Notices still disappear from Recently Added, but leave a hidden 'unavailable' item behind.",
                    "Plex Web → Settings → Library → tick 'Allow media deletion' → Save.")
            else:
                add("Allow media deletion", "warn", "Couldn't read this Plex setting.",
                    "Check Plex Web → Settings → Library → 'Allow media deletion' is ticked.")
        except PlexError as e:
            add("Allow media deletion", "warn", f"Couldn't read Plex settings ({e}).")
    else:
        add("Library & notice folder", "skip", "Skipped until Plex is reachable.")

    # 7. clips
    clips = config.get_media()["clips"]
    if clips:
        add("Placeholder clips", "ok", f"{len(clips)} clip(s) uploaded.")
    else:
        add("Placeholder clips", "warn", "No clips uploaded yet.", "Media → upload at least one video clip.")

    # 8. cloudflare
    if s.get("cf_enabled"):
        try:
            add("Cloudflare status page", "ok", cloudflare.test(s))
        except CloudflareError as e:
            add("Cloudflare status page", "fail", str(e))
    else:
        add("Cloudflare status page", "skip", "Not enabled (optional).")

    # 9. password
    if config.DISABLE_AUTH:
        add("Password", "warn", "Password is bypassed by DISABLE_AUTH in the compose file.",
            "Remove DISABLE_AUTH once you've regained access.")
    elif config.auth_required():
        add("Password", "ok", "A password is required to use this app.")
    else:
        add("Password", "warn", "No password set. Keep this app reachable from your LAN/VPN only.")
    return out
