"""Email alerts for urgent referrals.

SMTP settings are entered once by an administrator (Admin > Email settings) and stored in the
server-only Firestore document `system/email_settings`, so they survive restarts and
deployments. Environment variables (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
SMTP_FROM) override the stored settings when SMTP_HOST is set. Without either, alerts are
only written to `alert_outbox` with status "logged" (test mode). Every attempt is recorded
in the outbox for the admin delivery log.
"""
from __future__ import annotations

import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage

from . import firebase


SETTINGS_DOC = ("system", "email_settings")
_cache: dict = {}


def _stored() -> dict:
    hit = _cache.get("s")
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        snap = firebase.db().collection(SETTINGS_DOC[0]).document(SETTINGS_DOC[1]).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
    except Exception as e:
        print(f"[alerts] could not read email settings: {e}")
        data = {}
    _cache["s"] = (time.monotonic() + 60, data)
    return data


def config() -> dict:
    """Effective SMTP settings: environment variables first, then the stored admin settings."""
    if os.environ.get("SMTP_HOST"):
        return {"host": os.environ["SMTP_HOST"], "port": int(os.environ.get("SMTP_PORT", "587")),
                "user": os.environ.get("SMTP_USER", ""), "password": os.environ.get("SMTP_PASSWORD", ""),
                "sender": os.environ.get("SMTP_FROM", ""), "enabled": True, "source": "environment"}
    d = _stored()
    return {"host": d.get("host") or "smtp.gmail.com", "port": int(d.get("port") or 587),
            "user": d.get("user") or "", "password": d.get("password") or "",
            "sender": d.get("sender") or "", "enabled": bool(d.get("enabled")) and bool(d.get("user")),
            "source": "admin settings" if d else "none"}


def save_settings(changes: dict):
    firebase.db().collection(SETTINGS_DOC[0]).document(SETTINGS_DOC[1]).set(changes, merge=True)
    _cache.pop("s", None)


def stored_password_set() -> bool:
    return bool(_stored().get("password"))


def public_settings() -> dict:
    c = config()
    return {"host": c["host"], "port": c["port"], "user": c["user"], "sender": c["sender"],
            "enabled": c["enabled"], "password_set": bool(c["password"]), "source": c["source"],
            "updated_at": _stored().get("updated_at")}


def email_configured() -> bool:
    return config()["enabled"]



def mask(address: str) -> str:
    if "@" in address:
        name, _, domain = address.partition("@")
        return f"{name[:2]}***@{domain}"
    return f"***{address[-3:]}" if len(address) > 3 else "***"


def _send_email(to: str, subject: str, body: str):
    c = config()
    msg = EmailMessage()
    addr = c["user"] or "pneumoscan@localhost"
    msg["From"] = f"{c['sender']} <{addr}>" if c["sender"] and "@" not in c["sender"] else (c["sender"] or addr)
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body + "\n\n--\nPneumoScan research prototype. Do not reply to this message.")
    if c["port"] == 465:
        server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=15)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=15)
    local = c["host"] in ("localhost", "127.0.0.1") and c["port"] in (25, 1025)
    with server as s:
        if c["port"] != 465 and not local:
            s.starttls()
        if c["user"] and not local:
            s.login(c["user"], c["password"].replace(" ", ""))
        s.send_message(msg)



def _deliver(to: str, subject: str, body: str, user_id: str, link: str) -> dict:
    live = email_configured()
    status, error = ("logged", None)
    if live:
        try:
            _send_email(to, subject, body)
            status = "sent"
        except Exception as e:
            status, error = "failed", str(e)[:300]
    try:
        firebase.db().collection("alert_outbox").add({
            "channel": "email", "to": mask(to), "user_id": user_id, "subject": subject,
            "body": body, "link": link, "status": status, "error": error,
            "provider": "smtp" if live else "test-mode",
            "created_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f"[alerts] could not record outbox entry: {e}")
    return {"status": status, "error": error}


def alert_user(user: dict, subject: str, body: str, link: str = "", wait: bool = False):
    """Email a user if they have email alerts on. Runs in the background."""
    if not (user.get("email") and user.get("alert_email", True)):
        return []
    t = threading.Thread(target=_deliver, daemon=True,
                         args=(user["email"], subject, body, user.get("user_id"), link))
    t.start()
    if wait:
        t.join(20)
    return ["email"]


def send_now(user: dict, subject: str, body: str) -> dict:
    """Send immediately and report the real outcome: sent, failed or logged (test mode)."""
    return _deliver(user["email"], subject, body, user.get("user_id"), "")


def status() -> dict:
    return {"email": "smtp" if email_configured() else "test-mode"}
