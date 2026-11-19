"""Local-first document store for offline clinic mode (PNEUMOSCAN_OFFLINE=1).

The clinic server keeps every record in a SQLite file and answers all API calls from it,
so scanning, triage, patients, referrals and reports keep working without internet.
It implements the subset of the Firestore client API that the backend uses (collections,
documents, equality/range filters, order_by, limit, select, batches and the Increment /
ArrayUnion / ArrayRemove / DELETE_FIELD transforms).

Sync with Cloud Firestore runs in the background:
  push  every local write is queued in an outbox and replayed to Firestore, in order,
        whenever the connection is up (the whole document is written: last writer wins);
  pull  Firestore snapshot listeners copy changes made elsewhere (for example by a
        specialist on the hosted server) into the local file. A document with local
        changes still waiting in the outbox is not overwritten, and clinical records are
        never deleted locally because of a remote removal.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from google.api_core.exceptions import NotFound
from google.cloud.firestore_v1 import transforms

_pool = None


def _remote_call(fn, *args, timeout: float = None, **kw):
    """Run a Firestore call with a hard deadline: without internet, gRPC can otherwise wait
    far longer than the requested timeout while it keeps trying to connect."""
    global _pool
    from concurrent.futures import ThreadPoolExecutor
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clinic-remote")
    t = timeout or REMOTE_TIMEOUT_S
    return _pool.submit(fn, *args, retry=None, timeout=t, **kw).result(timeout=t + 1)


PULL_COLLECTIONS = ("users", "patients", "predictions", "escalations", "messages",
                    "notifications", "mfa", "system")
# Clinical records are never deleted by the app, so a removal reported by Firestore (for
# example after a database reset) does not delete the clinic's local copy.
KEEP_ON_REMOTE_DELETE = {"users", "patients", "predictions", "escalations", "messages", "notifications"}
REMOTE_TIMEOUT_S = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("latin-1")
    return str(v)


def _auto_id() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))


def _get_path(d: dict, path: str):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _has_path(d: dict, path: str) -> bool:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _resolve(value, current):
    """Apply a Firestore transform to the current value; plain values pass through."""
    if isinstance(value, transforms.Increment):
        return (current if isinstance(current, (int, float)) else 0) + value.value
    if isinstance(value, transforms.ArrayUnion):
        out = list(current) if isinstance(current, list) else []
        out += [x for x in value.values if x not in out]
        return out
    if isinstance(value, transforms.ArrayRemove):
        return [x for x in (current if isinstance(current, list) else []) if x not in value.values]
    if value is transforms.SERVER_TIMESTAMP:
        return _now_iso()
    return copy.deepcopy(value)


def _set_path(d: dict, path: str, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    if value is transforms.DELETE_FIELD:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = _resolve(value, cur.get(parts[-1]))


def _apply_set(data: dict, base: Optional[dict] = None) -> dict:
    out = copy.deepcopy(base) if base else {}

    def merge(dst: dict, src: dict):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            elif v is transforms.DELETE_FIELD:
                dst.pop(k, None)
            else:
                dst[k] = _resolve(v, dst.get(k))
    merge(out, data)
    return out


# ------------------------------------------------------------------ snapshots and refs
class LocalSnapshot:
    def __init__(self, ref: "LocalDocRef", data: Optional[dict]):
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field: str):
        return _get_path(self._data or {}, field)


class LocalDocRef:
    def __init__(self, store: "LocalStore", collection: str, doc_id: str):
        self._store, self._col, self.id = store, collection, doc_id

    @property
    def path(self) -> str:
        return f"{self._col}/{self.id}"

    def get(self, field_paths: Optional[list] = None, **_) -> LocalSnapshot:
        data = self._store.read(self._col, self.id)
        if data is None:
            data = self._store.fetch_missing(self._col, self.id)
        if data is not None and field_paths:
            data = {k: v for k, v in data.items() if k in field_paths}
        return LocalSnapshot(self, data)

    def set(self, data: dict, merge: bool = False):
        with self._store.lock:
            base = self._store.read(self._col, self.id) if merge else None
            self._store.write(self._col, self.id, _apply_set(data, base))

    def update(self, changes: dict):
        with self._store.lock:
            cur = self._store.read(self._col, self.id)
            if cur is None:
                raise NotFound(f"No document to update: {self.path}")
            for k, v in changes.items():
                _set_path(cur, k, v)
            self._store.write(self._col, self.id, cur)

    def delete(self):
        self._store.remove(self._col, self.id)


class LocalQuery:
    _OPS = {
        "==": lambda a, b: a == b, "!=": lambda a, b: a is not None and a != b,
        "<": lambda a, b: a is not None and a < b, "<=": lambda a, b: a is not None and a <= b,
        ">": lambda a, b: a is not None and a > b, ">=": lambda a, b: a is not None and a >= b,
        "in": lambda a, b: a in b, "not-in": lambda a, b: a is not None and a not in b,
        "array_contains": lambda a, b: isinstance(a, list) and b in a,
        "array_contains_any": lambda a, b: isinstance(a, list) and any(x in a for x in b),
    }

    def __init__(self, store: "LocalStore", collection: str, filters=(), orders=(),
                 limit_n: Optional[int] = None, fields: Optional[list] = None):
        self._store, self._col = store, collection
        self._filters, self._orders = list(filters), list(orders)
        self._limit, self._fields = limit_n, fields

    def _copy(self, **kw) -> "LocalQuery":
        args = dict(filters=self._filters, orders=self._orders, limit_n=self._limit,
                    fields=self._fields)
        args.update(kw)
        return LocalQuery(self._store, self._col, **args)

    def where(self, field_path=None, op_string=None, value=None, *, filter=None):
        if filter is not None:
            field_path, op_string, value = filter.field_path, filter.op_string, filter.value
        if op_string not in self._OPS:
            raise ValueError(f"unsupported operator {op_string}")
        return self._copy(filters=self._filters + [(field_path, op_string, value)])

    def order_by(self, field_path: str, direction: str = "ASCENDING"):
        return self._copy(orders=self._orders + [(field_path, direction == "DESCENDING")])

    def limit(self, n: int):
        return self._copy(limit_n=n)

    def select(self, field_paths: Iterable[str]):
        return self._copy(fields=list(field_paths))

    def stream(self, **_):
        rows = []
        for doc_id, data in self._store.scan(self._col):
            if all(self._OPS[op](_get_path(data, f), v) for f, op, v in self._filters) and \
                    all(_has_path(data, f) for f, _ in self._orders):
                rows.append((doc_id, data))
        for field, desc in reversed(self._orders):
            rows.sort(key=lambda r: _get_path(r[1], field), reverse=desc)
        if not self._orders:
            rows.sort(key=lambda r: r[0])
        if self._limit is not None:
            rows = rows[:self._limit]
        for doc_id, data in rows:
            if self._fields is not None:
                data = {k: v for k, v in data.items() if k in self._fields}
            yield LocalSnapshot(LocalDocRef(self._store, self._col, doc_id), data)

    def get(self, **_) -> list:
        return list(self.stream())


class LocalCollection(LocalQuery):
    def __init__(self, store: "LocalStore", name: str):
        super().__init__(store, name)
        self.id = name

    def document(self, doc_id: Optional[str] = None) -> LocalDocRef:
        return LocalDocRef(self._store, self._col, doc_id or _auto_id())

    def add(self, data: dict, document_id: Optional[str] = None):
        ref = self.document(document_id)
        ref.set(data)
        return datetime.now(timezone.utc), ref


class LocalBatch:
    def __init__(self):
        self._ops = []

    def set(self, ref: LocalDocRef, data: dict, merge: bool = False):
        self._ops.append(lambda: ref.set(data, merge=merge))

    def update(self, ref: LocalDocRef, changes: dict):
        self._ops.append(lambda: ref.update(changes))

    def delete(self, ref: LocalDocRef):
        self._ops.append(ref.delete)

    def commit(self):
        for op in self._ops:
            op()
        self._ops = []


# ------------------------------------------------------------------ the store
class LocalStore:
    """SQLite file with documents, an outbox of unsynced writes and local-only settings."""

    def __init__(self, path: Path, remote=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.remote = remote
        self.lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS docs (col TEXT, id TEXT, data TEXT, PRIMARY KEY (col, id));
            CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, col TEXT,
                id TEXT, op TEXT, data TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS local_kv (k TEXT PRIMARY KEY, v TEXT);
        """)
        self.sync = Sync(self) if remote is not None else None

    # Firestore client surface ---------------------------------------------------------
    def collection(self, name: str) -> LocalCollection:
        return LocalCollection(self, name)

    def batch(self) -> LocalBatch:
        return LocalBatch()

    def get_all(self, refs, field_paths: Optional[list] = None, **_):
        for ref in refs:
            yield ref.get(field_paths=field_paths)

    # storage --------------------------------------------------------------------------
    def read(self, col: str, doc_id: str) -> Optional[dict]:
        with self.lock:
            row = self._db.execute("SELECT data FROM docs WHERE col=? AND id=?",
                                   (col, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def scan(self, col: str):
        with self.lock:
            rows = self._db.execute("SELECT id, data FROM docs WHERE col=?", (col,)).fetchall()
        return [(i, json.loads(d)) for i, d in rows]

    def write(self, col: str, doc_id: str, data: dict, queue: bool = True):
        raw = json.dumps(data, default=_jsonable)
        with self.lock:
            self._db.execute("INSERT OR REPLACE INTO docs (col, id, data) VALUES (?, ?, ?)",
                             (col, doc_id, raw))
            if queue:
                self._db.execute("INSERT INTO outbox (col, id, op, data, created) VALUES "
                                 "(?, ?, 'set', ?, ?)", (col, doc_id, raw, time.time()))

    def remove(self, col: str, doc_id: str, queue: bool = True):
        with self.lock:
            self._db.execute("DELETE FROM docs WHERE col=? AND id=?", (col, doc_id))
            if queue:
                self._db.execute("INSERT INTO outbox (col, id, op, data, created) VALUES "
                                 "(?, ?, 'delete', NULL, ?)", (col, doc_id, time.time()))

    def pending(self, col: Optional[str] = None, doc_id: Optional[str] = None) -> int:
        with self.lock:
            if col is None:
                return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM outbox WHERE col=? AND id=?",
                                    (col, doc_id)).fetchone()[0]

    def kv_get(self, key: str) -> Optional[str]:
        with self.lock:
            row = self._db.execute("SELECT v FROM local_kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str):
        with self.lock:
            self._db.execute("INSERT OR REPLACE INTO local_kv (k, v) VALUES (?, ?)", (key, value))

    def reset(self):
        """Drop all documents and queued writes (tests only; local settings are kept)."""
        with self.lock:
            self._db.execute("DELETE FROM docs")
            self._db.execute("DELETE FROM outbox")

    def fetch_missing(self, col: str, doc_id: str) -> Optional[dict]:
        """A document not copied yet (first start, slow first sync) is read from Firestore
        once when the connection is up, then kept locally."""
        if self.sync is None or not self.sync.online:
            return None
        try:
            snap = _remote_call(self.remote.collection(col).document(doc_id).get)
        except Exception as e:
            self.sync.mark_offline(e)
            return None
        if not snap.exists:
            return None
        data = json.loads(json.dumps(snap.to_dict() or {}, default=_jsonable))
        with self.lock:
            if self.read(col, doc_id) is None and not self.pending(col, doc_id):
                self.write(col, doc_id, data, queue=False)
        return data


class Sync:
    """Background push of the outbox and pull of remote changes."""

    def __init__(self, store: LocalStore, interval_s: float = 5.0):
        self.store, self.interval = store, interval_s
        self.online = True
        self.last_push: Optional[str] = None
        self.last_pull: Optional[str] = None
        self.last_error: Optional[str] = None
        self.watches: dict = {}
        self.after_push = []          # callbacks run after each successful push cycle
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="clinic-sync", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        for w in self.watches.values():
            try:
                w.unsubscribe()
            except Exception:
                pass

    def status(self) -> dict:
        return {"online": self.online, "pending": self.store.pending(),
                "last_push": self.last_push, "last_pull": self.last_pull,
                "last_error": None if self.online else self.last_error}

    def mark_offline(self, err: Exception):
        self.online = False
        self.last_error = f"{type(err).__name__}: {str(err)[:200]}"

    # push ----------------------------------------------------------------------------
    def push_once(self) -> int:
        remote, db, sent = self.store.remote, self.store._db, 0
        while True:
            with self.store.lock:
                rows = db.execute("SELECT seq, col, id, op, data FROM outbox ORDER BY seq "
                                  "LIMIT 50").fetchall()
            if not rows:
                break
            for seq, col, doc_id, op, data in rows:
                ref = remote.collection(col).document(doc_id)
                if op == "delete":
                    _remote_call(ref.delete, timeout=REMOTE_TIMEOUT_S * 2)
                else:
                    _remote_call(ref.set, json.loads(data), timeout=REMOTE_TIMEOUT_S * 2)
                with self.store.lock:
                    db.execute("DELETE FROM outbox WHERE seq=?", (seq,))
                sent += 1
        return sent

    def probe(self):
        _remote_call(self.store.remote.collection("system").document("clinic_ping").get)

    # pull ----------------------------------------------------------------------------
    def _on_change(self, col: str):
        def callback(_docs, changes, _read_time):
            for ch in changes:
                doc = ch.document
                with self.store.lock:
                    if self.store.pending(col, doc.id):
                        continue
                    if ch.type.name == "REMOVED":
                        if col not in KEEP_ON_REMOTE_DELETE:
                            self.store.remove(col, doc.id, queue=False)
                    else:
                        data = json.loads(json.dumps(doc.to_dict() or {}, default=_jsonable))
                        self.store.write(col, doc.id, data, queue=False)
            self.last_pull = _now_iso()
        return callback

    def ensure_watches(self):
        for col in PULL_COLLECTIONS:
            w = self.watches.get(col)
            if w is not None and getattr(w, "is_active", True):
                continue
            try:
                self.watches[col] = self.store.remote.collection(col).on_snapshot(self._on_change(col))
            except Exception as e:
                self.mark_offline(e)

    # loop ----------------------------------------------------------------------------
    def run_once(self):
        try:
            self.probe()
            self.push_once()
            self.online, self.last_error = True, None
            self.last_push = _now_iso()
            self.ensure_watches()
            for fn in self.after_push:
                try:
                    fn()
                except Exception as e:
                    print(f"[clinic-sync] after-push task failed: {e}")
        except Exception as e:
            self.mark_offline(e)

    def _loop(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval if self.online else min(30.0, self.interval * 3))
