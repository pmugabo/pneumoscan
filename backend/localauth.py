"""Sign-in that works without internet, for offline clinic mode.

After a user signs in online on the clinic server (Firebase password + two-factor), the
server keeps a salted PBKDF2 hash of the password in its local SQLite file (never synced to
Firestore) and issues a signed clinic session token. While the internet is down the same
email and password, plus an authenticator-app or recovery code when two-factor is on, are
checked against that hash instead of Firebase.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from . import firebase

TOKEN_TTL_S = 12 * 3600
PBKDF2_ROUNDS = 200_000


def _store():
    db = firebase.db()
    if not hasattr(db, "kv_get"):
        raise RuntimeError("offline clinic mode is not enabled")
    return db


def _key() -> bytes:
    store = _store()
    key = store.kv_get("session_key")
    if not key:
        key = secrets.token_hex(32)
        store.kv_set("session_key", key)
    return key.encode()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()


def enroll(uid: str, email: str, password: str):
    salt = secrets.token_bytes(16)
    _store().kv_set(f"cred:{email.strip().lower()}", json.dumps(
        {"uid": uid, "salt": salt.hex(), "hash": _hash(password, salt), "at": int(time.time())}))


def enrolled(email: str) -> bool:
    return bool(_store().kv_get(f"cred:{(email or '').strip().lower()}"))


def check_password(email: str, password: str) -> Optional[str]:
    raw = _store().kv_get(f"cred:{email.strip().lower()}")
    if not raw:
        return None
    rec = json.loads(raw)
    good = _hash(password, bytes.fromhex(rec["salt"]))
    return rec["uid"] if hmac.compare_digest(good, rec["hash"]) else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def issue(uid: str, mfa_ok: bool) -> tuple[str, int]:
    now = int(time.time())
    exp = now + TOKEN_TTL_S
    body = _b64(json.dumps({"u": uid, "i": now, "e": exp, "m": bool(mfa_ok)}).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def verify(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if d.get("e", 0) < time.time():
        return None
    return {"uid": d["u"], "auth_time": d["i"], "local": True, "mfa": d.get("m", False)}
