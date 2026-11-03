"""Time-based one-time passwords (RFC 6238) for two-factor sign-in.

Secrets live in the server-only `mfa` Firestore collection. After a correct code the
browser receives a short-lived signed token bound to the user and to the Firebase sign-in
(`auth_time`), so a new password sign-in always needs a new code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse
from typing import Optional

from . import firebase

ISSUER = "PneumoScan"
TOKEN_TTL_S = 12 * 3600
_key_cache: dict = {}


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, at: Optional[float] = None, step: int = 30) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def check(secret: str, code: str, window: int = 1) -> bool:
    code = (code or "").replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    now = time.time()
    return any(hmac.compare_digest(totp(secret, now + i * 30), code) for i in range(-window, window + 1))


def otpauth_uri(secret: str, account: str) -> str:
    label = urllib.parse.quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&algorithm=SHA1&digits=6&period=30"


def qr_svg(uri: str) -> Optional[str]:
    try:
        import segno
    except ImportError:
        return None
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#0f172a")


def recovery_codes(n: int = 8) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper() for _ in range(n)]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _signing_key() -> bytes:
    if "k" not in _key_cache:
        env = os.environ.get("MFA_SIGNING_KEY")
        if env:
            _key_cache["k"] = env.encode()
        else:
            ref = firebase.db().collection("system").document("mfa_signing_key")
            snap = ref.get()
            key = (snap.to_dict() or {}).get("key") if snap.exists else None
            if not key:
                key = secrets.token_hex(32)
                ref.set({"key": key})
            _key_cache["k"] = key.encode()
    return _key_cache["k"]


def issue_token(uid: str, auth_time) -> tuple[str, int]:
    exp = int(time.time()) + TOKEN_TTL_S
    payload = f"{auth_time}.{exp}"
    sig = hmac.new(_signing_key(), f"{uid}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", exp


def token_valid(token: Optional[str], uid: str, auth_time) -> bool:
    try:
        t_auth, exp, sig = (token or "").split(".")
    except ValueError:
        return False
    if str(t_auth) != str(auth_time) or int(exp) < time.time():
        return False
    good = hmac.new(_signing_key(), f"{uid}.{t_auth}.{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)
