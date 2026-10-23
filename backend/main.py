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
import uuid
from datetime import datetime, timezone
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


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    claims = _decode(authorization)
    snap = col("users").document(claims["uid"]).get()
    if not snap.exists:
        raise HTTPException(403, "No PneumoScan profile for this account.")
    user = doc_dict(snap, "user_id")
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


class EscalationCreate(BaseModel):
    prediction_id: str
    specialist_id: str
    note: Optional[str] = ""


class EscalationResolve(BaseModel):
    specialist_comment: str
    confirmed_outcome: Optional[str] = None


# ------------------------------------------------------------------ public
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
    merged = {**user, **changes}
    return {"updated": True, "full_name": merged["full_name"], "hospital": merged.get("hospital")}


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
def cases(limit: int = 25, user: dict = Depends(current_user)):
    """Specialists see only cases sent to them; everyone else sees all cases."""
    if user["role"] == "specialist":
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
    return {"count": len(rows), "cases": rows}


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
        "notes": patient.notes, "created_at": now(), "created_by": user["user_id"]})
    return {"patient_id": patient_id, "message": "Patient created successfully"}


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
    preds.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    escs = [doc_dict(s, "escalation_id") for s in
            col("escalations").where(filter=FieldFilter("patient_id", "==", patient_id)).stream()]
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
             "is_active": d.get("is_active", True)}
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
    if not sd.get("is_active", True):
        raise HTTPException(400, f"{sd['full_name']} is not available")
    escalation_id = str(uuid.uuid4())[:8].upper()
    col("escalations").document(escalation_id).set({
        "prediction_id": esc.prediction_id, "patient_id": pred.to_dict().get("patient_id"),
        "status": "sent", "assigned_to": esc.specialist_id, "note": esc.note,
        "specialist_comment": None, "created_at": now(), "resolved_at": None,
        "created_by": user["user_id"]})
    log(esc.prediction_id, f"escalated_to:{sd['full_name']}", user["user_id"])
    return {"escalation_id": escalation_id, "status": "sent",
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
def list_escalations(status: Optional[str] = None, user: dict = Depends(current_user)):
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
                  "heatmap_png", "image_png", "confirmed_outcome"):
            r[k] = p.get(k)
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
    ref.update({"status": "resolved", "specialist_comment": resolution.specialist_comment,
                "resolved_at": now()})
    if resolution.confirmed_outcome:
        col("predictions").document(esc["prediction_id"]).update(
            {"confirmed_outcome": resolution.confirmed_outcome})
    log(esc["prediction_id"],
        f"specialist_review:{resolution.confirmed_outcome or 'reviewed'}", user["user_id"])
    return {"resolved": True}


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


@app.get("/api/admin/audit")
def get_audit_logs(limit: int = 100, user: dict = Depends(require("admin"))):
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
    try:
        buffer = generate_report(doc_dict(snap, "prediction_id"))
    except Exception as e:
        raise HTTPException(500, f"Failed to generate report: {e}")
    return Response(content=buffer.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment; filename=report_{prediction_id}.pdf"})


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")
