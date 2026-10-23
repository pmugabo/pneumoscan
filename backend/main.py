"""PneumoScan API.

    uvicorn backend.main:app --reload

Swagger UI at http://127.0.0.1:8000/docs, the clinician interface at /.

Endpoints
    GET  /api/health      service and model status
    GET  /api/model-info  architecture, gate thresholds, triage states
    POST /api/predict     upload an image, get a triage decision
    POST /api/feedback    record the confirmed outcome for a case
    GET  /api/cases       decision history for this session
    POST /api/auth/login  user authentication
    POST /api/auth/register user registration
    GET  /api/admin/stats admin-only performance metrics
    GET  /api/admin/users admin-only user management
    POST /api/admin/users create new user
    GET  /api/admin/audit admin-only audit log
    GET  /api/report/{prediction_id} generate PDF report
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import base64
import io

from fastapi import FastAPI, File, HTTPException, UploadFile, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import timedelta

from .inference import engine
from .report import generate_report
import hashlib

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DB = ROOT / "pneumoscan.db"
MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/jpeg", "image/png"}

# Security
SECRET_KEY = "your-secret-key-change-in-production"  # In production, use environment variable
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# Simple password hashing (for capstone project - in production use bcrypt)
def get_password_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password

app = FastAPI(
    title="PneumoScan API",
    description=("Autonomous chest X-ray triage with a confidence gate. "
                 "Research prototype — not a certified diagnostic device."),
    version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'clinician',
            hospital TEXT,
            created_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1)""")
        con.execute("""CREATE TABLE IF NOT EXISTS patients (
            patient_id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            age INTEGER,
            gender TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            created_by INTEGER,
            FOREIGN KEY (created_by) REFERENCES users(user_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS predictions (
            prediction_id TEXT PRIMARY KEY,
            patient_id TEXT,
            filename TEXT, created_at TEXT,
            calibrated_prob REAL, ood_score REAL, triage_state TEXT,
            model_version TEXT, confirmed_outcome TEXT, comment TEXT,
            heatmap_png TEXT, image_png TEXT,
            user_id INTEGER,
            FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id TEXT, action TEXT, timestamp TEXT,
            user_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(user_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS escalations (
            escalation_id TEXT PRIMARY KEY,
            prediction_id TEXT NOT NULL,
            patient_id TEXT,
            status TEXT DEFAULT 'pending',
            assigned_to INTEGER,
            note TEXT,
            specialist_comment TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            created_by INTEGER,
            FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id),
            FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
            FOREIGN KEY (assigned_to) REFERENCES users(user_id),
            FOREIGN KEY (created_by) REFERENCES users(user_id))""")

        # Migration for existing databases: store the original X-ray image
        try:
            con.execute("ALTER TABLE predictions ADD COLUMN image_png TEXT")
        except sqlite3.OperationalError:
            pass

        # Seed demo specialists (only Dr. Ephrem Bizimana is active)
        seed_hash = hashlib.sha256("password123".encode()).hexdigest()
        for email, name, active in [
            ("ephrem.bizimana@pneumoscan.rw", "Dr. Ephrem Bizimana", 1),
            ("daniel.bitega@pneumoscan.rw", "Dr. Bitega Daniel", 0),
            ("josephine.ngarambe@pneumoscan.rw", "Dr. Josephine Ngarambe", 0)]:
            if not con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                con.execute(
                    "INSERT INTO users (email, password_hash, full_name, role,"
                    " hospital, created_at, is_active) VALUES (?,?,?,?,?,?,?)",
                    (email, seed_hash, name, "specialist",
                     "Kigali University Teaching Hospital",
                     datetime.now(timezone.utc).isoformat(), active))


init_db()


def log(prediction_id: str, action: str, user_id: Optional[int] = None):
    with db() as con:
        con.execute("INSERT INTO audit_logs (prediction_id, action, timestamp, user_id)"
                    " VALUES (?,?,?,?)",
                    (prediction_id, action, datetime.now(timezone.utc).isoformat(), user_id))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hashlib.sha256(plain_password.encode()).hexdigest() == hashed_password


def get_password_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def get_current_user(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            return None
    except JWTError:
        return None
    
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return user


class Feedback(BaseModel):
    prediction_id: str
    confirmed_outcome: str   # "pneumonia" | "normal" | "unknown"
    comment: str = ""


class UserLogin(BaseModel):
    email: str
    password: str


class UserRegister(BaseModel):
    email: str
    password: str
    full_name: str
    hospital: Optional[str] = None


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "clinician"  # "clinician", "specialist", or "admin"
    hospital: Optional[str] = None


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    hospital: Optional[str] = None


class PasswordChange(BaseModel):
    email: str
    current_password: str
    new_password: str


class Token(BaseModel):
    access_token: str
    token_type: str


class PatientCreate(BaseModel):
    full_name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    notes: Optional[str] = ""


class EscalationCreate(BaseModel):
    prediction_id: str
    specialist_id: int
    note: Optional[str] = ""


class EscalationResolve(BaseModel):
    specialist_comment: str
    confirmed_outcome: Optional[str] = None  # "pneumonia" | "normal" | "unknown"


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": engine.demo,
            "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/model-info")
def model_info():
    return engine.info()


@app.post("/api/predict")
async def predict(file: UploadFile = File(...), patient_id: Optional[str] = None, user_id: Optional[int] = None):
    if file.content_type not in ALLOWED:
        raise HTTPException(400, "Upload a JPEG or PNG image.")
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(400, "File larger than 10 MB.")
    try:
        img = Image.open(__import__("io").BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "That file could not be read as an image.")

    result = engine.predict(img)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    image_png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    pid = str(uuid.uuid4())
    with db() as con:
        con.execute(
            "INSERT INTO predictions (prediction_id, patient_id, filename, created_at,"
            " calibrated_prob, ood_score, triage_state, model_version, heatmap_png,"
            " image_png, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, patient_id, file.filename, datetime.now(timezone.utc).isoformat(),
             result.probability, result.ood_score, result.state,
             result.model_version, result.heatmap_png, image_png, user_id))
    log(pid, f"triage:{result.state}", user_id)
    return {"prediction_id": pid, "patient_id": patient_id, **result.to_dict()}


# Patient endpoints
@app.post("/api/patients")
def create_patient(patient: PatientCreate, user_id: Optional[int] = None):
    """Create a new patient profile."""
    patient_id = str(uuid.uuid4())[:8].upper()
    with db() as con:
        con.execute(
            "INSERT INTO patients (patient_id, full_name, age, gender, notes, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (patient_id, patient.full_name, patient.age, patient.gender,
             patient.notes, datetime.now(timezone.utc).isoformat(), user_id))
    return {"patient_id": patient_id, "message": "Patient created successfully"}


@app.get("/api/patients")
def list_patients(specialist_id: Optional[int] = None):
    """List patients. Specialists only see patients whose cases were sent to them."""
    with db() as con:
        query = """SELECT p.*,
                      COUNT(pr.prediction_id) as scan_count,
                      MAX(pr.created_at) as last_scan
               FROM patients p
               LEFT JOIN predictions pr ON p.patient_id = pr.patient_id"""
        params = []
        if specialist_id:
            query += """ WHERE p.patient_id IN
                (SELECT patient_id FROM escalations WHERE assigned_to = ?)"""
            params.append(specialist_id)
        query += " GROUP BY p.patient_id ORDER BY p.created_at DESC"
        patients = con.execute(query, params).fetchall()
    return {"patients": [dict(p) for p in patients]}


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str):
    """Get patient details with their prediction history."""
    with db() as con:
        patient = con.execute(
            "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        if not patient:
            raise HTTPException(404, "Patient not found")
        
        predictions = con.execute(
            """SELECT prediction_id, filename, created_at, calibrated_prob,
                      ood_score, triage_state, model_version, confirmed_outcome,
                      heatmap_png, image_png
               FROM predictions WHERE patient_id = ?
               ORDER BY created_at DESC""",
            (patient_id,)
        ).fetchall()
        
        escalations = con.execute(
            """SELECT e.*, u.full_name as specialist_name
               FROM escalations e
               LEFT JOIN users u ON e.assigned_to = u.user_id
               WHERE e.patient_id = ?""",
            (patient_id,)
        ).fetchall()
    
    return {
        "patient": dict(patient),
        "predictions": [dict(p) for p in predictions],
        "escalations": [dict(e) for e in escalations]
    }


# Escalation endpoints
@app.get("/api/specialists")
def list_specialists():
    """List all specialist users for the send-to-specialist dropdown."""
    with db() as con:
        rows = con.execute(
            "SELECT user_id, full_name, hospital, is_active FROM users"
            " WHERE role = 'specialist' ORDER BY full_name"
        ).fetchall()
    return {"specialists": [dict(r) for r in rows]}


@app.post("/api/escalations")
def create_escalation(esc: EscalationCreate, user_id: Optional[int] = None):
    """Clinician sends an escalated case to a chosen specialist."""
    escalation_id = str(uuid.uuid4())[:8].upper()
    with db() as con:
        pred = con.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?", (esc.prediction_id,)
        ).fetchone()
        if not pred:
            raise HTTPException(404, "Prediction not found")

        specialist = con.execute(
            "SELECT user_id, full_name, is_active FROM users WHERE user_id = ? AND role = 'specialist'",
            (esc.specialist_id,)
        ).fetchone()
        if not specialist:
            raise HTTPException(404, "Specialist not found")
        if not specialist["is_active"]:
            raise HTTPException(400, f"{specialist['full_name']} is not available")

        con.execute(
            """INSERT INTO escalations
               (escalation_id, prediction_id, patient_id, status, assigned_to,
                note, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (escalation_id, esc.prediction_id, pred["patient_id"], 'sent',
             esc.specialist_id, esc.note, datetime.now(timezone.utc).isoformat(), user_id))

    log(esc.prediction_id, f"escalated_to:{specialist['full_name']}", user_id)
    return {"escalation_id": escalation_id, "status": "sent",
            "assigned_to": esc.specialist_id,
            "specialist_name": specialist["full_name"]}


@app.post("/api/escalations/{escalation_id}/start")
def start_escalation_review(escalation_id: str, user_id: Optional[int] = None):
    """Specialist marks a sent case as under review (status -> pending)."""
    with db() as con:
        esc = con.execute(
            "SELECT * FROM escalations WHERE escalation_id = ?", (escalation_id,)
        ).fetchone()
        if not esc:
            raise HTTPException(404, "Escalation not found")
        if esc["status"] == 'sent':
            con.execute("UPDATE escalations SET status='pending' WHERE escalation_id=?",
                        (escalation_id,))

    if esc["status"] == 'sent':
        log(esc["prediction_id"], "specialist_review_started", user_id)
    return {"status": "pending"}


@app.get("/api/escalations")
def list_escalations(status: Optional[str] = None,
                     assigned_to: Optional[int] = None,
                     created_by: Optional[int] = None):
    """List escalations — filter by status, assigned specialist, or sending clinician."""
    with db() as con:
        query = """
            SELECT e.*, p.filename, p.calibrated_prob, p.ood_score,
                   p.triage_state, p.heatmap_png, p.image_png,
                   pt.full_name as patient_name, pt.age, pt.gender,
                   u.full_name as clinician_name,
                   s.full_name as specialist_name
            FROM escalations e
            JOIN predictions p ON e.prediction_id = p.prediction_id
            LEFT JOIN patients pt ON e.patient_id = pt.patient_id
            LEFT JOIN users u ON e.created_by = u.user_id
            LEFT JOIN users s ON e.assigned_to = s.user_id
        """
        clauses, params = [], []
        if status:
            clauses.append("e.status = ?")
            params.append(status)
        if assigned_to:
            clauses.append("e.assigned_to = ?")
            params.append(assigned_to)
        if created_by:
            clauses.append("e.created_by = ?")
            params.append(created_by)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY e.created_at DESC"
        rows = con.execute(query, params).fetchall()
    return {"escalations": [dict(r) for r in rows]}


@app.post("/api/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: str, resolution: EscalationResolve, user_id: Optional[int] = None):
    """Specialist resolves an escalated case."""
    with db() as con:
        esc = con.execute(
            "SELECT * FROM escalations WHERE escalation_id = ?", (escalation_id,)
        ).fetchone()
        if not esc:
            raise HTTPException(404, "Escalation not found")
        if esc["status"] == "resolved":
            raise HTTPException(400, "Escalation already resolved")
        
        con.execute(
            """UPDATE escalations SET status='resolved', specialist_comment=?,
               resolved_at=? WHERE escalation_id=?""",
            (resolution.specialist_comment, datetime.now(timezone.utc).isoformat(),
             escalation_id))

        # If specialist provided a confirmed outcome, update the prediction
        if resolution.confirmed_outcome:
            con.execute(
                "UPDATE predictions SET confirmed_outcome=? WHERE prediction_id=?",
                (resolution.confirmed_outcome, esc["prediction_id"]))

    log(esc["prediction_id"], f"specialist_review:{resolution.confirmed_outcome or 'reviewed'}", user_id)
    return {"resolved": True}


@app.post("/api/feedback")
def feedback(fb: Feedback, user_id: Optional[int] = None):
    with db() as con:
        cur = con.execute(
            "UPDATE predictions SET confirmed_outcome=?, comment=?"
            " WHERE prediction_id=?",
            (fb.confirmed_outcome, fb.comment, fb.prediction_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Unknown prediction_id.")
    log(fb.prediction_id, f"feedback:{fb.confirmed_outcome}", user_id)
    return {"recorded": True}


@app.get("/api/cases")
def cases(limit: int = 25, specialist_id: Optional[int] = None,
          user_id: Optional[int] = None):
    """List predictions. Optional scoping: specialist sees only cases sent to them,
    or filter by the clinician who ran the scan."""
    with db() as con:
        query = """SELECT p.*, pt.full_name as patient_name, pt.patient_id as pid,
                      u.full_name as clinician_name
               FROM predictions p
               LEFT JOIN patients pt ON p.patient_id = pt.patient_id
               LEFT JOIN users u ON p.user_id = u.user_id"""
        clauses, params = [], []
        if specialist_id:
            clauses.append("""p.prediction_id IN
                (SELECT prediction_id FROM escalations WHERE assigned_to = ?)""")
            params.append(specialist_id)
        if user_id:
            clauses.append("p.user_id = ?")
            params.append(user_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY p.created_at DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(query, params).fetchall()
    return {"count": len(rows), "cases": [dict(r) for r in rows]}


@app.get("/api/stats")
def stats():
    """Autonomy rate over everything this instance has decided."""
    with db() as con:
        rows = con.execute(
            "SELECT triage_state, COUNT(*) n FROM predictions"
            " GROUP BY triage_state").fetchall()
    counts = {r["triage_state"]: r["n"] for r in rows}
    total = sum(counts.values())
    auto = counts.get("CLEARED", 0) + counts.get("FLAGGED_URGENT", 0)
    return {"total": total, "by_state": counts,
            "autonomy_rate": (auto / total) if total else None}


# Authentication endpoints
@app.post("/api/auth/login")
def login(user_login: UserLogin):
    """Authenticate user and return JWT token."""
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE email = ?", (user_login.email,)).fetchone()
    
    if not user or not verify_password(user_login.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    
    if not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated"
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["email"]}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "hospital": user["hospital"],
            "created_at": user["created_at"]
        }
    }


@app.post("/api/auth/register")
def register(user_register: UserRegister):
    """Register a new user."""
    with db() as con:
        # Check if user already exists
        existing = con.execute("SELECT * FROM users WHERE email = ?", (user_register.email,)).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Assign role based on email keyword (demo): admin/specialist/clinician
        email_l = user_register.email.lower()
        if "admin" in email_l:
            role = "admin"
        elif "specialist" in email_l:
            role = "specialist"
        else:
            role = "clinician"
        password_hash = get_password_hash(user_register.password)
        con.execute(
            "INSERT INTO users (email, password_hash, full_name, role, hospital, created_at) VALUES (?,?,?,?,?,?)",
            (user_register.email, password_hash, user_register.full_name, role,
             user_register.hospital, datetime.now(timezone.utc).isoformat())
        )
    
    return {"message": "User registered successfully", "role": role}


# Profile endpoints
@app.get("/api/users/{email}/profile")
def get_profile(email: str):
    """Return a user's public profile."""
    with db() as con:
        user = con.execute(
            "SELECT user_id, email, full_name, role, hospital, created_at, is_active"
            " FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, "User not found")
    return dict(user)


@app.patch("/api/users/{email}/profile")
def update_profile(email: str, update: ProfileUpdate):
    """Update a user's name or hospital."""
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, "User not found")
        name = update.full_name if update.full_name else user["full_name"]
        hospital = update.hospital if update.hospital is not None else user["hospital"]
        con.execute("UPDATE users SET full_name=?, hospital=? WHERE email=?",
                    (name, hospital, email))
    return {"updated": True, "full_name": name, "hospital": hospital}


@app.post("/api/users/password")
def change_password(pw: PasswordChange):
    """Change a user's password after verifying the current one."""
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE email = ?", (pw.email,)).fetchone()
        if not user or not verify_password(pw.current_password, user["password_hash"]):
            raise HTTPException(401, "Current password is incorrect")
        if len(pw.new_password) < 8:
            raise HTTPException(400, "New password must be at least 8 characters")
        con.execute("UPDATE users SET password_hash=? WHERE email=?",
                    (get_password_hash(pw.new_password), pw.email))
    return {"changed": True}


# Admin endpoints
@app.get("/api/admin/stats")
def admin_stats():
    """Admin-only: comprehensive performance metrics."""
    with db() as con:
        # Total predictions by state
        state_counts = con.execute(
            "SELECT triage_state, COUNT(*) as n FROM predictions GROUP BY triage_state"
        ).fetchall()
        by_state = {r["triage_state"]: r["n"] for r in state_counts}
        
        # Total predictions
        total = sum(by_state.values())
        
        # Autonomy rate
        auto = by_state.get("CLEARED", 0) + by_state.get("FLAGGED_URGENT", 0)
        autonomy_rate = (auto / total) if total else None
        
        # Abstention rate
        abstained = by_state.get("ESCALATED", 0) + by_state.get("REJECTED", 0)
        abstention_rate = (abstained / total) if total else None
        
        # By model version
        model_counts = con.execute(
            "SELECT model_version, COUNT(*) as n FROM predictions GROUP BY model_version"
        ).fetchall()
        by_model = {r["model_version"]: r["n"] for r in model_counts}
        
        # By user
        user_counts = con.execute(
            "SELECT u.full_name, COUNT(p.prediction_id) as n FROM predictions p "
            "JOIN users u ON p.user_id = u.user_id GROUP BY u.user_id"
        ).fetchall()
        by_user = {r["full_name"]: r["n"] for r in user_counts}
        
        # Confirmed outcomes
        outcome_counts = con.execute(
            "SELECT confirmed_outcome, COUNT(*) as n FROM predictions "
            "WHERE confirmed_outcome IS NOT NULL GROUP BY confirmed_outcome"
        ).fetchall()
        by_outcome = {r["confirmed_outcome"]: r["n"] for r in outcome_counts}
        
        return {
            "total_predictions": total,
            "by_state": by_state,
            "autonomy_rate": autonomy_rate,
            "abstention_rate": abstention_rate,
            "by_model": by_model,
            "by_user": by_user,
            "by_outcome": by_outcome
        }


@app.get("/api/admin/users")
def get_users():
    """Admin-only: list all users."""
    with db() as con:
        users = con.execute(
            "SELECT user_id, email, full_name, role, hospital, created_at, is_active FROM users"
        ).fetchall()
    return {"users": [dict(u) for u in users]}


@app.post("/api/admin/users")
def create_user(user_data: UserCreate):
    """Admin-only: create a new user."""
    with db() as con:
        # Check if user already exists
        existing = con.execute("SELECT * FROM users WHERE email = ?", (user_data.email,)).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Validate role
        if user_data.role not in ["clinician", "admin", "specialist"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role must be 'clinician', 'specialist' or 'admin'"
            )
        
        # Create new user
        password_hash = get_password_hash(user_data.password)
        con.execute(
            "INSERT INTO users (email, password_hash, full_name, role, hospital, created_at) VALUES (?,?,?,?,?,?)",
            (user_data.email, password_hash, user_data.full_name, user_data.role,
             user_data.hospital, datetime.now(timezone.utc).isoformat())
        )
    
    return {"message": "User created successfully"}


@app.get("/api/admin/audit")
def get_audit_logs(limit: int = 100):
    """Admin-only: view audit log."""
    with db() as con:
        logs = con.execute(
            """SELECT al.*, u.full_name, u.email 
               FROM audit_logs al 
               LEFT JOIN users u ON al.user_id = u.user_id 
               ORDER BY al.timestamp DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return {"logs": [dict(l) for l in logs]}


@app.get("/api/report/{prediction_id}")
def generate_report_endpoint(prediction_id: str):
    """Generate PDF report for a prediction."""
    with db() as con:
        prediction = con.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?", (prediction_id,)
        ).fetchone()
    
    if not prediction:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    try:
        # Convert sqlite row to dict
        prediction_dict = dict(prediction)
        buffer = generate_report(prediction_dict)
        
        return Response(
            content=buffer.getvalue(),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=report_{prediction_id}.pdf"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate report: {str(e)}")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")
