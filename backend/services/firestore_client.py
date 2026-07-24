"""Firestore client — real (GCE ADC) or in-memory stub.

Set ``HERALD_INMEMORY_DB=1`` to use a fully in-process Firestore stand-in.
The stub implements the exact subset of the Firestore API the relay uses
(collection / document / get / set(merge) / update / delete / stream /
where / order_by / add, plus snapshot .id / .exists / .reference /
.to_dict()).  This makes the E2E test hermetic — no GCP creds, no network,
no hangs — while production keeps using real Firestore via Application
Default Credentials on GCE.
"""
import os
import time

_app = None
_db = None


# ── in-memory stub ──────────────────────────────────────────────────────────

class _StubSnapshot:
    """Mimics a Firestore DocumentSnapshot."""

    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _StubDocRef:
    """Mimics a Firestore DocumentReference."""

    def __init__(self, collection, doc_id):
        self._collection = collection
        self.id = doc_id

    def get(self):
        return _StubSnapshot(self, self._collection._docs.get(self.id))

    def set(self, data, merge=False):
        if merge and self.id in self._collection._docs:
            self._collection._docs[self.id].update(data)
        else:
            self._collection._docs[self.id] = dict(data)

    def update(self, data):
        self._collection._docs.setdefault(self.id, {}).update(data)

    def delete(self):
        self._collection._docs.pop(self.id, None)
        self._collection._subcollections.pop(self.id, None)

    def collection(self, name):
        key = (self.id, name)
        subs = self._collection._subcollections
        if key not in subs:
            subs[key] = _StubCollection()
        return subs[key]


class _StubQuery:
    """Mimics a Firestore Query (chained where / order_by / stream)."""

    def __init__(self, collection, filters=None, order_key=None):
        self._collection = collection
        self._filters = filters or []
        self._order_key = order_key

    def where(self, field, op, value):
        if op != "==":
            raise NotImplementedError(f"stub where op {op!r} not supported")
        return _StubQuery(self._collection, self._filters + [(field, value)], self._order_key)

    def order_by(self, key):
        return _StubQuery(self._collection, self._filters, key)

    def stream(self):
        items = []
        for doc_id, data in self._collection._docs.items():
            if all(data.get(f) == v for f, v in self._filters):
                items.append((doc_id, data))
        if self._order_key:
            items.sort(key=lambda kv: kv[1].get(self._order_key))
        for doc_id, data in items:
            ref = _StubDocRef(self._collection, doc_id)
            yield _StubSnapshot(ref, data)


class _StubCollection:
    """Mimics a Firestore CollectionReference."""

    def __init__(self):
        self._docs = {}                 # doc_id -> dict
        self._subcollections = {}        # (parent_doc_id, name) -> _StubCollection

    def document(self, doc_id):
        return _StubDocRef(self, doc_id)

    def where(self, field, op, value):
        return _StubQuery(self).where(field, op, value)

    def order_by(self, key):
        return _StubQuery(self).order_by(key)

    def stream(self):
        return _StubQuery(self).stream()

    def add(self, data):
        doc_id = f"auto_{len(self._docs)}_{int(time.time()*1e6)}"
        self._docs[doc_id] = dict(data)
        return None, _StubDocRef(self, doc_id)


class _StubDB:
    """Mimics a Firestore client."""

    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = _StubCollection()
        return self._collections[name]


# ── entry point ─────────────────────────────────────────────────────────────

def get_db():
    global _app, _db
    if _db is None:
        if os.getenv("HERALD_INMEMORY_DB") == "1":
            _db = _StubDB()
        else:
            import firebase_admin
            from firebase_admin import credentials, firestore
            if not firebase_admin._apps:
                # Application default credentials (works on GCE automatically)
                cred = credentials.ApplicationDefault()
                _app = firebase_admin.initialize_app(cred, {
                    "projectId": os.getenv("GCP_PROJECT_ID", "eh-voice-ai"),
                })
            _db = firestore.client()
    return _db
