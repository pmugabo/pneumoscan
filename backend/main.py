"""PneumoScan API.

    uvicorn backend.main:app --reload

Swagger UI at http://127.0.0.1:8000/docs, the clinician interface at /.

Users sign in with Firebase Authentication in the browser and send the Firebase
ID token as `Authorization: Bearer <token>`. All records live in Firestore:
users, patients, predictions, escalations and audit_logs (see backend/firebase.py).

Endpoints
    GET  /api/health           service and model status
    GET  /api/model-info       architecture, gate thresholds, triage states
    GET  /api/firebase-config  public Firebase web config for the browser
    GET  /api/auth/me          profile of the signed-in user
    POST /api/auth/register    create the profile for a newly signed-up user
    POST /api/predict          upload an image, get a triage decision
    POST /api/feedback         record the confirmed outcome for a case
    GET  /api/cases            decision history
    GET  /api/stats            autonomy rate
    GET  /api/admin/*          admin-only stats, users and audit log
    GET  /api/report/{id}      PDF report
"""
from __future__ import annotations

import base64
import io
import json
import secrets
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from firebase_admin import auth as fb_auth
from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter, Query
from PIL import Image
from pydantic import BaseModel

from . import alerts, firebase, mfa
from .inference import engine
from .report import generate_report

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/jpeg", "image/png"}
STORED_IMAGE_MAX_PX = 768
ROLES = ("clinician", "specialist", "admin")
STATES = ("CLEARED", "FLAGGED_URGENT", "ESCALATED", "REJECTED")
PRIORITIES = ("routine", "urgent")
DANGER_SIGNS = ("chest_indrawing", "unable_to_feed", "lethargy", "convulsions",
                "stridor", "cyanosis", "grunting", "vomiting_everything")
MAX_MESSAGE_CHARS = 2000
OUTCOME_BASIS = ("specialist_review", "clinical_exam", "laboratory", "treatment_response",
                 "repeat_imaging", "clinical_course")
MFA_ROLES = ("specialist", "admin")
LANGUAGES = ("en", "fr", "rw")
CONSENT_VERSION = "2026-10-v1"
CONSENT_METHODS = ("verbal", "written")
URGENT_SLA_MIN = int(os.environ.get("URGENT_REFERRAL_SLA_MINUTES", "30"))
ROUTINE_SLA_MIN = int(os.environ.get("ROUTINE_REFERRAL_SLA_HOURS", "24")) * 60
EXAM_FINDINGS = ("crackles", "bronchial_breathing", "reduced_breath_sounds", "dullness",
                 "wheeze", "clear_chest", "fast_breathing", "chest_indrawing")

app = FastAPI(
    title="PneumoScan API",
    description=("Autonomous chest X-ray triage with a confidence gate. "
                 "Disclaimer: PneumoScan is an exploratory research tool for screening paediatric chest X-rays. Its output is not a diagnosis and has not been clinically validated. Results must be reviewed and confirmed by a qualified clinician or radiologist before any clinical decision."),
    version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def col(name: str):
    try:
        return firebase.db().collection(name)
    except RuntimeError:
        raise HTTPException(503, "Firebase is not configured on the server.")


def doc_dict(snap, id_field: str) -> dict:
    return {id_field: snap.id, **(snap.to_dict() or {})}


def get_many(collection: str, ids, id_field: str, fields: Optional[list] = None) -> dict:
    """Fetch documents by id in one round trip; returns {id: dict}. `fields` limits the
    download to those fields (prediction documents carry large images)."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    c = col(collection)
    snaps = firebase.db().get_all([c.document(i) for i in ids], field_paths=fields)
    return {s.id: doc_dict(s, id_field) for s in snaps if s.exists}


def parallel(*calls):
    """Run independent Firestore round trips concurrently; each call is (fn, *args)."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(fn, *args) for fn, *args in calls]
        return [f.result() for f in futures]


PRED_LIST_FIELDS = ["patient_id", "filename", "created_at", "calibrated_prob", "ood_score",
                    "triage_state", "model_version", "confirmed_outcome", "comment",
                    "user_id", "clinical", "outcome_record"]


def log(prediction_id: Optional[str], action: str, user_id: Optional[str] = None):
    col("audit_logs").add({"prediction_id": prediction_id, "action": action,
                           "timestamp": now(), "user_id": user_id})


def notify(user_id: Optional[str], title: str, body: str, link: str = ""):
    if user_id:
        col("notifications").add({"user_id": user_id, "title": title, "body": body,
                                  "link": link, "created_at": now(), "read": False})


def ai_label(pred: dict) -> Optional[str]:
    """What the model leaned towards: decided cases use the state, escalated ones the probability."""
    state = pred.get("triage_state")
    if state == "FLAGGED_URGENT":
        return "pneumonia"
    if state == "CLEARED":
        return "normal"
    if state == "ESCALATED" and pred.get("calibrated_prob") is not None:
        return "pneumonia" if pred["calibrated_prob"] >= 0.5 else "normal"
    return None


def hours_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def to_stored_image(img: Image.Image) -> str:
    """Downscaled JPEG data URL; keeps prediction documents under Firestore's 1 MB limit."""
    im = img.convert("RGB")
    im.thumbnail((STORED_IMAGE_MAX_PX, STORED_IMAGE_MAX_PX))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ auth
def _decode(authorization: Optional[str]) -> dict:
    if not firebase.configured():
        raise HTTPException(503, "Firebase is not configured on the server.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required.")
    try:
        return firebase.verify_id_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(401, "Invalid or expired session. Sign in again.")


# Firestore round trips are slow from far regions; cache profiles briefly so each API
# call does not re-read users/{uid}. Writes through this API call forget_profile().
PROFILE_TTL_S = 60
_profiles: dict = {}


def forget_profile(uid: str):
    _profiles.pop(uid, None)


def _load_profile(claims: dict) -> dict:
    uid = claims["uid"]
    cached = _profiles.get(uid)
    if cached and cached[0] > time.monotonic():
        user = cached[1]
    else:
        snap = col("users").document(uid).get()
        if not snap.exists:
            raise HTTPException(403, "No PneumoScan profile for this account.")
        user = doc_dict(snap, "user_id")
        _profiles[uid] = (time.monotonic() + PROFILE_TTL_S, user)
    if not user.get("is_active", True):
        raise HTTPException(403, "User account is deactivated.")
    return user


def mfa_required(user: dict) -> bool:
    return bool(user.get("mfa_enabled")) and user.get("role") in MFA_ROLES


def _mfa_ok(user: dict, claims: dict, token: Optional[str]) -> bool:
    return not mfa_required(user) or mfa.token_valid(token, user["user_id"], claims.get("auth_time"))


def current_user(authorization: Optional[str] = Header(None),
                 x_mfa_token: Optional[str] = Header(None)) -> dict:
    claims = _decode(authorization)
    user = _load_profile(claims)
    if not _mfa_ok(user, claims, x_mfa_token):
        raise HTTPException(401, "Two-factor verification required.")
    return user


def session_user(authorization: Optional[str] = Header(None),
                 x_mfa_token: Optional[str] = Header(None)) -> tuple[dict, dict, bool]:
    """Signed-in user before the two-factor check (for /me and code verification)."""
    claims = _decode(authorization)
    user = _load_profile(claims)
    return user, claims, _mfa_ok(user, claims, x_mfa_token)


def require(*roles: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "You do not have access to this action.")
        return user
    return dep


# ------------------------------------------------------------------ models
class Feedback(BaseModel):
    prediction_id: str
    confirmed_outcome: str   # "pneumonia" | "normal" | "unknown"
    comment: str = ""
    basis: list[str] = []            # OUTCOME_BASIS keys the decision rests on
    exam_findings: list[str] = []    # EXAM_FINDINGS keys
    crp_mg_l: Optional[float] = None
    wbc_10e9_l: Optional[float] = None
    certainty: Optional[str] = None  # "confirmed" | "probable"
    treatment: Optional[str] = None
    follow_up_date: Optional[str] = None


class UserRegister(BaseModel):
    full_name: str
    hospital: Optional[str] = None
    language: Optional[str] = None


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "clinician"
    hospital: Optional[str] = None


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    hospital: Optional[str] = None
    language: Optional[str] = None
    alert_email: Optional[bool] = None


class PatientCreate(BaseModel):
    full_name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = ""
    guardian_name: Optional[str] = None
    contact: Optional[str] = None
    consent_given: bool = False
    consent_by: Optional[str] = None
    consent_relationship: Optional[str] = None
    consent_method: Optional[str] = "verbal"
    hide_identity: bool = True


class ConsentUpdate(BaseModel):
    given: bool
    by: Optional[str] = None
    relationship: Optional[str] = None
    method: Optional[str] = "verbal"
    reason: Optional[str] = None


class Reassign(BaseModel):
    specialist_id: str
    reason: Optional[str] = ""


class MfaCode(BaseModel):
    code: str


class PatientUpdate(BaseModel):
    full_name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = None
    guardian_name: Optional[str] = None
    contact: Optional[str] = None
    hide_identity: Optional[bool] = None


class AdminUserUpdate(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    available: Optional[bool] = None
    full_name: Optional[str] = None
    hospital: Optional[str] = None
    reset_mfa: Optional[bool] = None


class Availability(BaseModel):
    available: bool


class EscalationCreate(BaseModel):
    prediction_id: str
    specialist_id: str
    note: Optional[str] = ""
    priority: str = "routine"


class MessageCreate(BaseModel):
    text: str


class EmailSettings(BaseModel):
    user: str
    password: Optional[str] = None     # blank keeps the stored password
    sender: Optional[str] = "PneumoScan"
    host: str = "smtp.gmail.com"
    port: int = 587
    enabled: bool = True


class Announcement(BaseModel):
    title: str
    body: str
    audience: str = "all"


class EscalationResolve(BaseModel):
    specialist_comment: str
    confirmed_outcome: Optional[str] = None
    findings: Optional[str] = None
    recommendation: Optional[str] = None


# ------------------------------------------------------------------ public
@app.on_event("startup")
def warm_up_firestore():
    """Open the Firestore connection in the background so the first sign-in is not slow."""
    def ping():
        try:
            if firebase.configured():
                col("users").limit(1).get()
        except Exception as e:
            print(f"[firebase] warm-up failed: {e}")
    threading.Thread(target=ping, daemon=True).start()


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": engine.demo,
            "firebase": firebase.configured(), "time": now()}


@app.get("/api/model-info")
def model_info():
    return engine.info()


@app.get("/api/firebase-config")
def firebase_config():
    cfg = firebase.web_config()
    if cfg is None:
        raise HTTPException(503, "Firebase web config is not set on the server.")
    return cfg


# ------------------------------------------------------------------ auth endpoints
@app.get("/api/auth/me")
def me(session: tuple = Depends(session_user)):
    user, _, verified = session
    return {**user, "mfa_required": mfa_required(user), "mfa_verified": verified,
            "mfa_email_available": alerts.email_configured()}


_mfa_failures: dict = {}


def _mfa_attempt_guard(uid: str):
    recent = [t for t in _mfa_failures.get(uid, []) if t > time.monotonic() - 600]
    _mfa_failures[uid] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many incorrect codes. Wait 10 minutes and try again.")


def _mfa_fail(uid: str):
    _mfa_failures.setdefault(uid, []).append(time.monotonic())
    log(None, "mfa_failed", uid)
    raise HTTPException(400, "That code is not valid. Check your authenticator app and try again.")


def _check_mfa_code(uid: str, code: str) -> tuple[bool, bool]:
    """(valid, used_recovery_code). A recovery code works once."""
    ref = col("mfa").document(uid)
    d = (ref.get().to_dict() or {})
    if d.get("secret") and mfa.check(d["secret"], code):
        return True, False
    if d.get("email_code_hash") and (d.get("email_code_exp") or 0) > time.time() \
            and mfa.hash_code((code or "").replace(" ", "")) == d["email_code_hash"]:
        ref.update({"email_code_hash": None, "email_code_exp": None})
        return True, False
    h = mfa.hash_code(code)
    if h in d.get("recovery_hashes", []):
        ref.update({"recovery_hashes": firestore.ArrayRemove([h])})
        return True, True
    return False, False


@app.post("/api/auth/mfa/verify")
def mfa_verify(body: MfaCode, session: tuple = Depends(session_user)):
    user, claims, _ = session
    uid = user["user_id"]
    if not mfa_required(user):
        raise HTTPException(400, "Two-factor sign-in is not turned on for this account.")
    _mfa_attempt_guard(uid)
    ok, recovery = _check_mfa_code(uid, body.code)
    if not ok:
        _mfa_fail(uid)
    token, exp = mfa.issue_token(uid, claims.get("auth_time"))
    log(None, "mfa_verified" + (":recovery_code" if recovery else ""), uid)
    return {"mfa_token": token, "expires_at": exp, "used_recovery_code": recovery}


EMAIL_CODE_TTL_S = 600
_email_code_sent: dict = {}


def _email_code(uid: str, email: str, field: str, purpose: str) -> str:
    """Create a 6-digit code, store its hash and email it. Raises if email cannot be sent."""
    if not alerts.email_configured():
        raise HTTPException(400, "Email is not set up yet. Ask your administrator to complete Admin > Email settings.")
    last = _email_code_sent.get(uid, 0)
    if time.monotonic() - last < 30:
        raise HTTPException(429, "A code was just sent. Wait 30 seconds before asking for another.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    col("mfa").document(uid).set({f"{field}_hash": mfa.hash_code(code),
                                   f"{field}_exp": time.time() + EMAIL_CODE_TTL_S}, merge=True)
    try:
        alerts._send_email(email, f"PneumoScan verification code: {code}",
                           f"Your PneumoScan {purpose} code is {code}. It expires in 10 minutes.\n"
                           "If you did not try to sign in, change your password and tell your administrator.")
    except Exception as e:
        raise HTTPException(502, f"Could not send the code by email: {str(e)[:200]}")
    _email_code_sent[uid] = time.monotonic()
    log(None, f"mfa_email_code_sent:{purpose}", uid)
    return alerts.mask(email)


@app.post("/api/auth/mfa/email/send")
def mfa_email_send(session: tuple = Depends(session_user)):
    user, _, _ = session
    if not mfa_required(user):
        raise HTTPException(400, "Two-factor sign-in is not turned on for this account.")
    return {"sent_to": _email_code(user["user_id"], user["email"], "email_code", "sign-in")}


@app.post("/api/auth/mfa/email/setup")
def mfa_email_setup(user: dict = Depends(require(*MFA_ROLES))):
    if user.get("mfa_enabled"):
        raise HTTPException(400, "Two-factor sign-in is already on.")
    return {"sent_to": _email_code(user["user_id"], user["email"], "pending_email_code", "setup")}


@app.post("/api/auth/mfa/email/enable")
def mfa_email_enable(body: MfaCode, authorization: Optional[str] = Header(None),
                     user: dict = Depends(require(*MFA_ROLES))):
    uid = user["user_id"]
    _mfa_attempt_guard(uid)
    ref = col("mfa").document(uid)
    d = ref.get().to_dict() or {}
    if not (d.get("pending_email_code_hash") and (d.get("pending_email_code_exp") or 0) > time.time()):
        raise HTTPException(400, "The code has expired. Send a new one.")
    if mfa.hash_code(body.code.replace(" ", "")) != d["pending_email_code_hash"]:
        _mfa_fail(uid)
    codes = mfa.recovery_codes()
    ref.set({"method": "email", "secret": None, "enabled_at": now(),
             "recovery_hashes": [mfa.hash_code(c) for c in codes]})
    col("users").document(uid).update({"mfa_enabled": True, "mfa_method": "email"})
    forget_profile(uid)
    log(None, "mfa_enabled:email", uid)
    token, exp = mfa.issue_token(uid, _decode(authorization).get("auth_time"))
    return {"enabled": True, "recovery_codes": codes, "mfa_token": token, "expires_at": exp}


@app.post("/api/auth/mfa/setup")
def mfa_setup(user: dict = Depends(require(*MFA_ROLES))):
    if user.get("mfa_enabled"):
        raise HTTPException(400, "Two-factor sign-in is already on.")
    secret = mfa.new_secret()
    col("mfa").document(user["user_id"]).set({"pending_secret": secret, "pending_at": now()}, merge=True)
    uri = mfa.otpauth_uri(secret, user.get("email") or user["user_id"])
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": mfa.qr_svg(uri)}


@app.post("/api/auth/mfa/enable")
def mfa_enable(body: MfaCode, authorization: Optional[str] = Header(None),
               user: dict = Depends(require(*MFA_ROLES))):
    uid = user["user_id"]
    _mfa_attempt_guard(uid)
    ref = col("mfa").document(uid)
    pending = (ref.get().to_dict() or {}).get("pending_secret")
    if not pending:
        raise HTTPException(400, "Start two-factor setup again.")
    if not mfa.check(pending, body.code):
        _mfa_fail(uid)
    codes = mfa.recovery_codes()
    ref.set({"secret": pending, "pending_secret": None, "enabled_at": now(),
             "recovery_hashes": [mfa.hash_code(c) for c in codes]})
    col("users").document(uid).update({"mfa_enabled": True, "mfa_method": "app"})
    forget_profile(uid)
    log(None, "mfa_enabled", uid)
    token, exp = mfa.issue_token(uid, _decode(authorization).get("auth_time"))
    return {"enabled": True, "recovery_codes": codes, "mfa_token": token, "expires_at": exp}


@app.post("/api/auth/mfa/disable")
def mfa_disable(body: MfaCode, user: dict = Depends(require(*MFA_ROLES))):
    uid = user["user_id"]
    if not user.get("mfa_enabled"):
        return {"enabled": False}
    _mfa_attempt_guard(uid)
    if not _check_mfa_code(uid, body.code)[0]:
        _mfa_fail(uid)
    col("mfa").document(uid).delete()
    col("users").document(uid).update({"mfa_enabled": False})
    forget_profile(uid)
    log(None, "mfa_disabled", uid)
    return {"enabled": False}


@app.post("/api/auth/register")
def register(body: UserRegister, authorization: Optional[str] = Header(None)):
    """Create the profile for an account just made with Firebase Auth. Always a clinician;
    admins assign other roles."""
    claims = _decode(authorization)
    ref = col("users").document(claims["uid"])
    snap = ref.get()
    if snap.exists:
        return doc_dict(snap, "user_id")
    profile = {"email": claims.get("email"), "full_name": body.full_name, "role": "clinician",
               "hospital": body.hospital, "created_at": now(), "is_active": True,
               "language": body.language if body.language in LANGUAGES else "en"}
    ref.set(profile)
    forget_profile(claims["uid"])
    return {"user_id": claims["uid"], **profile}


@app.patch("/api/users/me/profile")
def update_profile(update: ProfileUpdate, user: dict = Depends(current_user)):
    changes = {}
    if update.full_name:
        changes["full_name"] = update.full_name
    if update.hospital is not None:
        changes["hospital"] = update.hospital
    if update.language is not None:
        if update.language not in LANGUAGES:
            raise HTTPException(400, "Language must be en, fr or rw.")
        changes["language"] = update.language
    if update.alert_email is not None:
        changes["alert_email"] = update.alert_email
    if changes:
        col("users").document(user["user_id"]).update(changes)
        forget_profile(user["user_id"])
    merged = {**user, **changes}
    return {"updated": True, **{k: merged.get(k) for k in (
        "full_name", "hospital", "language", "alert_email")}}


@app.post("/api/users/me/test-alert")
def test_alert(user: dict = Depends(current_user)):
    if not (user.get("email") and user.get("alert_email", True)):
        raise HTTPException(400, "Turn on email alerts first.")
    result = alerts.send_now(user, "PneumoScan test alert", "This is a test of your PneumoScan referral alerts.")
    return {"channels": ["email"], **alerts.status(), **result}


@app.patch("/api/users/me/availability")
def set_availability(body: Availability, user: dict = Depends(require("specialist"))):
    col("users").document(user["user_id"]).update({"available": body.available})
    forget_profile(user["user_id"])
    log(None, f"availability:{'on' if body.available else 'off'}", user["user_id"])
    return {"available": body.available}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


@app.get("/api/model-card")
def model_card():
    """Model details and held-out test results, for the landing page and admin monitoring."""
    return {**engine.info(), "training": _read_json(ROOT / "models" / "gate_params.json"),
            "test_results": _read_json(ROOT / "results" / "gating_results.json")}


# ------------------------------------------------------------------ notifications
@app.get("/api/notifications")
def notifications(user: dict = Depends(current_user)):
    q = col("notifications").where(filter=FieldFilter("user_id", "==", user["user_id"]))
    notes, convs = parallel((lambda: [doc_dict(s, "notification_id") for s in q.stream()],),
                            (_conversations, user))
    rows = sorted(notes, key=lambda r: r.get("created_at") or "", reverse=True)
    return {"unread": sum(1 for r in rows if not r.get("read")), "notifications": rows[:30],
            "unread_messages": sum(c["unread"] for c in convs)}


@app.post("/api/notifications/read")
def mark_notifications_read(user: dict = Depends(current_user)):
    q = col("notifications").where(filter=FieldFilter("user_id", "==", user["user_id"]))
    batch = firebase.db().batch()
    n = 0
    for s in q.stream():
        if not (s.to_dict() or {}).get("read"):
            batch.update(s.reference, {"read": True})
            n += 1
    if n:
        batch.commit()
    return {"marked": n}


# ------------------------------------------------------------------ predictions
@app.post("/api/predict")
async def predict(file: UploadFile = File(...), patient_id: Optional[str] = None,
                  respiratory_rate: Optional[int] = Form(None), spo2: Optional[int] = Form(None),
                  temperature_c: Optional[float] = Form(None), danger_signs: str = Form(""),
                  user: dict = Depends(require("clinician", "admin"))):
    if file.content_type not in ALLOWED:
        raise HTTPException(400, "Upload a JPEG or PNG image.")
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(400, "File larger than 10 MB.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "That file could not be read as an image.")

    if patient_id:
        psnap = col("patients").document(patient_id).get()
        if not psnap.exists:
            raise HTTPException(404, "Patient not found")
        if not ((psnap.to_dict() or {}).get("consent") or {}).get("given"):
            raise HTTPException(400, "No valid guardian consent is recorded for this patient. "
                                     "Record consent on the patient page before analysing a radiograph.")
    signs = [x for x in (d.strip() for d in danger_signs.split(",")) if x in DANGER_SIGNS]
    clinical = {"respiratory_rate": respiratory_rate, "spo2": spo2,
                "temperature_c": temperature_c, "danger_signs": signs}
    result = engine.predict(img)
    pid = str(uuid.uuid4())
    col("predictions").document(pid).set({
        "patient_id": patient_id, "filename": file.filename, "created_at": now(),
        "calibrated_prob": result.probability, "ood_score": result.ood_score,
        "triage_state": result.state, "model_version": result.model_version,
        "model_sha256": engine.weights_sha256, "gate_sha256": engine.gate_sha256,
        "thresholds": {k: v for k, v in (result.thresholds or {}).items()
                       if isinstance(v, (int, float)) or v is None},
        "confirmed_outcome": None, "comment": None,
        "heatmap_png": result.heatmap_png, "image_png": to_stored_image(img),
        "user_id": user["user_id"], "clinical": clinical})
    log(pid, f"triage:{result.state}", user["user_id"])
    return {"prediction_id": pid, "patient_id": patient_id, "clinical": clinical,
            **result.to_dict()}


@app.post("/api/feedback")
def feedback(fb: Feedback, user: dict = Depends(current_user)):
    """A final diagnosis must say what it rests on; disagreeing with the specialist needs a reason."""
    ref = col("predictions").document(fb.prediction_id)
    snap, escs = parallel((ref.get,), (lambda: [doc_dict(s, "escalation_id") for s in
                          col("escalations").where(filter=FieldFilter(
                              "prediction_id", "==", fb.prediction_id)).stream()],))
    if not snap.exists:
        raise HTTPException(404, "Unknown prediction_id.")
    if fb.confirmed_outcome not in ("pneumonia", "normal", "unknown"):
        raise HTTPException(400, "Outcome must be pneumonia, normal or unknown")
    basis = [b for b in fb.basis if b in OUTCOME_BASIS]
    findings = [f for f in fb.exam_findings if f in EXAM_FINDINGS]
    comment = (fb.comment or "").strip()
    reviewed = next((e for e in sorted(escs, key=lambda e: e.get("resolved_at") or "",
                                       reverse=True)
                     if e.get("status") == "resolved"), None)
    spec_dx = reviewed.get("confirmed_outcome") if reviewed else None
    if fb.confirmed_outcome != "unknown":
        if not basis:
            raise HTTPException(400, "Select what this diagnosis is based on.")
        if "clinical_exam" in basis and not findings:
            raise HTTPException(400, "Record the examination findings.")
        if "laboratory" in basis and fb.crp_mg_l is None and fb.wbc_10e9_l is None:
            raise HTTPException(400, "Enter at least one laboratory result.")
        if fb.certainty not in ("confirmed", "probable"):
            raise HTTPException(400, "State whether the diagnosis is confirmed or probable.")
        if spec_dx in ("pneumonia", "normal") and spec_dx != fb.confirmed_outcome and not comment:
            raise HTTPException(400, "Your diagnosis differs from the specialist's; "
                                     "explain why in the rationale.")
    record = {"outcome": fb.confirmed_outcome, "basis": basis, "exam_findings": findings,
              "crp_mg_l": fb.crp_mg_l, "wbc_10e9_l": fb.wbc_10e9_l,
              "certainty": fb.certainty if fb.confirmed_outcome != "unknown" else None,
              "treatment": (fb.treatment or "").strip() or None,
              "follow_up_date": fb.follow_up_date, "rationale": comment or None,
              "specialist_outcome": spec_dx,
              "agrees_with_specialist": (spec_dx == fb.confirmed_outcome)
              if spec_dx in ("pneumonia", "normal") and fb.confirmed_outcome != "unknown"
              else None,
              "ai_label": ai_label(snap.to_dict() or {}),
              "recorded_by": user["user_id"], "recorded_by_name": user["full_name"],
              "recorded_at": now()}
    ref.update({"confirmed_outcome": fb.confirmed_outcome, "comment": comment,
                "outcome_record": record})
    log(fb.prediction_id, f"feedback:{fb.confirmed_outcome}", user["user_id"])
    return {"recorded": True, "outcome_record": record}


def _escalations_for(field: str, uid: str) -> list:
    q = col("escalations").where(filter=FieldFilter(field, "==", uid))
    return [doc_dict(s, "escalation_id") for s in q.stream()]


@app.get("/api/cases")
def cases(limit: int = 25, mine: bool = False, user: dict = Depends(current_user)):
    """Specialists see only cases sent to them; everyone else sees all cases, or only their
    own with mine=true."""
    if mine and user["role"] != "specialist":
        q = col("predictions").where(filter=FieldFilter("user_id", "==", user["user_id"]))
        rows = [doc_dict(s, "prediction_id") for s in q.select(PRED_LIST_FIELDS).stream()]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        rows = rows[:limit]
    elif user["role"] == "specialist":
        ids = [e["prediction_id"] for e in _escalations_for("assigned_to", user["user_id"])]
        rows = list(get_many("predictions", ids, "prediction_id", PRED_LIST_FIELDS).values())
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        rows = rows[:limit]
    else:
        q = col("predictions").order_by("created_at", direction=Query.DESCENDING).limit(limit)
        rows = [doc_dict(s, "prediction_id") for s in q.select(PRED_LIST_FIELDS).stream()]
    patients, users = parallel(
        (get_many, "patients", [r.get("patient_id") for r in rows], "patient_id"),
        (get_many, "users", [r.get("user_id") for r in rows], "user_id"))
    for r in rows:
        r.pop("image_png", None)
        r.pop("heatmap_png", None)
        p = patients.get(r.get("patient_id"))
        r["patient_name"] = p["full_name"] if p else None
        r["pid"] = r.get("patient_id")
        u = users.get(r.get("user_id"))
        r["clinician_name"] = u["full_name"] if u else None
        r["ai_label"] = ai_label(r)
    return {"count": len(rows), "cases": rows}


def _visible_escalations(prediction_id: str, user: dict) -> list:
    """Escalations for a prediction; 404 if a specialist has none assigned to them."""
    escs = [doc_dict(s, "escalation_id") for s in col("escalations").where(
        filter=FieldFilter("prediction_id", "==", prediction_id)).stream()]
    if user["role"] == "specialist":
        escs = [e for e in escs if e.get("assigned_to") == user["user_id"]]
        if not escs:
            raise HTTPException(404, "Prediction not found")
    return escs


@app.get("/api/cases/{prediction_id}")
def case_detail(prediction_id: str, user: dict = Depends(current_user)):
    snap, escs = parallel((col("predictions").document(prediction_id).get,),
                          (_visible_escalations, prediction_id, user))
    if not snap.exists:
        raise HTTPException(404, "Prediction not found")
    case = doc_dict(snap, "prediction_id")
    case["ai_label"] = ai_label(case)
    pts, users = parallel(
        (get_many, "patients", [case.get("patient_id")], "patient_id"),
        (get_many, "users", [case.get("user_id")] + [e.get("assigned_to") for e in escs],
         "user_id"))
    patient = mask_patient(pts.get(case.get("patient_id")), user)
    case["clinician_name"] = users.get(case.get("user_id"), {}).get("full_name")
    for e in escs:
        e["specialist_name"] = users.get(e.get("assigned_to"), {}).get("full_name")
        e["sla_minutes"] = sla_minutes(e.get("priority"))
        e["overdue"] = e.get("status") == "sent" and _waiting_minutes(e) >= e["sla_minutes"]
    escs.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return {"case": case, "patient": patient, "escalations": escs}


def _state_counts() -> dict:
    counts: dict = {}
    for s in col("predictions").select(["triage_state"]).stream():
        st = (s.to_dict() or {}).get("triage_state")
        counts[st] = counts.get(st, 0) + 1
    return counts


@app.get("/api/stats")
def stats(user: dict = Depends(current_user)):
    counts = _state_counts()
    total = sum(counts.values())
    auto = counts.get("CLEARED", 0) + counts.get("FLAGGED_URGENT", 0)
    return {"total": total, "by_state": counts,
            "autonomy_rate": (auto / total) if total else None}


# ------------------------------------------------------------------ patients
def patient_label(pt: Optional[dict], user: dict) -> Optional[str]:
    """Specialists see a study code instead of the name when the patient's identity is hidden."""
    if not pt:
        return None
    if pt.get("hide_identity") and user["role"] == "specialist":
        return f"Patient {pt.get('patient_id', '')}".strip()
    return pt.get("full_name")


def mask_patient(pt: Optional[dict], user: dict) -> Optional[dict]:
    if not pt or not (pt.get("hide_identity") and user["role"] == "specialist"):
        return pt
    return {**{k: v for k, v in pt.items() if k not in ("guardian_name", "contact", "notes", "consent")},
            "full_name": patient_label(pt, user), "identity_hidden": True}


def _consent_record(given: bool, by, relationship, method, user: dict, reason=None) -> dict:
    if given and not (by or "").strip():
        raise HTTPException(400, "Enter the name of the parent or guardian giving consent.")
    if given and method not in CONSENT_METHODS:
        raise HTTPException(400, "Consent method must be verbal or written.")
    return {"given": given, "by": (by or "").strip() or None, "relationship": relationship,
            "method": method if given else None, "version": CONSENT_VERSION,
            "recorded_by": user["user_id"], "recorded_at": now(),
            "withdrawn_at": None if given else now(), "withdraw_reason": None if given else reason}


@app.post("/api/patients")
def create_patient(patient: PatientCreate, user: dict = Depends(require("clinician", "admin"))):
    if not patient.consent_given:
        raise HTTPException(400, "Guardian consent is required before registering a patient.")
    consent = _consent_record(True, patient.consent_by or patient.guardian_name,
                              patient.consent_relationship, patient.consent_method, user)
    patient_id = str(uuid.uuid4())[:8].upper()
    col("patients").document(patient_id).set({
        "full_name": patient.full_name, "age": patient.age, "gender": patient.gender,
        "notes": patient.notes, "guardian_name": patient.guardian_name,
        "contact": patient.contact, "created_at": now(), "created_by": user["user_id"],
        "consent": consent, "hide_identity": patient.hide_identity})
    log(None, f"patient_created:{patient_id}", user["user_id"])
    log(None, f"consent_recorded:{patient_id}", user["user_id"])
    return {"patient_id": patient_id, "message": "Patient created successfully",
            "consent": consent, "hide_identity": patient.hide_identity}


@app.post("/api/patients/{patient_id}/consent")
def update_consent(patient_id: str, body: ConsentUpdate,
                   user: dict = Depends(require("clinician", "admin"))):
    ref = col("patients").document(patient_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "Patient not found")
    old = (snap.to_dict() or {}).get("consent") or {}
    if body.given:
        consent = _consent_record(True, body.by, body.relationship, body.method, user)
    else:
        consent = {**old, "given": False, "withdrawn_at": now(), "withdraw_reason": body.reason,
                   "withdrawn_by": user["user_id"]}
    ref.update({"consent": consent})
    log(None, f"consent_{'recorded' if body.given else 'withdrawn'}:{patient_id}", user["user_id"])
    return {"consent": consent}


@app.patch("/api/patients/{patient_id}")
def update_patient(patient_id: str, update: PatientUpdate,
                   user: dict = Depends(require("clinician", "admin"))):
    ref = col("patients").document(patient_id)
    if not ref.get().exists:
        raise HTTPException(404, "Patient not found")
    changes = update.model_dump(exclude_none=True)
    if changes.get("full_name", "x").strip() == "":
        raise HTTPException(400, "Patient name cannot be empty")
    if changes:
        ref.update(changes)
        log(None, f"patient_updated:{patient_id}", user["user_id"])
    return {"updated": True, **changes}


@app.get("/api/patients")
def list_patients(user: dict = Depends(current_user)):
    """Specialists only see patients whose cases were sent to them."""
    if user["role"] == "specialist":
        ids = [e.get("patient_id") for e in _escalations_for("assigned_to", user["user_id"])]
        patients = [mask_patient(p, user) for p in get_many("patients", ids, "patient_id").values()]
    else:
        patients = [doc_dict(s, "patient_id") for s in col("patients").stream()]
    scans: dict = {}
    for s in col("predictions").select(["patient_id", "created_at"]).stream():
        d = s.to_dict() or {}
        n, last = scans.get(d.get("patient_id"), (0, None))
        c = d.get("created_at")
        scans[d.get("patient_id")] = (n + 1, max(filter(None, [last, c]), default=None))
    for p in patients:
        p["scan_count"], p["last_scan"] = scans.get(p["patient_id"], (0, None))
    patients.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return {"patients": patients}


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str, user: dict = Depends(current_user)):
    snap = col("patients").document(patient_id).get()
    if not snap.exists:
        raise HTTPException(404, "Patient not found")
    preds, escs = parallel(
        (lambda: [doc_dict(s, "prediction_id") for s in col("predictions").where(
            filter=FieldFilter("patient_id", "==", patient_id)).stream()],),
        (lambda: [doc_dict(s, "escalation_id") for s in col("escalations").where(
            filter=FieldFilter("patient_id", "==", patient_id)).stream()],))
    if user["role"] == "specialist":
        escs = [e for e in escs if e.get("assigned_to") == user["user_id"]]
        if not escs:
            raise HTTPException(404, "Patient not found")
        mine = {e["prediction_id"] for e in escs}
        preds = [p for p in preds if p["prediction_id"] in mine]
    preds.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    specialists = get_many("users", [e.get("assigned_to") for e in escs], "user_id")
    for e in escs:
        s = specialists.get(e.get("assigned_to"))
        e["specialist_name"] = s["full_name"] if s else None
    return {"patient": mask_patient(doc_dict(snap, "patient_id"), user), "predictions": preds,
            "escalations": escs}


# ------------------------------------------------------------------ escalations
@app.get("/api/specialists")
def list_specialists(user: dict = Depends(current_user)):
    q = col("users").where(filter=FieldFilter("role", "==", "specialist"))
    rows = [{"user_id": s.id, "full_name": d.get("full_name"), "hospital": d.get("hospital"),
             "is_active": d.get("is_active", True),
             "available": d.get("is_active", True) and d.get("available", True)}
            for s in q.stream() for d in [s.to_dict() or {}]]
    rows.sort(key=lambda r: r["full_name"] or "")
    return {"specialists": rows}


@app.post("/api/escalations")
def create_escalation(esc: EscalationCreate, user: dict = Depends(require("clinician", "admin"))):
    pred = col("predictions").document(esc.prediction_id).get()
    if not pred.exists:
        raise HTTPException(404, "Prediction not found")
    spec = col("users").document(esc.specialist_id).get()
    sd = spec.to_dict() if spec.exists else None
    if not sd or sd.get("role") != "specialist":
        raise HTTPException(404, "Specialist not found")
    if not sd.get("is_active", True) or not sd.get("available", True):
        raise HTTPException(400, f"{sd['full_name']} is not available")
    if esc.priority not in PRIORITIES:
        raise HTTPException(400, "Priority must be 'routine' or 'urgent'")
    escalation_id = str(uuid.uuid4())[:8].upper()
    col("escalations").document(escalation_id).set({
        "prediction_id": esc.prediction_id, "patient_id": pred.to_dict().get("patient_id"),
        "status": "sent", "assigned_to": esc.specialist_id, "note": esc.note,
        "priority": esc.priority, "specialist_comment": None, "created_at": now(),
        "resolved_at": None, "created_by": user["user_id"]})
    log(esc.prediction_id, f"escalated_to:{sd['full_name']}", user["user_id"])
    notify(esc.specialist_id, ("Urgent referral" if esc.priority == "urgent" else "New referral"),
           f"{user['full_name']} sent you a case to review.", f"review/{escalation_id}")
    if esc.priority == "urgent":
        alerts.alert_user({**sd, "user_id": esc.specialist_id}, "PneumoScan: urgent referral",
                          f"{user['full_name']} sent you an urgent chest X-ray referral ({escalation_id}). "
                          f"Please open it in PneumoScan within {URGENT_SLA_MIN} minutes.",
                          f"/specialist#/review/{escalation_id}")
    return {"escalation_id": escalation_id, "status": "sent", "priority": esc.priority,
            "assigned_to": esc.specialist_id, "specialist_name": sd["full_name"]}


def _own_escalation(escalation_id: str, user: dict):
    ref = col("escalations").document(escalation_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "Escalation not found")
    esc = snap.to_dict()
    if user["role"] != "admin" and esc.get("assigned_to") != user["user_id"]:
        raise HTTPException(403, "This case is not assigned to you.")
    return ref, esc


@app.post("/api/escalations/{escalation_id}/start")
def start_escalation_review(escalation_id: str,
                            user: dict = Depends(require("specialist", "admin"))):
    ref, esc = _own_escalation(escalation_id, user)
    if esc["status"] == "sent":
        ref.update({"status": "pending"})
        log(esc["prediction_id"], "specialist_review_started", user["user_id"])
        notify(esc.get("created_by"), "Referral acknowledged",
               f"{user['full_name']} has started reviewing your referral.",
               f"case/{esc['prediction_id']}")
    return {"status": "pending"}


def sla_minutes(priority: Optional[str]) -> int:
    return URGENT_SLA_MIN if priority == "urgent" else ROUTINE_SLA_MIN


def _waiting_minutes(e: dict) -> float:
    h = hours_between(e.get("assigned_at") or e.get("created_at"), now())
    return (h or 0) * 60


@app.post("/api/escalations/{escalation_id}/reassign")
def reassign_escalation(escalation_id: str, body: Reassign,
                        user: dict = Depends(require("clinician", "admin"))):
    ref = col("escalations").document(escalation_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "Escalation not found")
    esc = snap.to_dict() or {}
    if user["role"] == "clinician" and esc.get("created_by") != user["user_id"]:
        raise HTTPException(403, "Only the referring clinician or an administrator can reassign.")
    if esc.get("status") == "resolved":
        raise HTTPException(400, "This referral has already been reviewed.")
    if body.specialist_id == esc.get("assigned_to"):
        raise HTTPException(400, "The case is already assigned to this specialist.")
    spec = col("users").document(body.specialist_id).get()
    sd = spec.to_dict() if spec.exists else None
    if not sd or sd.get("role") != "specialist":
        raise HTTPException(404, "Specialist not found")
    if not sd.get("is_active", True) or not sd.get("available", True):
        raise HTTPException(400, f"{sd['full_name']} is not available")
    old = esc.get("assigned_to")
    ref.update({"assigned_to": body.specialist_id, "status": "sent", "assigned_at": now(),
                "reminded_at": None, "overdue_alerted_at": None,
                "history": firestore.ArrayUnion([{"from": old, "to": body.specialist_id, "at": now(),
                                                  "by": user["user_id"], "reason": body.reason or ""}])})
    log(esc["prediction_id"], f"referral_reassigned_to:{sd['full_name']}", user["user_id"])
    urgent = esc.get("priority") == "urgent"
    notify(body.specialist_id, "Urgent referral" if urgent else "New referral",
           f"{user['full_name']} reassigned a case to you.", f"review/{escalation_id}")
    notify(old, "Referral reassigned", "A referral in your queue was reassigned to another specialist.", "queue")
    if urgent:
        alerts.alert_user({**sd, "user_id": body.specialist_id}, "PneumoScan: urgent referral",
                          f"An urgent chest X-ray referral ({escalation_id}) was reassigned to you.",
                          f"/specialist#/review/{escalation_id}")
    return {"escalation_id": escalation_id, "assigned_to": body.specialist_id,
            "specialist_name": sd["full_name"], "status": "sent"}


def sweep_referrals() -> dict:
    """Remind specialists about unopened referrals after the SLA and alert admins at twice the SLA."""
    reminded = overdue = 0
    admins = None
    for snap in col("escalations").where(filter=FieldFilter("status", "==", "sent")).stream():
        e = snap.to_dict() or {}
        waited, sla = _waiting_minutes(e), sla_minutes(e.get("priority"))
        urgent = e.get("priority") == "urgent"
        label = "urgent" if urgent else "routine"
        if waited >= sla and not e.get("reminded_at"):
            snap.reference.update({"reminded_at": now()})
            notify(e.get("assigned_to"), f"Reminder: {label} referral waiting",
                   f"A {label} referral has waited {int(waited)} minutes without being opened.",
                   f"review/{snap.id}")
            notify(e.get("created_by"), "Referral not yet opened",
                   f"Your {label} referral has not been opened after {int(waited)} minutes. "
                   "You can reassign it to another specialist from the case page.",
                   f"case/{e.get('prediction_id')}")
            if urgent:
                spec = col("users").document(e.get("assigned_to") or "-").get()
                if spec.exists:
                    alerts.alert_user({**spec.to_dict(), "user_id": spec.id},
                                      "PneumoScan: urgent referral still waiting",
                                      f"Urgent referral {snap.id} has waited {int(waited)} minutes.",
                                      f"/specialist#/review/{snap.id}")
            log(e.get("prediction_id"), "referral_reminder", None)
            reminded += 1
        if waited >= 2 * sla and not e.get("overdue_alerted_at"):
            snap.reference.update({"overdue_alerted_at": now()})
            if admins is None:
                admins = [doc_dict(a, "user_id") for a in
                          col("users").where(filter=FieldFilter("role", "==", "admin")).stream()]
            for a in admins:
                if a.get("is_active", True):
                    notify(a["user_id"], f"Overdue {label} referral",
                           f"Referral {snap.id} has not been opened after {int(waited)} minutes. "
                           "Consider reassigning it.", "referrals")
                    if urgent:
                        alerts.alert_user(a, "PneumoScan: overdue urgent referral",
                                          f"Urgent referral {snap.id} has waited {int(waited)} minutes.",
                                          "/admin#/referrals")
            log(e.get("prediction_id"), "referral_overdue", None)
            overdue += 1
    return {"reminded": reminded, "overdue": overdue}


def _sweep_loop():
    while True:
        time.sleep(60)
        try:
            if firebase.configured():
                sweep_referrals()
        except Exception as e:
            print(f"[referrals] sweep failed: {e}")


@app.on_event("startup")
def start_referral_sweeper():
    if os.environ.get("REFERRAL_SWEEP", "1") != "0":
        threading.Thread(target=_sweep_loop, daemon=True).start()


@app.post("/api/admin/referrals/sweep")
def run_sweep(user: dict = Depends(require("admin"))):
    return sweep_referrals()


@app.get("/api/admin/email-settings")
def get_email_settings(user: dict = Depends(require("admin"))):
    return alerts.public_settings()


@app.put("/api/admin/email-settings")
def put_email_settings(body: EmailSettings, user: dict = Depends(require("admin"))):
    addr = body.user.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
        raise HTTPException(400, "Enter the full email address that sends the alerts.")
    if body.port not in (25, 465, 587, 1025, 2525):
        raise HTTPException(400, "Port must be 587 (STARTTLS) or 465 (SSL).")
    changes = {"user": addr, "sender": (body.sender or "").strip(), "host": body.host.strip() or "smtp.gmail.com",
               "port": body.port, "enabled": body.enabled, "updated_at": now(), "updated_by": user["user_id"]}
    if body.password and body.password.strip():
        changes["password"] = body.password.strip()
    elif body.enabled and not alerts.stored_password_set():
        raise HTTPException(400, "Enter the app password.")
    alerts.save_settings(changes)
    log(None, f"email_settings_updated:{addr}:{'on' if body.enabled else 'off'}", user["user_id"])
    return alerts.public_settings()


@app.post("/api/admin/email-settings/test")
def test_email_settings(user: dict = Depends(require("admin"))):
    if not alerts.email_configured():
        raise HTTPException(400, "Save and turn on the email settings first.")
    try:
        alerts._send_email(user["email"], "PneumoScan test email",
                           "Email alerts are working. Urgent referral alerts will be sent from this account.")
    except Exception as e:
        raise HTTPException(400, f"Could not send: {str(e)[:250]}")
    log(None, "email_settings_tested", user["user_id"])
    return {"sent": True, "to": user["email"]}


@app.get("/api/admin/alerts")
def alert_log(limit: int = 50, user: dict = Depends(require("admin"))):
    q = col("alert_outbox").order_by("created_at", direction=Query.DESCENDING).limit(min(limit, 200))
    return {"alerts": [doc_dict(s, "alert_id") for s in q.stream()], "providers": alerts.status(),
            "sla": {"urgent_minutes": URGENT_SLA_MIN, "routine_minutes": ROUTINE_SLA_MIN}}


@app.get("/api/escalations")
def list_escalations(status: Optional[str] = None, images: bool = True,
                     user: dict = Depends(current_user)):
    """Specialists see cases assigned to them, clinicians see cases they sent, admins see all."""
    if user["role"] == "specialist":
        rows = _escalations_for("assigned_to", user["user_id"])
    elif user["role"] == "clinician":
        rows = _escalations_for("created_by", user["user_id"])
    else:
        rows = [doc_dict(s, "escalation_id") for s in col("escalations").stream()]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    preds, patients, users = parallel(
        (get_many, "predictions", [r["prediction_id"] for r in rows], "prediction_id",
         None if images else PRED_LIST_FIELDS),
        (get_many, "patients", [r.get("patient_id") for r in rows], "patient_id"),
        (get_many, "users", [r.get("created_by") for r in rows] +
         [r.get("assigned_to") for r in rows], "user_id"))
    for r in rows:
        p = preds.get(r["prediction_id"], {})
        r["unread"] = (r.get("unread") or {}).get(user["user_id"], 0)
        r["final_outcome"] = p.get("confirmed_outcome")
        for k in ("filename", "calibrated_prob", "ood_score", "triage_state", "clinical") + (("heatmap_png", "image_png") if images else ()):
            r[k] = p.get(k)
        r["ai_label"] = ai_label(p)
        r.setdefault("priority", "routine")
        pt = patients.get(r.get("patient_id"), {})
        r["patient_name"], r["age"], r["gender"] = patient_label(pt, user), pt.get("age"), pt.get("gender")
        r["identity_hidden"] = bool(pt.get("hide_identity")) and user["role"] == "specialist"
        r["sla_minutes"] = sla_minutes(r.get("priority"))
        r["overdue"] = r.get("status") == "sent" and _waiting_minutes(r) >= r["sla_minutes"]
        r["clinician_name"] = users.get(r.get("created_by"), {}).get("full_name")
        r["specialist_name"] = users.get(r.get("assigned_to"), {}).get("full_name")
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"escalations": rows}


@app.post("/api/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: str, resolution: EscalationResolve,
                       user: dict = Depends(require("specialist", "admin"))):
    ref, esc = _own_escalation(escalation_id, user)
    if esc["status"] == "resolved":
        raise HTTPException(400, "Escalation already resolved")
    pref = col("predictions").document(esc["prediction_id"])
    psnap = pref.get()
    label = ai_label(psnap.to_dict() or {}) if psnap.exists else None
    outcome = resolution.confirmed_outcome
    agrees = (label == outcome) if outcome in ("pneumonia", "normal") and label else None
    ref.update({"status": "resolved", "specialist_comment": resolution.specialist_comment,
                "findings": resolution.findings, "recommendation": resolution.recommendation,
                "confirmed_outcome": outcome, "ai_label": label, "ai_agrees": agrees,
                "resolved_at": now(), "resolved_by": user["user_id"]})
    if outcome and psnap.exists:
        pref.update({"confirmed_outcome": outcome})
    log(esc["prediction_id"], f"specialist_review:{outcome or 'reviewed'}", user["user_id"])
    notify(esc.get("created_by"), "Specialist review completed",
           f"{user['full_name']} reviewed your referral"
           + (f": {outcome}." if outcome else "."), f"case/{esc['prediction_id']}")
    return {"resolved": True, "ai_agrees": agrees}



# ------------------------------------------------------------------ chat
def _chat_escalation(escalation_id: str, user: dict):
    """Only the referring clinician and the assigned specialist can use a referral's thread."""
    ref = col("escalations").document(escalation_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "Referral not found")
    esc = snap.to_dict()
    if user["user_id"] not in (esc.get("created_by"), esc.get("assigned_to")):
        raise HTTPException(403, "Only the referring clinician and the assigned specialist "
                                 "can use this conversation.")
    return ref, esc


def _conversations(user: dict) -> list:
    if user["role"] == "specialist":
        rows = _escalations_for("assigned_to", user["user_id"])
    elif user["role"] == "clinician":
        rows = _escalations_for("created_by", user["user_id"])
    else:
        return []
    for r in rows:
        r["unread"] = (r.get("unread") or {}).get(user["user_id"], 0)
    return rows


@app.get("/api/conversations")
def conversations(user: dict = Depends(require("clinician", "specialist"))):
    rows = _conversations(user)
    other_field = "created_by" if user["role"] == "specialist" else "assigned_to"
    patients, preds, users = parallel(
        (get_many, "patients", [r.get("patient_id") for r in rows], "patient_id"),
        (get_many, "predictions", [r.get("prediction_id") for r in rows], "prediction_id",
         ["triage_state"]),
        (get_many, "users", [r.get(other_field) for r in rows], "user_id"))
    out = []
    for r in rows:
        pt = patients.get(r.get("patient_id"), {})
        other = users.get(r.get(other_field), {})
        out.append({"escalation_id": r["escalation_id"], "prediction_id": r.get("prediction_id"),
                    "patient_name": patient_label(pt, user), "status": r.get("status"),
                    "priority": r.get("priority", "routine"),
                    "triage_state": preds.get(r.get("prediction_id"), {}).get("triage_state"),
                    "other_name": other.get("full_name"), "other_role": other.get("role"),
                    "last_message": r.get("last_message"), "unread": r["unread"],
                    "updated_at": (r.get("last_message") or {}).get("at") or r.get("created_at")})
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return {"conversations": out, "unread": sum(c["unread"] for c in out)}


@app.get("/api/escalations/{escalation_id}/messages")
def get_messages(escalation_id: str, after: Optional[str] = None,
                 user: dict = Depends(require("clinician", "specialist"))):
    ref, esc = _chat_escalation(escalation_id, user)
    q = col("messages").where(filter=FieldFilter("escalation_id", "==", escalation_id))
    rows = sorted((doc_dict(s, "message_id") for s in q.stream()),
                  key=lambda m: m.get("created_at") or "")
    if after:
        rows = [m for m in rows if (m.get("created_at") or "") > after]
    if (esc.get("unread") or {}).get(user["user_id"]):
        ref.update({f"unread.{user['user_id']}": 0})
    other = esc.get("assigned_to") if user["user_id"] == esc.get("created_by") else esc.get("created_by")
    other_read = (esc.get("read_at") or {}).get(other)
    ref.update({f"read_at.{user['user_id']}": now()})
    return {"messages": rows, "status": esc.get("status"), "other_read_at": other_read}


@app.post("/api/escalations/{escalation_id}/messages")
def post_message(escalation_id: str, body: MessageCreate,
                 user: dict = Depends(require("clinician", "specialist"))):
    ref, esc = _chat_escalation(escalation_id, user)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message cannot be empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Messages are limited to {MAX_MESSAGE_CHARS} characters")
    ts = now()
    msg = {"escalation_id": escalation_id, "sender_id": user["user_id"],
           "sender_name": user["full_name"], "sender_role": user["role"],
           "text": text, "created_at": ts}
    _, mref = col("messages").add(msg)
    other = esc.get("assigned_to") if user["user_id"] == esc.get("created_by") else esc.get("created_by")
    ref.update({"last_message": {"text": text[:140], "sender_id": user["user_id"], "at": ts},
                f"unread.{other}": firestore.Increment(1),
                f"read_at.{user['user_id']}": ts})
    link = f"messages/{escalation_id}"
    pending = col("notifications").where(filter=FieldFilter("user_id", "==", other)).stream()
    if not any((n.to_dict() or {}).get("link") == link and not (n.to_dict() or {}).get("read")
               for n in pending):
        notify(other, f"New message from {user['full_name']}", text[:120], link)
    return {"message_id": mref.id, **msg}


# ------------------------------------------------------------------ admin
@app.get("/api/admin/stats")
def admin_stats(user: dict = Depends(require("admin"))):
    by_state, by_model, by_user_id, by_outcome = {}, {}, {}, {}
    fields = ["triage_state", "model_version", "user_id", "confirmed_outcome"]
    for s in col("predictions").select(fields).stream():
        d = s.to_dict() or {}
        by_state[d.get("triage_state")] = by_state.get(d.get("triage_state"), 0) + 1
        by_model[d.get("model_version")] = by_model.get(d.get("model_version"), 0) + 1
        if d.get("user_id"):
            by_user_id[d["user_id"]] = by_user_id.get(d["user_id"], 0) + 1
        if d.get("confirmed_outcome"):
            by_outcome[d["confirmed_outcome"]] = by_outcome.get(d["confirmed_outcome"], 0) + 1
    total = sum(by_state.values())
    auto = by_state.get("CLEARED", 0) + by_state.get("FLAGGED_URGENT", 0)
    abstained = by_state.get("ESCALATED", 0) + by_state.get("REJECTED", 0)
    names = get_many("users", by_user_id.keys(), "user_id")
    by_user = {names.get(k, {}).get("full_name", k): n for k, n in by_user_id.items()}
    return {"total_predictions": total, "by_state": by_state,
            "autonomy_rate": (auto / total) if total else None,
            "abstention_rate": (abstained / total) if total else None,
            "by_model": by_model, "by_user": by_user, "by_outcome": by_outcome}


@app.get("/api/admin/users")
def get_users(user: dict = Depends(require("admin"))):
    return {"users": [doc_dict(s, "user_id") for s in col("users").stream()]}


@app.post("/api/admin/users")
def create_user(data: UserCreate, user: dict = Depends(require("admin"))):
    if data.role not in ROLES:
        raise HTTPException(400, "Role must be 'clinician', 'specialist' or 'admin'")
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    try:
        rec = fb_auth.create_user(email=data.email, password=data.password,
                                  display_name=data.full_name)
    except fb_auth.EmailAlreadyExistsError:
        raise HTTPException(400, "Email already registered")
    col("users").document(rec.uid).set({
        "email": data.email, "full_name": data.full_name, "role": data.role,
        "hospital": data.hospital, "created_at": now(), "is_active": True})
    log(None, f"user_created:{data.email}:{data.role}", user["user_id"])
    return {"message": "User created successfully", "user_id": rec.uid}


@app.patch("/api/admin/users/{uid}")
def update_user(uid: str, data: AdminUserUpdate, user: dict = Depends(require("admin"))):
    ref = col("users").document(uid)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "User not found")
    changes = data.model_dump(exclude_none=True)
    if changes.pop("reset_mfa", False):
        col("mfa").document(uid).delete()
        changes["mfa_enabled"] = False
    if "role" in changes and changes["role"] not in ROLES:
        raise HTTPException(400, "Role must be 'clinician', 'specialist' or 'admin'")
    if uid == user["user_id"] and (changes.get("role", "admin") != "admin"
                                   or changes.get("is_active") is False):
        raise HTTPException(400, "You cannot remove your own admin access.")
    if not changes:
        return {"updated": False}
    ref.update(changes)
    if "is_active" in changes:
        try:
            fb_auth.update_user(uid, disabled=not changes["is_active"])
        except Exception as e:
            print(f"[firebase] could not update auth status for {uid}: {e}")
    forget_profile(uid)
    log(None, f"user_updated:{(snap.to_dict() or {}).get('email')}:" +
        ",".join(f"{k}={v}" for k, v in changes.items()), user["user_id"])
    return {"updated": True, "user_id": uid, **changes}


@app.post("/api/admin/announce")
def announce(body: Announcement, user: dict = Depends(require("admin"))):
    title, text = body.title.strip(), body.body.strip()
    if not title or not text:
        raise HTTPException(400, "Title and message are required")
    if body.audience not in ("all",) + ROLES:
        raise HTTPException(400, "Unknown audience")
    q = col("users")
    if body.audience != "all":
        q = q.where(filter=FieldFilter("role", "==", body.audience))
    n = 0
    for s in q.stream():
        d = s.to_dict() or {}
        if d.get("is_active", True) and s.id != user["user_id"]:
            notify(s.id, f"Announcement: {title}", text, "")
            n += 1
    log(None, f"announcement:{body.audience}:{title[:60]}", user["user_id"])
    return {"sent": n}


CALIB_BINS = 5


def _calibration_summary(calib: list, brier: list) -> dict:
    n = sum(b[0] for b in calib)
    bins = [{"lo": i / CALIB_BINS, "hi": (i + 1) / CALIB_BINS, "n": c,
             "mean_predicted": s / c if c else None, "observed": y / c if c else None}
            for i, (c, s, y) in enumerate(calib)]
    ece = sum(abs(b["mean_predicted"] - b["observed"]) * b["n"] for b in bins if b["n"]) / n if n else None
    return {"n": n, "bins": bins, "ece": ece, "brier": sum(brier) / n if n else None,
            "min_cases": CALIB_MIN_CASES, "enough": n >= CALIB_MIN_CASES}


CALIB_MIN_CASES = 20


@app.get("/api/admin/monitoring")
def monitoring(days: int = 14, user: dict = Depends(require("admin"))):
    """Live behaviour of the model compared with its held-out test results."""
    days = max(1, min(days, 90))
    today = datetime.now(timezone.utc).date()
    daily = {(today - timedelta(d)).isoformat(): {s: 0 for s in STATES}
             for d in range(days - 1, -1, -1)}
    hist = [0] * 10
    matrix = {a: {t: 0 for t in ("pneumonia", "normal")} for a in ("pneumonia", "normal")}
    counts = {s: 0 for s in STATES}
    fields = ["triage_state", "created_at", "calibrated_prob", "confirmed_outcome", "model_version"]
    calib = [[0, 0.0, 0] for _ in range(CALIB_BINS)]   # n, sum of p, n pneumonia
    brier, versions = [], {}
    for snap in col("predictions").select(fields).stream():
        p = snap.to_dict() or {}
        st = p.get("triage_state")
        if st in counts:
            counts[st] += 1
        day = (p.get("created_at") or "")[:10]
        if day in daily and st in STATES:
            daily[day][st] += 1
        if p.get("calibrated_prob") is not None and st != "REJECTED":
            hist[min(int(p["calibrated_prob"] * 10), 9)] += 1
        label, truth = ai_label(p), p.get("confirmed_outcome")
        if label and truth in ("pneumonia", "normal"):
            matrix[label][truth] += 1
        if p.get("model_version"):
            versions[p["model_version"]] = versions.get(p["model_version"], 0) + 1
        prob = p.get("calibrated_prob")
        if prob is not None and st != "REJECTED" and truth in ("pneumonia", "normal"):
            y = 1 if truth == "pneumonia" else 0
            b = calib[min(int(prob * CALIB_BINS), CALIB_BINS - 1)]
            b[0] += 1; b[1] += prob; b[2] += y
            brier.append((prob - y) ** 2)
    confirmed = sum(sum(r.values()) for r in matrix.values())
    agree = matrix["pneumonia"]["pneumonia"] + matrix["normal"]["normal"]

    turnaround, open_by_priority = [], {p: 0 for p in PRIORITIES}
    for snap in col("escalations").stream():
        e = snap.to_dict() or {}
        if e.get("status") == "resolved":
            h = hours_between(e.get("created_at"), e.get("resolved_at"))
            if h is not None:
                turnaround.append(h)
        else:
            open_by_priority[e.get("priority", "routine")] = \
                open_by_priority.get(e.get("priority", "routine"), 0) + 1
    turnaround.sort()
    total = sum(counts.values())
    test = _read_json(ROOT / "results" / "gating_results.json")
    n_test = (test.get("ungated_all_images") or {}).get("n")

    def rate(k):
        return counts[k] / total if total else None

    return {
        "total": total, "by_state": counts,
        "live": {"autonomy_rate": ((counts["CLEARED"] + counts["FLAGGED_URGENT"]) / total)
                 if total else None,
                 "escalation_rate": rate("ESCALATED"), "rejection_rate": rate("REJECTED")},
        "expected": {"autonomy_rate": test.get("autonomy_rate"),
                     "escalation_rate": (test["escalated_low_confidence"] / n_test)
                     if n_test and "escalated_low_confidence" in test else None,
                     "rejection_rate": (test["rejected_out_of_distribution"] / n_test)
                     if n_test and "rejected_out_of_distribution" in test else None},
        "daily": [{"date": d, **v} for d, v in daily.items()],
        "probability_histogram": hist,
        "calibration": _calibration_summary(calib, brier),
        "model_versions": versions, "current_model_version": engine.version,
        "agreement": {"confirmed": confirmed, "agree": agree,
                      "rate": agree / confirmed if confirmed else None, "matrix": matrix},
        "referrals": {"open_by_priority": open_by_priority, "resolved": len(turnaround),
                      "median_turnaround_h": turnaround[len(turnaround) // 2]
                      if turnaround else None,
                      "mean_turnaround_h": sum(turnaround) / len(turnaround)
                      if turnaround else None},
    }


@app.get("/api/admin/audit")
def get_audit_logs(limit: int = 300, user: dict = Depends(require("admin"))):
    q = col("audit_logs").order_by("timestamp", direction=Query.DESCENDING).limit(limit)
    logs = [doc_dict(s, "log_id") for s in q.stream()]
    users = get_many("users", [l.get("user_id") for l in logs], "user_id")
    for l in logs:
        u = users.get(l.get("user_id"), {})
        l["full_name"], l["email"] = u.get("full_name"), u.get("email")
    return {"logs": logs}


@app.get("/api/report/{prediction_id}")
def generate_report_endpoint(prediction_id: str, user: dict = Depends(current_user)):
    snap = col("predictions").document(prediction_id).get()
    if not snap.exists:
        raise HTTPException(404, "Prediction not found")
    escs = _visible_escalations(prediction_id, user)
    data = doc_dict(snap, "prediction_id")
    resolved = sorted((e for e in escs if e.get("status") == "resolved"),
                      key=lambda e: e.get("resolved_at") or "")
    if resolved:
        review = resolved[-1]
        spec = col("users").document(review.get("assigned_to") or "-").get()
        review["specialist_name"] = (spec.to_dict() or {}).get("full_name") if spec.exists else None
        data["review"] = review
    if data.get("patient_id"):
        patient = col("patients").document(data["patient_id"]).get()
        if patient.exists:
            data["patient_name"] = patient_label({**patient.to_dict(), "patient_id": patient.id}, user)
    try:
        buffer = generate_report(data)
    except Exception as e:
        raise HTTPException(500, f"Failed to generate report: {e}")
    return Response(content=buffer.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment; filename=report_{prediction_id}.pdf"})


PAGES = {"/": "index.html",
         "/login": "login.html", "/specialist/login": "login.html", "/admin/login": "login.html",
         "/clinician": "app.html", "/specialist": "app.html", "/admin": "app.html"}


def _page(name: str):
    def view():
        return FileResponse(FRONTEND / name, headers={"Cache-Control": "no-store"})
    return view


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    for _path, _name in PAGES.items():
        app.add_api_route(_path, _page(_name), methods=["GET"], include_in_schema=False)
