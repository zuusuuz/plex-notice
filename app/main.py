"""Web UI and HTTP routes."""
import mimetypes
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import auth, cloudflare, config, notices, poster, storage
from .cloudflare import CloudflareError
from .plex import Plex, PlexError

BASE = Path(__file__).parent
CLIP_EXTS = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".flv"]
BROWSER_PLAYABLE = {".mp4", ".m4v", ".webm", ".mov"}
IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp"]


# ---------------------------------------------------------------- startup
def _seed_default_clip():
    if (storage.DATA_DIR / "media.json").exists():
        return
    src = BASE / "assets" / "default-clip.mp4"
    media = {"clips": [], "images": []}
    if src.exists():
        cid = config.new_id()
        dest = storage.CLIPS_DIR / f"{cid}.mp4"
        shutil.copyfile(src, dest)
        media["clips"].append({"id": cid, "name": "Default placeholder (5s)", "filename": dest.name,
                               "original": "default-clip.mp4", "size": dest.stat().st_size, "uploaded": time.time()})
    config.save_media(media)


@asynccontextmanager
async def lifespan(_app):
    storage.ensure_dirs()
    notices.ensure_sentinel()
    _seed_default_clip()
    config.seed_samples()
    notices.recover_after_restart()
    notices.start_watchdog()
    config.log(f"{config.APP_NAME} {config.VERSION} started on port {config.PORT}")
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _filesize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


templates.env.filters["ts"] = notices.fmt_ts
templates.env.filters["ts_short"] = lambda t: datetime.fromtimestamp(t).strftime("%-d %b %H:%M") if t else ""
templates.env.filters["filesize"] = _filesize


# ---------------------------------------------------------------- auth
OPEN_PATHS = {"/login", "/healthz"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in OPEN_PATHS or not config.auth_required():
        return await call_next(request)
    if auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"error": "Log in first."}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


def _set_session(resp: Response, request: Request):
    resp.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not config.auth_required():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error=""))


@app.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    s = config.get_settings()
    if not verify(password, s):
        time.sleep(1)
        return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error="Wrong password."),
                                          status_code=401)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    _set_session(resp, request)
    return resp


def verify(password, s):
    return bool(s.get("auth_hash")) and auth.verify_password(password, s["auth_hash"])


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- helpers
def _ctx(request: Request, nav: str = "", **extra) -> dict:
    ctx = {
        "app_name": config.APP_NAME,
        "version": config.VERSION,
        "nav": nav,
        "auth_on": config.auth_required(),
        "msg": request.query_params.get("msg", ""),
        "msg_kind": request.query_params.get("kind", "ok"),
        "port": config.PORT,
    }
    ctx.update(extra)
    return ctx


def _redirect(url: str, msg: str = "", kind: str = "ok"):
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={quote(msg)}&kind={kind}"
    return RedirectResponse(url, status_code=303)


def _parse_local(value: str):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass
    return None


def _local_input(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M") if ts else ""


def _presets_with_flags():
    clip_ids = {c["id"] for c in config.get_media()["clips"]}
    out = []
    for p in config.get_presets():
        p["clip_ok"] = p.get("clip_id") in clip_ids
        out.append(p)
    return out


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    st = config.get_state()
    s = config.get_settings()
    active = st.get("active")
    if active:
        active = dict(active)
        active["auto_remove_local"] = _local_input(active.get("auto_remove_at"))
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, "dashboard",
        active=active,
        has_active_poster=(storage.POSTERS_DIR / notices.ACTIVE_POSTER).exists(),
        presets=_presets_with_flags(),
        job=st.get("job"),
        log=st.get("log", [])[:40],
        leftovers=st.get("leftovers", []),
        busy=notices.is_busy(),
        cf_enabled=s.get("cf_enabled"),
        last_watchdog=st.get("last_watchdog"),
        watchdog_note=st.get("watchdog_note"),
        now_local=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    ))


@app.get("/api/status")
def api_status():
    st = config.get_state()
    return {"busy": notices.is_busy(), "job": st.get("job"), "active": st.get("active"),
            "last_watchdog": st.get("last_watchdog"), "watchdog_note": st.get("watchdog_note"),
            "log": st.get("log", [])[:15]}


@app.get("/api/checks")
def api_checks():
    return notices.run_checks()


@app.post("/activate")
def activate(preset_id: str = Form(...), auto_remove: str = Form(""), push_status: str = Form("")):
    preset = config.get_preset(preset_id)
    if not preset:
        return _redirect("/", "That preset doesn't exist.", "err")
    ts = _parse_local(auto_remove)
    if auto_remove and ts is None:
        return _redirect("/", "Couldn't read the auto-remove time.", "err")
    ok, msg = notices.start_job("activate", f"Activating '{preset['name']}'", notices.activate,
                                preset_id, ts, bool(push_status))
    return _redirect("/", "" if ok else msg, "ok" if ok else "err")


@app.post("/deactivate")
def deactivate():
    ok, msg = notices.start_job("deactivate", "Removing the notice", notices.deactivate)
    return _redirect("/", "" if ok else msg, "ok" if ok else "err")


@app.post("/forget")
def forget():
    if notices.is_busy():
        return _redirect("/", "Wait for the current action to finish.", "err")
    notices.forget()
    return _redirect("/", "Forgot the active notice.")


@app.post("/cleanup")
def cleanup():
    ok, msg = notices.start_job("cleanup", "Cleaning up leftovers", notices.retry_cleanup)
    return _redirect("/", "" if ok else msg, "ok" if ok else "err")


@app.post("/active/auto-remove")
def change_auto_remove(auto_remove: str = Form("")):
    ts = _parse_local(auto_remove)
    if auto_remove and ts is None:
        return _redirect("/", "Couldn't read that time.", "err")
    if ts and ts <= time.time():
        return _redirect("/", "That time is in the past.", "err")
    st = config.get_state()
    a = st.get("active")
    if not a:
        return _redirect("/", "No notice is active.", "err")

    def fn(state):
        if state.get("active"):
            state["active"]["auto_remove_at"] = ts
    config.update_state(fn)
    msg = f"Auto-remove {'set to ' + notices.fmt_ts(ts) if ts else 'turned off'}."
    s = config.get_settings()
    if s.get("cf_enabled") and a.get("cf_planned"):
        preset = config.get_preset(a["preset_id"]) or {}
        try:
            cloudflare.push_planned(s, preset.get("status_title") or s["cf_default_title"],
                                    preset.get("status_message") or s["cf_default_message"], ts)
            msg += " Status page message updated."
        except CloudflareError as e:
            return _redirect("/", msg + f" But Cloudflare update failed: {e}", "err")
    config.log(msg)
    return _redirect("/", msg)


@app.get("/posters/active.jpg")
def active_poster():
    p = storage.POSTERS_DIR / notices.ACTIVE_POSTER
    if not p.exists():
        return Response(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- presets
def _preset_from_form(form) -> dict:
    p = {}
    for key, default in config.DEFAULT_PRESET.items():
        if key in ("updated",):
            continue
        if isinstance(default, bool):
            p[key] = form.get(key) in ("on", "true", "1")
        else:
            p[key] = (form.get(key) or "").strip() if key != "summary" else (form.get(key) or "").rstrip()
    if p.get("poster_mode") not in ("generated", "image"):
        p["poster_mode"] = "generated"
    if p.get("theme") not in config.THEMES:
        p["theme"] = "amber"
    return p


@app.get("/presets", response_class=HTMLResponse)
def presets_page(request: Request):
    a = config.get_state().get("active") or {}
    return templates.TemplateResponse(request, "presets.html", _ctx(
        request, "presets", presets=_presets_with_flags(), active_id=a.get("preset_id")))


def _edit_page(request: Request, p: dict):
    media = config.get_media()
    s = config.get_settings()
    return templates.TemplateResponse(request, "preset_edit.html", _ctx(
        request, "presets", p=p, clips=media["clips"], images=media["images"], themes=config.THEMES,
        cf_enabled=s.get("cf_enabled"), default_status_title=s.get("cf_default_title"),
        default_status_message=s.get("cf_default_message")))


@app.get("/presets/new", response_class=HTMLResponse)
def preset_new(request: Request):
    p = dict(config.DEFAULT_PRESET)
    clips = config.get_media()["clips"]
    if clips:
        p["clip_id"] = clips[0]["id"]
    return _edit_page(request, p)


@app.get("/presets/{pid}/edit", response_class=HTMLResponse)
def preset_edit(request: Request, pid: str):
    p = config.get_preset(pid)
    if not p:
        return _redirect("/presets", "Preset not found.", "err")
    return _edit_page(request, p)


@app.post("/presets/save")
async def preset_save(request: Request):
    form = await request.form()
    p = _preset_from_form(form)
    if not p["name"]:
        return _redirect("/presets", "A preset needs a name.", "err")
    saved = config.save_preset(p)
    a = config.get_state().get("active") or {}
    note = " It's live now; activate it again on the Dashboard to apply the changes." if a.get("preset_id") == saved["id"] else ""
    return _redirect("/presets", f"Saved '{saved['name']}'.{note}")


@app.post("/presets/{pid}/delete")
def preset_delete(pid: str):
    a = config.get_state().get("active") or {}
    if a.get("preset_id") == pid:
        return _redirect("/presets", "That preset is live. Remove the notice first.", "err")
    p = config.get_preset(pid)
    config.delete_preset(pid)
    return _redirect("/presets", f"Deleted '{p['name'] if p else 'preset'}'.")


@app.post("/presets/{pid}/duplicate")
def preset_duplicate(pid: str):
    p = config.get_preset(pid)
    if not p:
        return _redirect("/presets", "Preset not found.", "err")
    p = dict(p)
    p["id"] = ""
    p["name"] = f"{p['name']} (copy)"
    new = config.save_preset(p)
    return _redirect(f"/presets/{new['id']}/edit", "Copy created. Rename it and adjust what you need.")


@app.get("/presets/{pid}/poster.jpg")
def preset_poster(pid: str, kind: str = "poster"):
    p = config.get_preset(pid)
    if not p:
        return Response(status_code=404)
    return Response(poster.render(p, "art" if kind == "art" else "poster"), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@app.post("/api/preview")
async def preview(request: Request, kind: str = "poster"):
    form = await request.form()
    p = _preset_from_form(form)
    return Response(poster.render(p, "art" if kind == "art" else "poster"), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- media
@app.get("/media", response_class=HTMLResponse)
def media_page(request: Request):
    media = config.get_media()
    presets = config.get_presets()
    clips = []
    for c in media["clips"]:
        c = dict(c)
        c["ext"] = Path(c["filename"]).suffix.lower()
        c["playable"] = c["ext"] in BROWSER_PLAYABLE
        c["used_by"] = [p["name"] for p in presets if p.get("clip_id") == c["id"]]
        clips.append(c)
    images = []
    for i in media["images"]:
        i = dict(i)
        i["used_by"] = [p["name"] for p in presets if i["id"] in (p.get("image_id"), p.get("bg_image_id"))]
        images.append(i)
    return templates.TemplateResponse(request, "media.html", _ctx(
        request, "media", clips=clips, images=images, max_mb=config.MAX_UPLOAD_MB,
        clip_exts=", ".join(e.lstrip(".") for e in CLIP_EXTS), image_exts=", ".join(e.lstrip(".") for e in IMAGE_EXTS),
        clip_accept=",".join(CLIP_EXTS) + ",video/*", image_accept=",".join(IMAGE_EXTS)))


@app.post("/media/upload")
async def media_upload(kind: str = Form(...), name: str = Form(""), file: UploadFile = File(...)):
    if kind not in ("clip", "image"):
        return _redirect("/media", "Unknown upload type.", "err")
    original = Path(file.filename or "upload").name
    ext = Path(original).suffix.lower()
    allowed = CLIP_EXTS if kind == "clip" else IMAGE_EXTS
    if ext not in allowed:
        return _redirect("/media", f"'{ext or 'no extension'}' isn't accepted. Use one of: {', '.join(allowed)}", "err")
    fid = config.new_id()
    folder = storage.CLIPS_DIR if kind == "clip" else storage.IMAGES_DIR
    dest = folder / f"{fid}{ext}"
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                return _redirect("/media", f"That file is bigger than {config.MAX_UPLOAD_MB} MB (MAX_UPLOAD_MB).", "err")
            out.write(chunk)
    entry = {"id": fid, "name": (name.strip() or Path(original).stem)[:80], "filename": dest.name,
             "original": original, "size": size, "uploaded": time.time()}
    if kind == "image":
        try:
            with Image.open(dest) as im:
                im.verify()
            with Image.open(dest) as im:
                entry["width"], entry["height"] = im.size
        except Exception:
            dest.unlink(missing_ok=True)
            return _redirect("/media", "That doesn't look like a valid image.", "err")
    media = config.get_media()
    media["clips" if kind == "clip" else "images"].append(entry)
    config.save_media(media)
    config.log(f"Uploaded {kind} '{entry['name']}' ({_filesize(size)})")
    return _redirect("/media", f"Uploaded '{entry['name']}'.")


def _media_entry(kind, mid):
    return config.find_clip(mid) if kind == "clip" else config.find_image(mid) if kind == "image" else None


@app.get("/media/{kind}/{mid}/file")
def media_file(kind: str, mid: str):
    entry = _media_entry(kind, mid)
    if not entry:
        return Response(status_code=404)
    folder = storage.CLIPS_DIR if kind == "clip" else storage.IMAGES_DIR
    path = folder / entry["filename"]
    if not path.exists():
        return Response(status_code=404)
    mt = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mt)


@app.post("/media/{kind}/{mid}/delete")
def media_delete(kind: str, mid: str):
    entry = _media_entry(kind, mid)
    if not entry:
        return _redirect("/media", "Not found.", "err")
    users = [p["name"] for p in config.get_presets()
             if mid in (p.get("clip_id"), p.get("image_id"), p.get("bg_image_id"))]
    if users:
        return _redirect("/media", f"'{entry['name']}' is used by: {', '.join(users)}. Change those presets first.", "err")
    folder = storage.CLIPS_DIR if kind == "clip" else storage.IMAGES_DIR
    (folder / entry["filename"]).unlink(missing_ok=True)
    media = config.get_media()
    key = "clips" if kind == "clip" else "images"
    media[key] = [m for m in media[key] if m["id"] != mid]
    config.save_media(media)
    return _redirect("/media", f"Deleted '{entry['name']}'.")


# ---------------------------------------------------------------- settings
def _movie_sections(plex: Plex):
    out = []
    for d in plex.sections():
        if d.get("type") == "movie":
            other = "none" in str(d.get("agent", ""))
            out.append({"key": str(d.get("key")), "title": d.get("title"),
                        "kind": "Other Videos" if other else "Movies",
                        "locations": [l.get("path") for l in d.get("Location", []) or []]})
    return out


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = config.get_settings()
    sections, err = [], ""
    if s.get("plex_token"):
        try:
            sections = _movie_sections(Plex.from_settings(s, timeout=4))
        except PlexError as e:
            err = str(e)
    name, source = notices.folder_name(s)
    return templates.TemplateResponse(request, "settings.html", _ctx(
        request, "settings", s=s, sections=sections, sections_error=err, notice_dir=str(storage.NOTICE_DIR),
        detected_name=notices.detect_folder_name(), folder_name=name, folder_source=source,
        legacy_path=s.get("plex_notice_path", ""),
        has_password=bool(s.get("auth_hash")), disable_auth=config.DISABLE_AUTH))


def _settings_from_form(form, current: dict) -> dict:
    s = dict(current)
    for key in ("plex_url", "section_key", "notice_folder_name", "plex_path_override", "pin_date", "cf_account_id",
                "cf_namespace_id", "cf_default_title", "cf_default_message"):
        s[key] = (form.get(key) or "").strip()
    s["plex_url"] = s["plex_url"].rstrip("/")
    for key in ("plex_verify_ssl", "sort_to_top", "pin_release_date", "empty_trash_fallback", "cf_enabled", "auth_enabled"):
        s[key] = form.get(key) in ("on", "true", "1")
    for key, lo, hi in (("watchdog_minutes", 0, 1440), ("scan_timeout_seconds", 20, 900), ("cf_expiry_hours", 1, 720)):
        try:
            s[key] = max(lo, min(hi, int(form.get(key) or current[key])))
        except ValueError:
            pass
    for key in ("plex_token", "cf_api_token"):
        v = (form.get(key) or "").strip()
        if v:
            s[key] = v
    return s


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    current = config.get_settings()
    s = _settings_from_form(form, current)

    # validation
    try:
        pin = datetime.strptime(s["pin_date"], "%Y-%m-%d")
        if pin <= datetime.now():
            return _redirect("/settings", "The pinned date must be in the future.", "err")
        if pin.year > 2037:
            return _redirect("/settings", "Pick a pinned date in 2037 or earlier (see the note under that setting).", "err")
    except ValueError:
        return _redirect("/settings", "The pinned date isn't a valid date.", "err")
    s["notice_folder_name"] = s["notice_folder_name"].strip("/")
    if "/" in s["notice_folder_name"]:
        return _redirect("/settings#library", "'Notice folder name' is just the folder's name, without any slashes.", "err")
    s["plex_path_override"] = s["plex_path_override"].rstrip("/")
    if s["plex_path_override"] and not s["plex_path_override"].startswith("/"):
        return _redirect("/settings#library", "A custom Plex path must start with /.", "err")

    new_pw = form.get("new_password") or ""
    confirm = form.get("confirm_password") or ""
    password_changed = False
    if new_pw or confirm:
        if new_pw != confirm:
            return _redirect("/settings#security", "The two passwords don't match.", "err")
        if len(new_pw) < 6:
            return _redirect("/settings#security", "Use a password of at least 6 characters.", "err")
        s["auth_hash"] = auth.hash_password(new_pw)
        password_changed = True
    if s["auth_enabled"] and not s.get("auth_hash"):
        return _redirect("/settings#security", "Enter a password to turn password protection on.", "err")

    config.save_settings(s)
    msg = "Settings saved."
    kind = "ok"

    cf_changed = any(s.get(k) != current.get(k) for k in (
        "cf_enabled", "cf_account_id", "cf_namespace_id", "cf_api_token", "cf_default_title", "cf_default_message"))
    if s["cf_enabled"] and cf_changed:
        try:
            cloudflare.push_default(s)
            msg += " Default status message sent to Cloudflare."
        except CloudflareError as e:
            msg += f" But sending the default message to Cloudflare failed: {e}"
            kind = "err"

    resp = _redirect("/settings", msg, kind)
    if s["auth_enabled"] and (password_changed or not current.get("auth_enabled")):
        _set_session(resp, request)  # keep the person who just set it logged in
    return resp


@app.post("/settings/test-plex")
async def settings_test_plex(request: Request):
    form = await request.form()
    s = _settings_from_form(form, config.get_settings())
    try:
        plex = Plex.from_settings(s, timeout=8)
        ident = plex.identity()
        sections = _movie_sections(plex)
        if not sections:
            return {"ok": False, "message": "Connected, but Plex has no Movies or Other Videos libraries."}
        name = ""
        try:
            name = plex.server_info().get("friendlyName", "")
        except PlexError:
            pass
        return {"ok": True, "sections": sections,
                "message": f"Connected to {name or 'Plex'} (v{ident.get('version', '?')}). "
                           f"Found {len(sections)} usable librar{'y' if len(sections) == 1 else 'ies'} (Movies / Other Videos)."}
    except PlexError as e:
        return {"ok": False, "message": str(e)}


@app.post("/settings/test-cloudflare")
async def settings_test_cf(request: Request):
    form = await request.form()
    s = _settings_from_form(form, config.get_settings())
    try:
        return {"ok": True, "message": cloudflare.test(s)}
    except CloudflareError as e:
        return {"ok": False, "message": str(e)}


@app.post("/settings/push-default")
async def settings_push_default(request: Request):
    form = await request.form()
    s = _settings_from_form(form, config.get_settings())
    try:
        cloudflare.push_default(s)
        return {"ok": True, "message": "Default message sent. It can take up to a minute to appear everywhere."}
    except CloudflareError as e:
        return {"ok": False, "message": str(e)}


# ---------------------------------------------------------------- guide
WORKER_FILE = BASE / "worker" / "requests-fallback.js"


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return templates.TemplateResponse(request, "guide.html", _ctx(
        request, "guide", worker_code=WORKER_FILE.read_text("utf-8"), notice_dir=str(storage.NOTICE_DIR)))


@app.get("/guide/worker.js")
def guide_worker():
    return PlainTextResponse(WORKER_FILE.read_text("utf-8"), media_type="text/javascript",
                             headers={"Content-Disposition": 'attachment; filename="requests-fallback.js"'})
