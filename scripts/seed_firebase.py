"""Create the demo accounts in Firebase Auth + Firestore. Safe to re-run.

    python -m scripts.seed_firebase --admin-email you@example.com --admin-password '...'

Specialists get SEED_PASSWORD (default: password123). Only Dr. Ephrem Bizimana is active.
"""
import argparse
import os
from datetime import datetime, timezone

from firebase_admin import auth

from backend import firebase

SPECIALISTS = [
    ("ephrem.bizimana@pneumoscan.rw", "Dr. Ephrem Bizimana", True),
    ("daniel.bitega@pneumoscan.rw", "Dr. Bitega Daniel", False),
    ("josephine.ngarambe@pneumoscan.rw", "Dr. Josephine Ngarambe", False),
]
HOSPITAL = "Kigali University Teaching Hospital"


def ensure(email, password, name, role, active=True, hospital=None):
    try:
        rec = auth.get_user_by_email(email)
        created = False
    except auth.UserNotFoundError:
        rec = auth.create_user(email=email, password=password, display_name=name)
        created = True
    ref = firebase.db().collection("users").document(rec.uid)
    profile = {"email": email, "full_name": name, "role": role,
               "hospital": hospital, "is_active": active}
    if not ref.get().exists:
        profile["created_at"] = datetime.now(timezone.utc).isoformat()
    ref.set(profile, merge=True)
    print(f"{'created' if created else 'updated'} {role:<10} {email}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-email")
    ap.add_argument("--admin-password")
    ap.add_argument("--admin-name", default="PneumoScan Admin")
    a = ap.parse_args()
    if not firebase.configured():
        raise SystemExit("Firebase is not configured: add serviceAccountKey.json "
                         "or set FIREBASE_CREDENTIALS / FIRESTORE_EMULATOR_HOST.")
    pw = os.environ.get("SEED_PASSWORD", "password123")
    for email, name, active in SPECIALISTS:
        ensure(email, pw, name, "specialist", active, HOSPITAL)
    if a.admin_email and a.admin_password:
        ensure(a.admin_email, a.admin_password, a.admin_name, "admin")


if __name__ == "__main__":
    main()
