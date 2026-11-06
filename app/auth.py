"""Optional password protection: scrypt password hash + HMAC-signed session cookie."""
import hashlib
import hmac
import os
import time

from . import config, storage

COOKIE = "pn_session"
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _sign(payload: str) -> str:
    # Mixing in the password hash means changing the password logs every session out.
    key = storage.secret_key() + config.get_settings().get("auth_hash", "").encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    return f"{expires}.{_sign(str(expires))}"


def valid_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expires, sig = value.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(expires))
