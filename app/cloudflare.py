"""Writes the status-page messages into Cloudflare Workers KV.

KV keys used (the Worker reads these only when your server is unreachable):
  planned  -> JSON {title, message, until, until_display, expires, set_at}
              written when a notice is activated with a status message,
              auto-expires, deleted when the notice is deactivated.
  default  -> JSON {title, message}
              the "unexpected outage" text; written from Settings.
"""
import json
import os
import time
from datetime import datetime
from urllib.parse import quote

import requests

API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")


class CloudflareError(Exception):
    pass


def _check(s: dict):
    missing = [label for key, label in (("cf_account_id", "Account ID"),
                                        ("cf_namespace_id", "KV namespace ID"),
                                        ("cf_api_token", "API token")) if not s.get(key)]
    if missing:
        raise CloudflareError("Missing Cloudflare setting(s): " + ", ".join(missing))


def _headers(s):
    return {"Authorization": f"Bearer {s['cf_api_token']}"}


def _ns_url(s):
    return f"{API}/accounts/{s['cf_account_id']}/storage/kv/namespaces/{s['cf_namespace_id']}"


def _raise_for(r, what):
    try:
        js = r.json()
    except ValueError:
        js = {}
    if r.status_code >= 400 or (isinstance(js, dict) and js.get("success") is False):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in js.get("errors", [])) if js else ""
        hint = ""
        if r.status_code in (401, 403):
            hint = " Check the API token has 'Account → Workers KV Storage → Edit' permission for this account."
        elif r.status_code == 404:
            hint = " Check the Account ID and KV namespace ID."
        raise CloudflareError(f"Cloudflare {what} failed (HTTP {r.status_code}) {errs}.{hint}".strip())
    return js


def test(s: dict) -> str:
    _check(s)
    try:
        r = requests.get(_ns_url(s), headers=_headers(s), timeout=15)
    except requests.RequestException as e:
        raise CloudflareError(f"Can't reach the Cloudflare API: {e}")
    js = _raise_for(r, "connection test")
    title = (js.get("result") or {}).get("title", "?")
    return f"Connected. KV namespace: {title}"


def put(s: dict, key: str, value: dict, expiration: int | None = None):
    _check(s)
    params = {}
    if expiration:
        params["expiration"] = max(int(expiration), int(time.time()) + 120)
    try:
        r = requests.put(f"{_ns_url(s)}/values/{quote(key, safe='')}", params=params,
                         headers={**_headers(s), "Content-Type": "application/octet-stream"},
                         data=json.dumps(value).encode(), timeout=15)
    except requests.RequestException as e:
        raise CloudflareError(f"Can't reach the Cloudflare API: {e}")
    _raise_for(r, f"write of '{key}'")


def delete(s: dict, key: str):
    _check(s)
    try:
        r = requests.delete(f"{_ns_url(s)}/values/{quote(key, safe='')}", headers=_headers(s), timeout=15)
    except requests.RequestException as e:
        raise CloudflareError(f"Can't reach the Cloudflare API: {e}")
    if r.status_code == 404:
        return
    _raise_for(r, f"delete of '{key}'")


def push_default(s: dict):
    put(s, "default", {"title": s.get("cf_default_title", ""), "message": s.get("cf_default_message", "")})


def push_planned(s: dict, title: str, message: str, until_ts: float | None):
    now = int(time.time())
    if until_ts:
        expires = int(until_ts) + 3600            # keep it an hour past the planned end
    else:
        expires = now + int(float(s.get("cf_expiry_hours") or 24) * 3600)
    payload = {
        "title": title,
        "message": message,
        "until": int(until_ts) if until_ts else None,
        "until_display": datetime.fromtimestamp(until_ts).strftime("%a %-d %b, %-I:%M %p").replace(":00 ", " ")
        if until_ts else "",
        "expires": expires,
        "set_at": now,
    }
    put(s, "planned", payload, expiration=expires)
    return payload


def clear_planned(s: dict):
    delete(s, "planned")
