"""Container entrypoint, LinuxServer-style PUID/PGID handling.

Docker creates missing host folders for bind mounts as root before the container
starts. When the container starts as root, this script:
  1. hands /data (recursively) to PUID:PGID,
  2. hands the top-level /notice folder (only that folder, never anything else in
     your Movies library) to PUID:PGID,
  3. drops from root to PUID:PGID and starts the web app.
If the container was started with a compose `user:` line instead, it skips the
fixes and just starts the app as that user.
"""
import os
import sys
from pathlib import Path


def _say(msg: str) -> None:
    print(f"[init] {msg}", flush=True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        _say(f"{name} must be a number; using {default}")
        return default


def _fix_tree(root: Path, uid: int, gid: int) -> int:
    changed = 0
    for path in [root, *root.rglob("*")]:
        try:
            st = path.lstat()
            if st.st_uid != uid or st.st_gid != gid:
                os.lchown(path, uid, gid)
                changed += 1
        except OSError as e:
            _say(f"couldn't change owner of {path}: {e.strerror}")
    return changed


def main() -> None:
    data = Path(os.environ.get("DATA_DIR", "/data"))
    notice = Path(os.environ.get("NOTICE_DIR", "/notice"))
    port = os.environ.get("PORT", "8742")
    cmd = sys.argv[1:] or [
        "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
        "--proxy-headers", "--forwarded-allow-ips", "*",
    ]

    os.umask(0o002)  # new files are group-writable and readable by Plex

    if os.getuid() == 0:
        puid = _int_env("PUID", 1000)
        pgid = _int_env("PGID", 1000)
        _say(f"User UID: {puid}  User GID: {pgid}")
        if puid == 0:
            _say("PUID=0 runs the app as root. That works, but isn't recommended.")

        data.mkdir(parents=True, exist_ok=True)
        n = _fix_tree(data, puid, pgid)
        if n:
            _say(f"set owner of {n} item(s) in {data} to {puid}:{pgid}")

        if notice.is_dir():
            keep = notice / ".plex-notice-keep"
            try:
                if not keep.exists():
                    keep.write_text("Created by Plex Notice. It keeps this folder from being removed when Plex "
                                    "deletes a notice\nand tidies up empty folders. Plex ignores hidden files.\n")
                os.chown(keep, puid, pgid)
            except OSError:
                pass
            st = notice.stat()
            if st.st_uid != puid or st.st_gid != pgid:
                try:
                    os.chown(notice, puid, pgid)
                    _say(f"set owner of {notice} (that folder only) to {puid}:{pgid}")
                except OSError as e:
                    _say(f"couldn't change owner of {notice}: {e.strerror}. Fix it on the host with "
                         f"chown {puid}:{pgid} <that folder>.")
        else:
            _say(f"{notice} isn't mounted. Add the notice folder volume in compose.")

        if puid != 0:
            os.setgroups([])
            os.setgid(pgid)
            os.setuid(puid)
        os.environ["HOME"] = "/tmp"
    else:
        _say(f"Started as UID {os.getuid()} (compose 'user:'), so PUID/PGID and the automatic "
             "folder ownership fixes are skipped.")

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
