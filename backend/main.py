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
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from firebase_admin import auth as fb_auth
from google.cloud.firestore_v1 import FieldFilter, Query
from PIL import Image
from pydantic import BaseModel

from . import firebase
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

app = FastAPI(
    title="PneumoScan API",
    description=("Autonomous chest X-ray triage with a confidence gate. "
                 "Research prototype — not a certified diagnostic device."),
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


def get_many(collection: str, ids, id_field: str) -> dict:
    """Fetch documents by id in one round trip; returns {id: dict}."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    c = col(collection)
    snaps = firebase.db().get_all([c.document(i) for i in ids])
    return {s.id: doc_dict(s, id_field) for s in snaps if s.exists}


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


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    claims = _decode(authorization)
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


class UserRegister(BaseModel):
    full_name: str
    hospital: Optional[str] = None


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "clinician"
    hospital: Optional[str] = None


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    hospital: Optional[str] = None


class PatientCreate(BaseModel):
    full_name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = ""
    guardian_name: Optional[str] = None
    contact: Optional[str] = None


class PatientUpdate(BaseModel):
    full_name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = None
    guardian_name: Optional[str] = None
    contact: Optional[str] = None


class AdminUserUpdate(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    available: Optional[bool] = None
    full_name: Optional[str] = None
    hospital: Optional[str] = None


class Availability(BaseModel):
    available: bool


class EscalationCreate(BaseModel):
    prediction_id: str
    specialist_id: str
    note: Optional[str] = ""
    priority: str = "routine"


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
def me(user: dict = Depends(current_user)):
    return user


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
               "hospital": body.hospital, "created_at": now(), "is_active": True}
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
    if changes:
        col("users").document(user["user_id"]).update(changes)
        forget_profile(user["user_id"])
    merged = {**user, **changes}
    return {"updated": True, "full_name": merged["full_name"], "hospital": merged.get("hospital")}


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
    rows = sorted((doc_dict(s, "notification_id") for s in q.stream()),
                  key=lambda r: r.get("created_at") or "", reverse=True)
    return {"unread": sum(1 for r in rows if not r.get("read")), "notifications": rows[:30]}


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

    result = engine.predict(img)
    pid = str(uuid.uuid4())
    col("predictions").document(pid).set({
        "patient_id": patient_id, "filename": file.filename, "created_at": now(),
        "calibrated_prob": result.probability, "ood_score": result.ood_score,
        "triage_state": result.state, "model_version": result.model_version,
        "confirmed_outcome": None, "comment": None,
        "heatmap_png": result.heatmap_png, "image_png": to_stored_image(img),
        "user_id": user["user_id"]})
    log(pid, f"triage:{result.state}", user["user_id"])
    return {"prediction_id": pid, "patient_id": patient_id, **result.to_dict()}


@app.post("/api/feedback")
def feedback(fb: Feedback, user: dict = Depends(current_user)):
    ref = col("predictions").document(fb.prediction_id)
    if not ref.get().exists:
        raise HTTPException(404, "Unknown prediction_id.")
    ref.update({"confirmed_outcome": fb.confirmed_outcome, "comment": fb.comment})
    log(fb.prediction_id, f"feedback:{fb.confirmed_outcome}", user["user_id"])
    return {"recorded": True}


def _escalations_for(field: str, uid: str) -> list:
    q = col("escalations").where(filter=FieldFilter(field, "==", uid))
    return [doc_dict(s, "escalation_id") for s in q.stream()]


@app.get("/api/cases")
def cases(limit: int = 25, mine: bool = False, user: dict = Depends(current_user)):
    """Specialists see only cases sent to them; everyone else sees all cases, or only their
    own with mine=true."""
    if mine and user["role"] != "specialist":
        q = col("predictions").where(filter=FieldFilter("user_id", "==", user["user_id"]))
        rows = [doc_dict(s, "prediction_id") for s in q.stream()]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        rows = rows[:limit]
    elif user["role"] == "specialist":
        ids = [e["prediction_id"] for e in _escalations_for("assigned_to", user["user_id"])]
        rows = list(get_many("predictions", ids, "prediction_id").values())
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        rows = rows[:limit]
    else:
        q = col("predictions").order_by("created_at", direction=Query.DESCENDING).limit(limit)
        rows = [doc_dict(s, "prediction_id") for s in q.stream()]
    patients = get_many("patients", [r.get("patient_id") for r in rows], "patient_id")
    users = get_many("users", [r.get("user_id") for r in rows], "user_id")
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
    snap = col("predictions").document(prediction_id).get()
    if not snap.exists:
        raise HTTPException(404, "Prediction not found")
    escs = _visible_escalations(prediction_id, user)
    case = doc_dict(snap, "prediction_id")
    case["ai_label"] = ai_label(case)
    patient = None
    if case.get("patient_id"):
        p = col("patients").document(case["patient_id"]).get()
        patient = doc_dict(p, "patient_id") if p.exists else None
    users = get_many("users", [case.get("user_id")] + [e.get("assigned_to") for e in escs],
                     "user_id")
    case["clinician_name"] = users.get(case.get("user_id"), {}).get("full_name")
    for e in escs:
        e["specialist_name"] = users.get(e.get("assigned_to"), {}).get("full_name")
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
@app.post("/api/patients")
def create_patient(patient: PatientCreate, user: dict = Depends(require("clinician", "admin"))):
    patient_id = str(uuid.uuid4())[:8].upper()
    col("patients").document(patient_id).set({
        "full_name": patient.full_name, "age": patient.age, "gender": patient.gender,
        "notes": patient.notes, "guardian_name": patient.guardian_name,
        "contact": patient.contact, "created_at": now(), "created_by": user["user_id"]})
    log(None, f"patient_created:{patient_id}", user["user_id"])
    return {"patient_id": patient_id, "message": "Patient created successfully"}


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
        patients = list(get_many("patients", ids, "patient_id").values())
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
    preds = [doc_dict(s, "prediction_id") for s in
             col("predictions").where(filter=FieldFilter("patient_id", "==", patient_id)).stream()]
    escs = [doc_dict(s, "escalation_id") for s in
            col("escalations").where(filter=FieldFilter("patient_id", "==", patient_id)).stream()]
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
    return {"patient": doc_dict(snap, "patient_id"), "predictions": preds, "escalations": escs}


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
           f"{user['full_name']} sent you a case to review.", "queue")
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
    return {"status": "pending"}


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
    preds = get_many("predictions", [r["prediction_id"] for r in rows], "prediction_id")
    patients = get_many("patients", [r.get("patient_id") for r in rows], "patient_id")
    users = get_many("users", [r.get("created_by") for r in rows] +
                     [r.get("assigned_to") for r in rows], "user_id")
    for r in rows:
        p = preds.get(r["prediction_id"], {})
        for k in ("filename", "calibrated_prob", "ood_score", "triage_state",
                  "confirmed_outcome") + (("heatmap_png", "image_png") if images else ()):
            r[k] = p.get(k)
        r["ai_label"] = ai_label(p)
        r.setdefault("priority", "routine")
        pt = patients.get(r.get("patient_id"), {})
        r["patient_name"], r["age"], r["gender"] = pt.get("full_name"), pt.get("age"), pt.get("gender")
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
           + (f": {outcome}." if outcome else "."), "referrals")
    return {"resolved": True, "ai_agrees": agrees}


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
    fields = ["triage_state", "created_at", "calibrated_prob", "confirmed_outcome"]
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
            data["patient_name"] = patient.to_dict().get("full_name")
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
        return FileResponse(FRONTEND / name, headers={"Cache-Control": "no-cache"})
    return view


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    for _path, _name in PAGES.items():
        app.add_api_route(_path, _page(_name), methods=["GET"], include_in_schema=False)
