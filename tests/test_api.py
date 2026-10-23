"""API integration tests. Endpoints that need auth/Firestore run against the Firebase
emulators (see tests/conftest.py) and are skipped otherwise."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from tests.conftest import needs_emulator

client = TestClient(app)
SAMPLES = Path(__file__).parent.parent / "samples"


def sample_upload():
    img = SAMPLES / "PNEUMONIA_person1664_virus_2877.jpeg"
    return {"file": (img.name, img.read_bytes(), "image/jpeg")}


def test_health():
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert {"demo_mode", "firebase", "time"} <= data.keys()


def test_model_info():
    data = client.get("/api/model-info").json()
    assert {"architecture", "gate", "states"} <= data.keys()
    for s in ("CLEARED", "FLAGGED_URGENT", "ESCALATED", "REJECTED"):
        assert s in data["states"]


def test_frontend_index():
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


@needs_emulator
class TestAuth:
    def test_requires_token(self):
        assert client.post("/api/predict", files=sample_upload()).status_code == 401
        assert client.get("/api/cases").status_code == 401

    def test_rejects_bad_token(self):
        r = client.get("/api/auth/me", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_me(self, clinician):
        r = client.get("/api/auth/me", headers=clinician["headers"])
        assert r.status_code == 200
        assert r.json()["role"] == "clinician"
        assert r.json()["user_id"] == clinician["uid"]

    def test_register_always_clinician(self):
        from tests.conftest import make_user
        u = make_user("clinician")
        from backend import firebase
        firebase.db().collection("users").document(u["uid"]).delete()
        r = client.post("/api/auth/register", headers=u["headers"],
                        json={"full_name": "Admin Wannabe", "hospital": "CHUK"})
        assert r.status_code == 200 and r.json()["role"] == "clinician"

    def test_role_checks(self, clinician, specialist):
        assert client.get("/api/admin/users", headers=clinician["headers"]).status_code == 403
        r = client.post("/api/predict", files=sample_upload(), headers=specialist["headers"])
        assert r.status_code == 403

    def test_profile_update(self, clinician):
        r = client.patch("/api/users/me/profile", headers=clinician["headers"],
                         json={"full_name": "Dr New Name", "hospital": "CHUK"})
        assert r.json()["full_name"] == "Dr New Name"
        assert client.get("/api/auth/me", headers=clinician["headers"]).json()["hospital"] == "CHUK"


@needs_emulator
class TestPredict:
    def test_invalid_file_type(self, clinician):
        r = client.post("/api/predict", headers=clinician["headers"],
                        files={"file": ("t.txt", b"not an image", "text/plain")})
        assert r.status_code == 400

    def test_large_file(self, clinician):
        r = client.post("/api/predict", headers=clinician["headers"],
                        files={"file": ("l.jpg", b"x" * (11 * 1024 * 1024), "image/jpeg")})
        assert r.status_code == 400

    def test_valid_image_stored(self, clinician):
        r = client.post("/api/predict", files=sample_upload(), headers=clinician["headers"])
        assert r.status_code == 200
        data = r.json()
        assert data["state"] in ("CLEARED", "FLAGGED_URGENT", "ESCALATED", "REJECTED")
        cases = client.get("/api/cases", headers=clinician["headers"]).json()["cases"]
        assert data["prediction_id"] in [c["prediction_id"] for c in cases]
        stats = client.get("/api/stats", headers=clinician["headers"]).json()
        assert stats["total"] >= 1

    def test_feedback(self, clinician):
        pid = client.post("/api/predict", files=sample_upload(),
                          headers=clinician["headers"]).json()["prediction_id"]
        r = client.post("/api/feedback", headers=clinician["headers"],
                        json={"prediction_id": pid, "confirmed_outcome": "pneumonia"})
        assert r.status_code == 200
        r = client.post("/api/feedback", headers=clinician["headers"],
                        json={"prediction_id": "missing", "confirmed_outcome": "normal"})
        assert r.status_code == 404

    def test_report(self, clinician):
        pid = client.post("/api/predict", files=sample_upload(),
                          headers=clinician["headers"]).json()["prediction_id"]
        r = client.get(f"/api/report/{pid}", headers=clinician["headers"])
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"


@needs_emulator
def test_escalation_workflow(clinician, specialist, admin):
    other = pytest.importorskip("tests.conftest").make_user("specialist")
    pat = client.post("/api/patients", headers=clinician["headers"],
                      json={"full_name": "Test Child", "age": 4, "gender": "F"}).json()
    pid = client.post(f"/api/predict?patient_id={pat['patient_id']}", files=sample_upload(),
                      headers=clinician["headers"]).json()["prediction_id"]

    specs = client.get("/api/specialists", headers=clinician["headers"]).json()["specialists"]
    assert specialist["uid"] in [s["user_id"] for s in specs]

    r = client.post("/api/escalations", headers=clinician["headers"],
                    json={"prediction_id": pid, "specialist_id": specialist["uid"], "note": "?"})
    assert r.status_code == 200
    esc_id = r.json()["escalation_id"]

    # assigned specialist sees it; another specialist does not and cannot act on it
    mine = client.get("/api/escalations", headers=specialist["headers"]).json()["escalations"]
    assert [e["escalation_id"] for e in mine] == [esc_id]
    assert mine[0]["patient_name"] == "Test Child"
    assert client.get("/api/escalations", headers=other["headers"]).json()["escalations"] == []
    assert client.post(f"/api/escalations/{esc_id}/start",
                       headers=other["headers"]).status_code == 403
    assert client.get(f"/api/patients/{pat['patient_id']}",
                      headers=other["headers"]).status_code == 404
    assert client.get(f"/api/report/{pid}", headers=other["headers"]).status_code == 404
    assert client.get(f"/api/report/{pid}", headers=specialist["headers"]).status_code == 200
    pats = client.get("/api/patients", headers=specialist["headers"]).json()["patients"]
    assert [p["patient_id"] for p in pats] == [pat["patient_id"]]

    assert client.post(f"/api/escalations/{esc_id}/start",
                       headers=specialist["headers"]).json()["status"] == "pending"
    r = client.post(f"/api/escalations/{esc_id}/resolve", headers=specialist["headers"],
                    json={"specialist_comment": "Consolidation RLL", "confirmed_outcome": "pneumonia"})
    assert r.status_code == 200

    sent = client.get("/api/escalations", headers=clinician["headers"]).json()["escalations"]
    assert sent[0]["status"] == "resolved" and sent[0]["confirmed_outcome"] == "pneumonia"

    detail = client.get(f"/api/patients/{pat['patient_id']}", headers=clinician["headers"]).json()
    assert detail["escalations"][0]["specialist_name"] == "Test Specialist"

    stats = client.get("/api/admin/stats", headers=admin["headers"]).json()
    assert stats["total_predictions"] >= 1
    logs = client.get("/api/admin/audit", headers=admin["headers"]).json()["logs"]
    assert any(l["action"].startswith("specialist_review:") for l in logs)


@needs_emulator
def test_admin_creates_user(admin):
    r = client.post("/api/admin/users", headers=admin["headers"],
                    json={"email": "new.spec@test.rw", "password": "password123",
                          "full_name": "Dr New", "role": "specialist"})
    assert r.status_code == 200
    users = client.get("/api/admin/users", headers=admin["headers"]).json()["users"]
    assert any(u["email"] == "new.spec@test.rw" and u["role"] == "specialist" for u in users)
    r = client.post("/api/admin/users", headers=admin["headers"],
                    json={"email": "new.spec@test.rw", "password": "password123",
                          "full_name": "Dr New", "role": "specialist"})
    assert r.status_code == 400
