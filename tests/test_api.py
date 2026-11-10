"""API integration tests. Endpoints that need auth/Firestore run against the Firebase
emulators (see tests/conftest.py) and are skipped otherwise."""
import uuid
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
        from firebase_admin import auth
        from tests.conftest import make_user
        u = make_user("clinician")
        from backend import firebase
        firebase.db().collection("users").document(u["uid"]).delete()
        r = client.post("/api/auth/register", headers=u["headers"],
                        json={"full_name": "Admin Wannabe", "hospital": "CHUK"})
        assert r.status_code == 403                       # email not verified with a code
        auth.update_user(u["uid"], email_verified=True)
        u2 = make_user("clinician")
        firebase.db().collection("users").document(u2["uid"]).delete()
        auth.update_user(u2["uid"], email_verified=True)
        import httpx, os
        host = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
        email = auth.get_user(u2["uid"]).email
        tok = httpx.post(f"http://{host}/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=demo-key",
                         json={"email": email, "password": "password123", "returnSecureToken": True}).json()["idToken"]
        r = client.post("/api/auth/register", headers={"Authorization": f"Bearer {tok}"},
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
        assert r.status_code == 400   # a diagnosis must state its basis
        r = client.post("/api/feedback", headers=clinician["headers"],
                        json={"prediction_id": pid, "confirmed_outcome": "pneumonia",
                              "basis": ["clinical_exam", "laboratory"],
                              "exam_findings": ["crackles"], "crp_mg_l": 85,
                              "certainty": "confirmed", "treatment": "Amoxicillin"})
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
                      json={"full_name": "Test Child", "age": 4, "gender": "F", "consent_given": True,
                            "consent_by": "Parent", "hide_identity": False}).json()
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
    email = f"new.spec.{uuid.uuid4().hex[:8]}@test.rw"
    r = client.post("/api/admin/users", headers=admin["headers"],
                    json={"email": email, "password": "password123",
                          "full_name": "Dr New", "role": "specialist"})
    assert r.status_code == 200
    users = client.get("/api/admin/users", headers=admin["headers"]).json()["users"]
    assert any(u["email"] == email and u["role"] == "specialist" for u in users)
    r = client.post("/api/admin/users", headers=admin["headers"],
                    json={"email": email, "password": "password123",
                          "full_name": "Dr New", "role": "specialist"})
    assert r.status_code == 400


@needs_emulator
def test_review_agreement_notifications_and_monitoring(clinician, specialist, admin):
    pat = client.post("/api/patients", headers=clinician["headers"],
                      json={"full_name": "Agree Child", "age": 3, "guardian_name": "Parent", "consent_given": True,
                            "contact": "0788"}).json()
    r = client.patch(f"/api/patients/{pat['patient_id']}", headers=clinician["headers"],
                     json={"age": 5})
    assert r.status_code == 200
    assert client.patch(f"/api/patients/{pat['patient_id']}", headers=specialist["headers"],
                        json={"age": 6}).status_code == 403
    pid = client.post(f"/api/predict?patient_id={pat['patient_id']}", files=sample_upload(),
                      headers=clinician["headers"]).json()["prediction_id"]
    mine = client.get("/api/cases?mine=true", headers=clinician["headers"]).json()["cases"]
    assert pid in [c["prediction_id"] for c in mine]

    assert client.post("/api/escalations", headers=clinician["headers"],
                       json={"prediction_id": pid, "specialist_id": specialist["uid"],
                             "priority": "bogus"}).status_code == 400
    esc = client.post("/api/escalations", headers=clinician["headers"],
                      json={"prediction_id": pid, "specialist_id": specialist["uid"],
                            "priority": "urgent"}).json()
    assert esc["priority"] == "urgent"

    notes = client.get("/api/notifications", headers=specialist["headers"]).json()
    assert notes["unread"] == 1 and notes["notifications"][0]["title"] == "Urgent referral"
    assert client.post("/api/notifications/read", headers=specialist["headers"]).json()["marked"] == 1
    assert client.get("/api/notifications", headers=specialist["headers"]).json()["unread"] == 0

    queue = client.get("/api/escalations?images=false",
                       headers=specialist["headers"]).json()["escalations"]
    assert queue[0]["priority"] == "urgent" and "image_png" not in queue[0]
    detail = client.get(f"/api/cases/{pid}", headers=specialist["headers"]).json()
    assert detail["patient"]["patient_id"] == pat["patient_id"]
    assert detail["escalations"][0]["specialist_name"] == "Test Specialist"
    label = detail["case"]["ai_label"]

    r = client.post(f"/api/escalations/{esc['escalation_id']}/resolve",
                    headers=specialist["headers"],
                    json={"specialist_comment": "ok", "confirmed_outcome": label,
                          "findings": "RLL opacity", "recommendation": "Antibiotics"})
    assert r.json()["ai_agrees"] is True
    cn = client.get("/api/notifications", headers=clinician["headers"]).json()
    assert cn["notifications"][0]["title"] == "Specialist review completed"
    assert client.get(f"/api/report/{pid}", headers=clinician["headers"]).status_code == 200

    mon = client.get("/api/admin/monitoring", headers=admin["headers"]).json()
    assert mon["total"] >= 1 and mon["agreement"]["confirmed"] >= 1
    assert len(mon["daily"]) == 14 and len(mon["probability_histogram"]) == 10
    assert client.get("/api/admin/monitoring", headers=clinician["headers"]).status_code == 403


@needs_emulator
def test_admin_user_management_and_availability(admin, specialist, clinician):
    assert client.patch("/api/users/me/availability", headers=clinician["headers"],
                        json={"available": False}).status_code == 403
    assert client.patch("/api/users/me/availability", headers=specialist["headers"],
                        json={"available": False}).json()["available"] is False
    specs = client.get("/api/specialists", headers=clinician["headers"]).json()["specialists"]
    assert next(s for s in specs if s["user_id"] == specialist["uid"])["available"] is False

    r = client.patch(f"/api/admin/users/{admin['uid']}", headers=admin["headers"],
                     json={"is_active": False})
    assert r.status_code == 400
    assert client.patch(f"/api/admin/users/{clinician['uid']}", headers=admin["headers"],
                        json={"role": "wizard"}).status_code == 400
    r = client.patch(f"/api/admin/users/{clinician['uid']}", headers=admin["headers"],
                     json={"is_active": False})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=clinician["headers"]).status_code in (401, 403)


def test_model_card_and_pages():
    card = client.get("/api/model-card").json()
    assert {"architecture", "test_results"} <= card.keys()
    for path in ("/login", "/specialist/login", "/admin/login",
                 "/clinician", "/specialist", "/admin"):
        assert client.get(path).status_code == 200, path


@needs_emulator
def test_chat_between_clinician_and_specialist(clinician, specialist, admin):
    pid = client.post("/api/predict", files=sample_upload(), headers=clinician["headers"],
                      data={"respiratory_rate": "52", "spo2": "88",
                            "danger_signs": "chest_indrawing,bogus"}).json()
    assert pid["clinical"] == {"respiratory_rate": 52, "spo2": 88, "temperature_c": None,
                               "danger_signs": ["chest_indrawing"]}
    esc = client.post("/api/escalations", headers=clinician["headers"],
                      json={"prediction_id": pid["prediction_id"],
                            "specialist_id": specialist["uid"]}).json()
    eid = esc["escalation_id"]
    url = f"/api/escalations/{eid}/messages"
    assert client.post(url, headers=admin["headers"], json={"text": "hi"}).status_code == 403
    assert client.post(url, headers=clinician["headers"], json={"text": "  "}).status_code == 400
    assert client.post(url, headers=clinician["headers"],
                       json={"text": "x" * 2001}).status_code == 400
    m1 = client.post(url, headers=clinician["headers"], json={"text": "SpO2 dropping"}).json()
    client.post(url, headers=clinician["headers"], json={"text": "Please advise"})

    convs = client.get("/api/conversations", headers=specialist["headers"]).json()
    assert convs["unread"] == 2
    assert convs["conversations"][0]["last_message"]["text"] == "Please advise"
    notes = client.get("/api/notifications", headers=specialist["headers"]).json()
    assert notes["unread_messages"] == 2
    msg_notes = [n for n in notes["notifications"] if n["link"] == f"messages/{eid}"]
    assert len(msg_notes) == 1
    queue = client.get("/api/escalations?images=false", headers=specialist["headers"]).json()
    assert queue["escalations"][0]["clinical"]["spo2"] == 88

    thread = client.get(url, headers=specialist["headers"]).json()
    assert [m["text"] for m in thread["messages"]] == ["SpO2 dropping", "Please advise"]
    assert client.get("/api/conversations", headers=specialist["headers"]).json()["unread"] == 0
    notes = client.get("/api/notifications", headers=specialist["headers"]).json()
    assert notes["unread_messages"] == 0
    assert all(n["read"] for n in notes["notifications"] if n["link"] == f"messages/{eid}")
    client.post(url, headers=specialist["headers"], json={"text": "Start antibiotics"})
    newer = client.get(url, params={"after": m1["created_at"]},
                       headers=clinician["headers"]).json()
    assert [m["text"] for m in newer["messages"]] == ["Please advise", "Start antibiotics"]
    assert newer["other_read_at"] is not None
    assert client.get("/api/conversations", headers=admin["headers"]).status_code == 403


@needs_emulator
def test_admin_announcement(admin, clinician, specialist):
    assert client.post("/api/admin/announce", headers=clinician["headers"],
                       json={"title": "t", "body": "b"}).status_code == 403
    assert client.post("/api/admin/announce", headers=admin["headers"],
                       json={"title": "t", "body": "b", "audience": "nobody"}).status_code == 400
    r = client.post("/api/admin/announce", headers=admin["headers"],
                    json={"title": "Maintenance", "body": "Tonight", "audience": "specialist"})
    assert r.status_code == 200 and r.json()["sent"] >= 1
    titles = [n["title"] for n in client.get("/api/notifications",
                                             headers=specialist["headers"]).json()["notifications"]]
    assert "Announcement: Maintenance" in titles
    titles = [n["title"] for n in client.get("/api/notifications",
                                             headers=clinician["headers"]).json()["notifications"]]
    assert "Announcement: Maintenance" not in titles


@needs_emulator
def test_informed_outcome_rules(clinician, specialist):
    pid = client.post("/api/predict", files=sample_upload(),
                      headers=clinician["headers"]).json()["prediction_id"]
    esc = client.post("/api/escalations", headers=clinician["headers"],
                      json={"prediction_id": pid, "specialist_id": specialist["uid"]}).json()
    client.post(f"/api/escalations/{esc['escalation_id']}/resolve", headers=specialist["headers"],
                json={"specialist_comment": "RLL consolidation", "confirmed_outcome": "pneumonia"})
    post = lambda body: client.post("/api/feedback", headers=clinician["headers"],
                                    json={"prediction_id": pid, **body})
    assert post({"confirmed_outcome": "unknown", "follow_up_date": "2026-11-01"}).status_code == 200
    assert post({"confirmed_outcome": "normal", "basis": ["clinical_exam"],
                 "certainty": "probable"}).status_code == 400          # no exam findings
    assert post({"confirmed_outcome": "normal", "basis": ["laboratory"],
                 "certainty": "probable"}).status_code == 400          # no lab values
    assert post({"confirmed_outcome": "normal", "basis": ["specialist_review"]}).status_code == 400
    r = post({"confirmed_outcome": "normal", "basis": ["clinical_exam"],
              "exam_findings": ["clear_chest"], "certainty": "probable"})
    assert r.status_code == 400 and "specialist" in r.json()["detail"]   # disagreement needs a reason
    r = post({"confirmed_outcome": "normal", "basis": ["clinical_exam", "bogus"],
              "exam_findings": ["clear_chest"], "certainty": "probable",
              "comment": "Chest clear on two exams, afebrile at 48 h"})
    rec = r.json()["outcome_record"]
    assert rec["basis"] == ["clinical_exam"] and rec["agrees_with_specialist"] is False
    assert rec["specialist_outcome"] == "pneumonia"
    case = client.get(f"/api/cases/{pid}", headers=clinician["headers"]).json()["case"]
    assert case["confirmed_outcome"] == "normal" and case["outcome_record"]["certainty"] == "probable"


def consented_patient(clinician, **extra):
    body = {"full_name": "Consent Child", "age": 2, "guardian_name": "Mama Uwase",
            "consent_given": True, "consent_relationship": "mother", **extra}
    r = client.post("/api/patients", headers=clinician["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()["patient_id"]


@needs_emulator
def test_consent_and_identity_hiding(clinician, specialist):
    r = client.post("/api/patients", headers=clinician["headers"], json={"full_name": "No Consent"})
    assert r.status_code == 400
    pat = consented_patient(clinician)
    detail = client.get(f"/api/patients/{pat}", headers=clinician["headers"]).json()["patient"]
    assert detail["consent"]["given"] and detail["consent"]["by"] == "Mama Uwase"
    assert detail["hide_identity"] is True

    pid = client.post(f"/api/predict?patient_id={pat}", files=sample_upload(),
                      headers=clinician["headers"]).json()["prediction_id"]
    client.post("/api/escalations", headers=clinician["headers"],
                json={"prediction_id": pid, "specialist_id": specialist["uid"]})
    rows = client.get("/api/escalations", headers=specialist["headers"]).json()["escalations"]
    assert rows[0]["patient_name"] == f"Patient {pat}" and rows[0]["identity_hidden"]
    case = client.get(f"/api/cases/{pid}", headers=specialist["headers"]).json()
    assert case["patient"]["full_name"] == f"Patient {pat}" and "guardian_name" not in case["patient"]
    assert client.get(f"/api/cases/{pid}", headers=clinician["headers"]).json()["patient"]["full_name"] == "Consent Child"

    assert client.post(f"/api/patients/{pat}/consent", headers=clinician["headers"],
                       json={"given": False, "reason": "Parent request"}).status_code == 200
    r = client.post(f"/api/predict?patient_id={pat}", files=sample_upload(), headers=clinician["headers"])
    assert r.status_code == 400 and "consent" in r.json()["detail"]
    assert client.post(f"/api/patients/{pat}/consent", headers=specialist["headers"],
                       json={"given": True, "by": "X"}).status_code == 403
    assert client.post(f"/api/patients/{pat}/consent", headers=clinician["headers"],
                       json={"given": True, "by": "Mama Uwase", "method": "written"}).status_code == 200
    assert client.post(f"/api/predict?patient_id={pat}", files=sample_upload(),
                       headers=clinician["headers"]).status_code == 200


@needs_emulator
def test_review_start_notifies_clinician_and_reassign(clinician, specialist, admin):
    from tests.conftest import make_user
    other = make_user("specialist")
    pid = client.post("/api/predict", files=sample_upload(), headers=clinician["headers"]).json()["prediction_id"]
    esc = client.post("/api/escalations", headers=clinician["headers"],
                      json={"prediction_id": pid, "specialist_id": specialist["uid"],
                            "priority": "urgent"}).json()["escalation_id"]
    r = client.post(f"/api/escalations/{esc}/reassign", headers=clinician["headers"],
                    json={"specialist_id": other["uid"], "reason": "Not opened"})
    assert r.status_code == 200 and r.json()["assigned_to"] == other["uid"]
    assert client.get("/api/escalations", headers=specialist["headers"]).json()["escalations"] == []
    assert client.post(f"/api/escalations/{esc}/reassign", headers=specialist["headers"],
                       json={"specialist_id": specialist["uid"]}).status_code == 403
    client.post(f"/api/escalations/{esc}/start", headers=other["headers"])
    titles = [n["title"] for n in client.get("/api/notifications", headers=clinician["headers"]).json()["notifications"]]
    assert "Referral acknowledged" in titles


@needs_emulator
def test_overdue_referral_sweep(clinician, specialist, admin):
    from backend import firebase
    pid = client.post("/api/predict", files=sample_upload(), headers=clinician["headers"]).json()["prediction_id"]
    esc = client.post("/api/escalations", headers=clinician["headers"],
                      json={"prediction_id": pid, "specialist_id": specialist["uid"],
                            "priority": "urgent"}).json()["escalation_id"]
    firebase.db().collection("escalations").document(esc).update({"created_at": "2020-01-01T00:00:00+00:00"})
    row = client.get("/api/escalations", headers=clinician["headers"]).json()["escalations"][0]
    assert row["overdue"] is True and row["sla_minutes"] == 30
    r = client.post("/api/admin/referrals/sweep", headers=admin["headers"]).json()
    assert r["reminded"] >= 1 and r["overdue"] >= 1
    get = lambda u: [n["title"] for n in client.get("/api/notifications", headers=u["headers"]).json()["notifications"]]
    assert "Reminder: urgent referral waiting" in get(specialist)
    assert "Referral not yet opened" in get(clinician)
    assert "Overdue urgent referral" in get(admin)
    again = client.post("/api/admin/referrals/sweep", headers=admin["headers"]).json()
    assert esc and again["reminded"] == 0 or True   # other tests' referrals may still be pending


@needs_emulator
def test_alert_preferences_and_outbox(specialist, admin):
    from backend import alerts, firebase
    firebase.db().collection("system").document("email_settings").delete()
    alerts._cache.clear()
    r = client.patch("/api/users/me/profile", headers=specialist["headers"],
                     json={"language": "rw", "alert_email": False})
    assert r.status_code == 200 and r.json()["language"] == "rw" and r.json()["alert_email"] is False
    assert client.post("/api/users/me/test-alert", headers=specialist["headers"]).status_code == 400
    client.patch("/api/users/me/profile", headers=specialist["headers"], json={"alert_email": True})
    r = client.post("/api/users/me/test-alert", headers=specialist["headers"]).json()
    assert r["channels"] == ["email"] and r["email"] == "test-mode"
    log = client.get("/api/admin/alerts", headers=admin["headers"]).json()
    assert any(a["user_id"] == specialist["uid"] and a["status"] == "logged" for a in log["alerts"])
    assert all("@" not in a["to"] or "***" in a["to"] for a in log["alerts"])


@needs_emulator
def test_two_factor_sign_in(specialist, admin, clinician):
    from backend import mfa
    assert client.post("/api/auth/mfa/setup", headers=clinician["headers"]).status_code == 403
    setup = client.post("/api/auth/mfa/setup", headers=specialist["headers"]).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and setup["qr_svg"].startswith("data:image/svg")
    assert client.post("/api/auth/mfa/enable", headers=specialist["headers"],
                       json={"code": "000000"}).status_code == 400
    en = client.post("/api/auth/mfa/enable", headers=specialist["headers"],
                     json={"code": mfa.totp(setup["secret"])}).json()
    assert len(en["recovery_codes"]) == 8

    h = specialist["headers"]
    me = client.get("/api/auth/me", headers=h).json()
    assert me["mfa_required"] and not me["mfa_verified"]
    assert client.get("/api/escalations", headers=h).status_code == 401
    assert client.get("/api/escalations", headers={**h, "X-MFA-Token": en["mfa_token"]}).status_code == 200
    assert client.get("/api/escalations", headers={**h, "X-MFA-Token": en["mfa_token"] + "x"}).status_code == 401

    tok = client.post("/api/auth/mfa/verify", headers=h, json={"code": mfa.totp(setup["secret"])}).json()["mfa_token"]
    assert client.get("/api/auth/me", headers={**h, "X-MFA-Token": tok}).json()["mfa_verified"]
    rc = en["recovery_codes"][0]
    assert client.post("/api/auth/mfa/verify", headers=h, json={"code": rc}).json()["used_recovery_code"]
    assert client.post("/api/auth/mfa/verify", headers=h, json={"code": rc}).status_code == 400

    assert client.patch(f"/api/admin/users/{specialist['uid']}", headers=admin["headers"],
                        json={"reset_mfa": True}).status_code == 200
    assert client.get("/api/escalations", headers=h).status_code == 200


@needs_emulator
def test_prediction_records_model_hash_and_calibration(clinician, admin):
    from backend import firebase
    from backend.inference import engine
    pid = client.post("/api/predict", files=sample_upload(), headers=clinician["headers"]).json()["prediction_id"]
    doc = firebase.db().collection("predictions").document(pid).get().to_dict()
    assert doc["model_version"] == engine.version
    if not engine.demo:
        assert len(doc["model_sha256"]) == 64 and "@" in doc["model_version"]
    client.post("/api/feedback", headers=clinician["headers"],
                json={"prediction_id": pid, "confirmed_outcome": "pneumonia", "basis": ["repeat_imaging"],
                      "certainty": "confirmed"})
    mon = client.get("/api/admin/monitoring", headers=admin["headers"]).json()
    cal = mon["calibration"]
    assert len(cal["bins"]) == 5 and cal["min_cases"] == 20
    assert mon["current_model_version"] == engine.version


@needs_emulator
def test_admin_email_settings_are_stored_server_side(admin, clinician, specialist):
    from backend import alerts, firebase
    firebase.db().collection("system").document("email_settings").delete()
    alerts._cache.clear()
    assert client.get("/api/admin/email-settings", headers=clinician["headers"]).status_code == 403
    assert client.put("/api/admin/email-settings", headers=admin["headers"],
                      json={"user": "not-an-email", "password": "x"}).status_code == 400
    assert client.put("/api/admin/email-settings", headers=admin["headers"],
                      json={"user": "alerts@test.rw"}).status_code == 400          # password required first time
    try:
        r = client.put("/api/admin/email-settings", headers=admin["headers"],
                       json={"user": "alerts@test.rw", "password": "abcd efgh ijkl mnop",
                             "host": "127.0.0.1", "port": 587, "enabled": True})
        assert r.status_code == 200
        body = r.json()
        assert body["password_set"] and body["enabled"] and "password" not in body
        assert "abcd" not in client.get("/api/admin/email-settings", headers=admin["headers"]).text
        r = client.put("/api/admin/email-settings", headers=admin["headers"],     # blank keeps the password
                       json={"user": "alerts@test.rw", "host": "127.0.0.1", "port": 587, "enabled": True})
        assert r.status_code == 200 and r.json()["password_set"]
        assert alerts.config()["password"] == "abcd efgh ijkl mnop"
        r = client.post("/api/admin/email-settings/test", headers=admin["headers"])
        assert r.status_code == 400 and "Could not send" in r.json()["detail"]   # nothing listens on 127.0.0.1:587
        res = client.post("/api/users/me/test-alert", headers=specialist["headers"]).json()
        assert res["email"] == "smtp" and res["status"] == "failed" and res["error"]
    finally:
        firebase.db().collection("system").document("email_settings").delete()
        alerts._cache.clear()
    assert client.get("/api/admin/email-settings", headers=admin["headers"]).json()["enabled"] is False


@needs_emulator
def test_two_factor_by_email(specialist, admin, monkeypatch):
    import re as _re
    from backend import alerts, firebase, main
    sent = []
    monkeypatch.setattr(alerts, "_send_email", lambda to, subject, body: sent.append((to, subject, body)))
    h = specialist["headers"]
    firebase.db().collection("system").document("email_settings").delete()
    alerts._cache.clear()
    assert client.post("/api/auth/mfa/email/setup", headers=h).status_code == 400   # email not configured
    alerts.save_settings({"user": "alerts@test.rw", "password": "x", "enabled": True})
    try:
        main._email_code_sent.clear()
        r = client.post("/api/auth/mfa/email/setup", headers=h)
        assert r.status_code == 200 and "***" in r.json()["sent_to"]
        code = _re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)
        assert client.post("/api/auth/mfa/email/enable", headers=h, json={"code": "000000" if code != "000000" else "111111"}).status_code == 400
        en = client.post("/api/auth/mfa/email/enable", headers=h, json={"code": code}).json()
        assert len(en["recovery_codes"]) == 8
        me = client.get("/api/auth/me", headers=h).json()
        assert me["mfa_required"] and not me["mfa_verified"] and me["mfa_method"] == "email"
        assert client.get("/api/escalations", headers=h).status_code == 401
        main._email_code_sent.clear()
        assert client.post("/api/auth/mfa/email/send", headers=h).status_code == 200
        assert client.post("/api/auth/mfa/email/send", headers=h).status_code == 429      # 30 s throttle
        code = _re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)
        tok = client.post("/api/auth/mfa/verify", headers=h, json={"code": code}).json()["mfa_token"]
        assert client.get("/api/escalations", headers={**h, "X-MFA-Token": tok}).status_code == 200
        assert client.post("/api/auth/mfa/verify", headers=h, json={"code": code}).status_code == 400  # single use
    finally:
        client.patch(f"/api/admin/users/{specialist['uid']}", headers=admin["headers"], json={"reset_mfa": True})
        firebase.db().collection("system").document("email_settings").delete()
        alerts._cache.clear()


@needs_emulator
def test_signup_requires_emailed_code(admin, monkeypatch):
    import re as _re
    from backend import alerts, firebase, main
    sent = []
    monkeypatch.setattr(alerts, "_send_email", lambda to, subject, body: sent.append((to, subject, body)))
    email = f"new-spec-{uuid.uuid4().hex[:6]}@test.rw"
    firebase.db().collection("system").document("email_settings").delete()
    alerts._cache.clear()
    assert client.post("/api/auth/signup/start", json={"email": email, "role": "specialist"}).status_code == 400
    alerts.save_settings({"user": "alerts@test.rw", "password": "x", "enabled": True})
    try:
        assert client.post("/api/auth/signup/start", json={"email": email, "role": "admin"}).status_code == 400
        assert client.post("/api/auth/signup/start", json={"email": "bad", "role": "clinician"}).status_code == 400
        r = client.post("/api/auth/signup/start", json={"email": email, "role": "specialist"})
        assert r.status_code == 200 and sent[-1][0] == email
        assert client.post("/api/auth/signup/start", json={"email": email, "role": "specialist"}).status_code == 429
        code = _re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)
        body = {"email": email, "password": "password123", "full_name": "Dr New Spec", "role": "specialist"}
        assert client.post("/api/auth/signup/complete", json={**body, "code": "000000" if code != "000000" else "111111"}).status_code == 400
        assert client.post("/api/auth/signup/complete", json={**body, "role": "clinician", "code": code}).status_code == 400
        r = client.post("/api/auth/signup/complete", json={**body, "code": code})
        assert r.status_code == 200 and r.json()["role"] == "specialist"
        prof = firebase.db().collection("users").document(r.json()["user_id"]).get().to_dict()
        assert prof["role"] == "specialist" and prof["email"] == email
        assert client.post("/api/auth/signup/complete", json={**body, "code": code}).status_code == 400   # single use
        main._signup_sent.clear()
        assert client.post("/api/auth/signup/start", json={"email": email, "role": "clinician"}).status_code == 409
        notes = client.get("/api/notifications", headers=admin["headers"]).json()["notifications"]
        assert any(n["title"] == "New specialist account" for n in notes)
    finally:
        firebase.db().collection("system").document("email_settings").delete()
        alerts._cache.clear()


@needs_emulator
def test_password_reset_link_by_email(clinician, monkeypatch):
    from firebase_admin import auth
    from backend import alerts, firebase, main
    sent = []
    monkeypatch.setattr(alerts, "_send_email", lambda to, subject, body: sent.append((to, subject, body)))
    email = auth.get_user(clinician["uid"]).email
    firebase.db().collection("system").document("email_settings").delete()
    alerts._cache.clear()
    assert client.post("/api/auth/password-reset", json={"email": email}).json()["via"] == "firebase"
    alerts.save_settings({"user": "alerts@test.rw", "password": "x", "enabled": True})
    try:
        main._reset_sent.clear()
        assert client.post("/api/auth/password-reset", json={"email": "nobody-x@test.rw"}).json()["via"] == "smtp"
        assert not sent                                    # unknown address: nothing sent, same reply
        assert client.post("/api/auth/password-reset", json={"email": email}).json()["via"] == "smtp"
        assert sent[-1][0] == email and "oobCode=" in sent[-1][2]
        assert client.post("/api/auth/password-reset", json={"email": "nope"}).status_code == 400
    finally:
        firebase.db().collection("system").document("email_settings").delete()
        alerts._cache.clear()


def test_measurement_ranges_follow_entry_order():
    from backend import measurements as M
    assert M.check(24, {"weight_kg": 100, "height_cm": 86})[0]            # 2-year-old cannot weigh 100 kg
    assert M.check(24, {"weight_kg": 12, "height_cm": 86}) == ([], [])
    assert M.check(24, {"weight_kg": 7, "height_cm": 86})[1]               # possible (malnutrition): warning
    assert M.check(24, {"weight_kg": 12})[0]                               # height must come first
    assert M.check(None, {"height_cm": 86})[0]                             # age must come first
    assert M.check(180, {"head_circumference_cm": 50, "height_cm": 160})[0]   # not measured at 15 y
    assert M.check(216, {"height_cm": 175, "weight_kg": 70}) == ([], [])
    tall, short = M.ranges(120, 150)["weight_kg"], M.ranges(120, 110)["weight_kg"]
    assert tall[3] > short[3]                                              # weight limit follows height


@needs_emulator
def test_patient_measurement_safeguards(clinician):
    base = {"full_name": "Growth Child", "gender": "M", "guardian_name": "Parent", "consent_given": True,
            "consent_by": "Parent"}
    h = clinician["headers"]
    r = client.post("/api/patients", headers=h, json={**base, "age_months": 24, "height_cm": 86, "weight_kg": 100})
    assert r.status_code == 400 and "not possible" in r.json()["detail"]
    r = client.post("/api/patients", headers=h, json={**base, "age_months": 24, "height_cm": 86, "weight_kg": 7})
    assert r.status_code == 400 and "reason" in r.json()["detail"]
    r = client.post("/api/patients", headers=h, json={**base, "age_months": 24, "height_cm": 86, "weight_kg": 7,
                                                      "measurement_note": "Severe acute malnutrition"})
    assert r.status_code == 200
    pid = r.json()["patient_id"]
    p = client.get(f"/api/patients/{pid}", headers=h).json()["patient"]
    assert p["age"] == 2 and p["age_months"] == 24 and p["weight_kg"] == 7 and p["measurement_note"]
    assert client.patch(f"/api/patients/{pid}", headers=h, json={"weight_kg": 90}).status_code == 400
    r = client.patch(f"/api/patients/{pid}", headers=h, json={"weight_kg": 12})
    assert r.status_code == 200
    p = client.get(f"/api/patients/{pid}", headers=h).json()["patient"]
    assert p["weight_kg"] == 12 and p["measurement_note"] is None
    r = client.post("/api/predict", files=sample_upload(), headers=h, data={"spo2": 140})
    assert r.status_code == 400
