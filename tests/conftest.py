"""API tests run against the Firebase emulators:

    firebase emulators:exec --only auth,firestore --project demo-pneumoscan "python -m pytest"
"""
import os
import uuid

import httpx
import pytest

EMULATED = bool(os.environ.get("FIRESTORE_EMULATOR_HOST") and
                os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"))
needs_emulator = pytest.mark.skipif(not EMULATED, reason="Firebase emulators not running")


def make_user(role: str) -> dict:
    """Create a user in the Auth emulator + Firestore and return its auth header."""
    from firebase_admin import auth
    from backend import firebase
    firebase.init()
    email = f"{role}-{uuid.uuid4().hex[:8]}@test.rw"
    rec = auth.create_user(email=email, password="password123", display_name=role.title())
    firebase.db().collection("users").document(rec.uid).set(
        {"email": email, "full_name": f"Test {role.title()}", "role": role,
         "hospital": None, "created_at": "2026-01-01T00:00:00+00:00", "is_active": True})
    host = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
    r = httpx.post(f"http://{host}/identitytoolkit.googleapis.com/v1/"
                   "accounts:signInWithPassword?key=demo-key",
                   json={"email": email, "password": "password123", "returnSecureToken": True})
    r.raise_for_status()
    return {"uid": rec.uid, "headers": {"Authorization": f"Bearer {r.json()['idToken']}"}}


@pytest.fixture
def clinician():
    return make_user("clinician")


@pytest.fixture
def specialist():
    return make_user("specialist")


@pytest.fixture
def admin():
    return make_user("admin")
