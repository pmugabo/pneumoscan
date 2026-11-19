"""Offline clinic mode: local store, outbox sync with Firestore, offline sign-in.

The local-store tests run anywhere. Sync tests need the Firestore emulator; the sign-in
tests run when the suite is started in clinic mode (PNEUMOSCAN_OFFLINE=1), which also runs
every API test against the local store:

    PNEUMOSCAN_OFFLINE=1 PNEUMOSCAN_LOCAL_DB=/tmp/clinic-test.db PNEUMOSCAN_SYNC=0 \\
        firebase emulators:exec --only auth,firestore --project demo-pneumoscan "python -m pytest"
"""
import os
import time
import uuid

import pytest
from google.cloud.firestore_v1 import FieldFilter, Query
from google.cloud import firestore

from backend.localstore import LocalStore
from tests.conftest import EMULATED, needs_emulator


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "clinic.db")


def test_queries_and_transforms(store):
    c = store.collection("predictions")
    for i, (u, s) in enumerate([("a", "CLEARED"), ("b", "ESCALATED"), ("a", "FLAGGED_URGENT")]):
        c.document(f"p{i}").set({"user_id": u, "triage_state": s, "created_at": f"2026-01-0{i + 1}",
                                 "image": "x" * 10})
    mine = [s.id for s in c.where(filter=FieldFilter("user_id", "==", "a")).stream()]
    assert sorted(mine) == ["p0", "p2"]
    latest = c.order_by("created_at", direction=Query.DESCENDING).limit(2).select(["triage_state"]).get()
    assert [s.id for s in latest] == ["p2", "p1"] and latest[0].to_dict() == {"triage_state": "FLAGGED_URGENT"}

    ref = store.collection("escalations").document("e1")
    ref.set({"unread": {"u1": 0}, "history": []})
    ref.update({"unread.u1": firestore.Increment(2), "history": firestore.ArrayUnion([{"to": "s1"}])})
    ref.update({"history": firestore.ArrayUnion([{"to": "s1"}])})
    assert ref.get().to_dict() == {"unread": {"u1": 2}, "history": [{"to": "s1"}]}
    ref.update({"history": firestore.ArrayRemove([{"to": "s1"}]), "unread": firestore.DELETE_FIELD})
    assert ref.get().to_dict() == {"history": []}

    _, added = store.collection("audit_logs").add({"action": "x"})
    assert len(added.id) == 20 and added.get().exists
    b = store.batch()
    b.update(store.collection("predictions").document("p0"), {"comment": "ok"})
    b.delete(store.collection("predictions").document("p1"))
    b.commit()
    assert store.collection("predictions").document("p0").get().get("comment") == "ok"
    assert not store.collection("predictions").document("p1").get().exists
    assert store.pending() == 3 + 4 + 1 + 2


def test_remote_removal_keeps_clinical_records(tmp_path):
    from types import SimpleNamespace as NS
    from backend.localstore import Sync
    store = LocalStore(tmp_path / "clinic.db")
    store.write("predictions", "p1", {"triage_state": "CLEARED"}, queue=False)
    store.write("mfa", "u1", {"secret": "x"}, queue=False)
    sync = Sync(store)
    gone = lambda i: NS(type=NS(name="REMOVED"), document=NS(id=i, to_dict=lambda: {}))
    sync._on_change("predictions")(None, [gone("p1")], None)
    sync._on_change("mfa")(None, [gone("u1")], None)
    assert store.read("predictions", "p1") and store.read("mfa", "u1") is None


def _client(host: str):
    old = os.environ.get("FIRESTORE_EMULATOR_HOST")
    os.environ["FIRESTORE_EMULATOR_HOST"] = host
    try:
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore as gc
        return gc.Client(project=os.environ.get("FIREBASE_PROJECT_ID", "demo-pneumoscan"),
                         credentials=AnonymousCredentials())
    finally:
        os.environ["FIRESTORE_EMULATOR_HOST"] = old or ""


@needs_emulator
def test_writes_wait_offline_then_sync(tmp_path):
    remote = _client(os.environ["FIRESTORE_EMULATOR_HOST"])
    store = LocalStore(tmp_path / "clinic.db", remote=_client("127.0.0.1:9"))   # nothing listens there
    pid = f"offline-{uuid.uuid4().hex[:8]}"
    store.collection("predictions").document(pid).set({"triage_state": "CLEARED", "user_id": "u"})
    store.sync.run_once()
    assert not store.sync.online and store.pending() == 1
    assert store.collection("predictions").document(pid).get().exists      # still usable offline

    store.remote = remote                                                   # internet is back
    store.sync.run_once()
    assert store.sync.online and store.pending() == 0
    assert remote.collection("predictions").document(pid).get().to_dict()["triage_state"] == "CLEARED"

    remote.collection("predictions").document(pid).update({"comment": "seen by specialist"})
    for _ in range(50):
        if store.collection("predictions").document(pid).get().get("comment"):
            break
        time.sleep(0.1)
    assert store.collection("predictions").document(pid).get().get("comment") == "seen by specialist"
    store.sync.stop()


@needs_emulator
def test_missing_document_is_fetched_once(tmp_path):
    remote = _client(os.environ["FIRESTORE_EMULATOR_HOST"])
    uid = f"u-{uuid.uuid4().hex[:8]}"
    remote.collection("users").document(uid).set({"role": "clinician"})
    store = LocalStore(tmp_path / "clinic.db", remote=remote)
    assert store.collection("users").document(uid).get().to_dict() == {"role": "clinician"}
    assert store.read("users", uid) == {"role": "clinician"} and store.pending() == 0


clinic = pytest.mark.skipif(not (EMULATED and os.environ.get("PNEUMOSCAN_OFFLINE")),
                            reason="run the suite with PNEUMOSCAN_OFFLINE=1")


@clinic
def test_offline_sign_in(clinician):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend import firebase
    client = TestClient(app)
    email = firebase.db().collection("users").document(clinician["uid"]).get().get("email")
    bad = client.post("/api/auth/offline/login", json={"email": email, "password": "password123"})
    assert bad.status_code == 401                                   # never signed in online here

    r = client.post("/api/auth/offline/session", json={"password": "password123"},
                    headers=clinician["headers"])
    assert r.status_code == 200 and r.json()["offline_sign_in"]
    local = {"Authorization": f"Local {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=local).json()["role"] == "clinician"

    assert client.post("/api/auth/offline/login", json={"email": email, "password": "nope"}).status_code == 401
    assert client.post("/api/auth/offline/login",
                       json={"email": email, "password": "password123", "role": "specialist"}).status_code == 403
    ok = client.post("/api/auth/offline/login", json={"email": email, "password": "password123",
                                                       "role": "clinician"})
    assert ok.status_code == 200
    hdr = {"Authorization": f"Local {ok.json()['token']}"}
    assert client.get("/api/patients", headers=hdr).status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": "Local forged.token"}).status_code == 401
    assert client.get("/api/sync/status", headers=hdr).json()["clinic_mode"] is True


@clinic
def test_offline_sign_in_needs_second_factor(specialist):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend import firebase, mfa
    client = TestClient(app)
    secret = mfa.new_secret() if hasattr(mfa, "new_secret") else "JBSWY3DPEHPK3PXP"
    firebase.db().collection("mfa").document(specialist["uid"]).set({"secret": secret})
    firebase.db().collection("users").document(specialist["uid"]).update(
        {"mfa_enabled": True, "mfa_method": "totp"})
    from backend.main import forget_profile
    forget_profile(specialist["uid"])
    email = firebase.db().collection("users").document(specialist["uid"]).get().get("email")
    assert client.post("/api/auth/offline/session", json={"password": "password123"},
                       headers=specialist["headers"]).status_code == 401      # 2FA not done yet
    from backend import localauth
    localauth.enroll(specialist["uid"], email, "password123")
    r = client.post("/api/auth/offline/login", json={"email": email, "password": "password123"})
    assert r.status_code == 401 and "Two-factor" in r.json()["detail"]
    r = client.post("/api/auth/offline/login", json={"email": email, "password": "password123",
                                                      "code": mfa.totp(secret, time.time())})
    assert r.status_code == 200
