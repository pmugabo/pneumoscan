"""Firebase Admin initialisation: Firestore client and ID-token verification.

Credentials, first match wins:
    FIREBASE_SERVICE_ACCOUNT_JSON   the service-account key as a JSON string
    FIREBASE_CREDENTIALS            path to the service-account key file
    <repo>/serviceAccountKey.json   default key location (git-ignored)
    FIRESTORE_EMULATOR_HOST         local emulator, no key needed (FIREBASE_PROJECT_ID
                                    or "demo-pneumoscan" is used as the project)

The browser config comes from FIREBASE_WEB_CONFIG (JSON string) or
<repo>/firebase-web-config.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import auth, credentials, firestore
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore as gc_firestore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY = ROOT / "serviceAccountKey.json"
WEB_CONFIG_FILE = ROOT / "firebase-web-config.json"

_app: Optional[firebase_admin.App] = None
_db = None


def _credential():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw:
        return credentials.Certificate(json.loads(raw))
    path = os.environ.get("FIREBASE_CREDENTIALS")
    if path:
        return credentials.Certificate(path)
    if DEFAULT_KEY.exists():
        return credentials.Certificate(str(DEFAULT_KEY))
    return None


def using_emulator() -> bool:
    return bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))


def init() -> Optional[firebase_admin.App]:
    """Initialise once. Returns None when no credentials and no emulator are configured."""
    global _app, _db
    if _app is not None:
        return _app
    if using_emulator():
        project = os.environ.get("FIREBASE_PROJECT_ID", "demo-pneumoscan")
        app = firebase_admin.initialize_app(options={"projectId": project})
        _db = gc_firestore.Client(project=project, credentials=AnonymousCredentials())
    else:
        cred = _credential()
        if cred is None:
            return None
        app = firebase_admin.initialize_app(cred)
        _db = firestore.client(app)
    _app = app
    return _app


def configured() -> bool:
    return init() is not None


def db():
    if init() is None:
        raise RuntimeError("Firebase is not configured")
    return _db


def verify_id_token(token: str) -> dict:
    init()
    return auth.verify_id_token(token, app=_app)


def web_config() -> Optional[dict]:
    raw = os.environ.get("FIREBASE_WEB_CONFIG")
    if raw:
        cfg = json.loads(raw)
    elif WEB_CONFIG_FILE.exists():
        cfg = json.loads(WEB_CONFIG_FILE.read_text())
    elif using_emulator():
        cfg = {"apiKey": "demo-key",
               "projectId": os.environ.get("FIREBASE_PROJECT_ID", "demo-pneumoscan"),
               "authDomain": "localhost"}
    else:
        return None
    emu = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST")
    if emu:
        cfg["authEmulatorUrl"] = f"http://{emu}"
    return cfg
