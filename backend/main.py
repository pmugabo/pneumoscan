"""PneumoScan API.

    uvicorn backend.main:app --reload

Swagger UI at http://127.0.0.1:8000/docs, the clinician interface at /.

Endpoints
    GET  /api/health      service and model status
    GET  /api/model-info  architecture, gate thresholds, triage states
    POST /api/predict     upload an image, get a triage decision
    POST /api/feedback    record the confirmed outcome for a case
    GET  /api/cases       decision history for this session
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from .inference import engine

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DB = ROOT / "pneumoscan.db"
MAX_BYTES = 10 * 1024 * 1024
ALLOWED = {"image/jpeg", "image/png"}

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
        con.execute("""CREATE TABLE IF NOT EXISTS predictions (
            prediction_id TEXT PRIMARY KEY,
            filename TEXT, created_at TEXT,
            calibrated_prob REAL, ood_score REAL, triage_state TEXT,
            model_version TEXT, confirmed_outcome TEXT, comment TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id TEXT, action TEXT, timestamp TEXT)""")


init_db()


def log(prediction_id: str, action: str):
    with db() as con:
        con.execute("INSERT INTO audit_logs (prediction_id, action, timestamp)"
                    " VALUES (?,?,?)",
                    (prediction_id, action, datetime.now(timezone.utc).isoformat()))


class Feedback(BaseModel):
    prediction_id: str
    confirmed_outcome: str   # "pneumonia" | "normal" | "unknown"
    comment: str = ""


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": engine.demo,
            "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/model-info")
def model_info():
    return engine.info()


@app.post("/api/predict")
async def predict(file: UploadFile = File(...)):
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
    pid = str(uuid.uuid4())
    with db() as con:
        con.execute(
            "INSERT INTO predictions (prediction_id, filename, created_at,"
            " calibrated_prob, ood_score, triage_state, model_version)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, file.filename, datetime.now(timezone.utc).isoformat(),
             result.probability, result.ood_score, result.state,
             result.model_version))
    log(pid, f"triage:{result.state}")
    return {"prediction_id": pid, **result.to_dict()}


@app.post("/api/feedback")
def feedback(fb: Feedback):
    with db() as con:
        cur = con.execute(
            "UPDATE predictions SET confirmed_outcome=?, comment=?"
            " WHERE prediction_id=?",
            (fb.confirmed_outcome, fb.comment, fb.prediction_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Unknown prediction_id.")
    log(fb.prediction_id, f"feedback:{fb.confirmed_outcome}")
    return {"recorded": True}


@app.get("/api/cases")
def cases(limit: int = 25):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
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


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")
