"""Shared test fixtures – provides a FakeDB that replaces Firestore."""
import pytest
import services.firestore_client as _fc_mod


class FakeDoc:
    def __init__(self, data=None):
        self.exists = data is not None
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._id = doc_id
        self._subcollections: dict = {}

    def get(self):
        return FakeDoc(self._store.get(self._id))

    def set(self, data, merge=False):
        if merge and self._id in self._store:
            self._store[self._id].update(data)
        else:
            self._store[self._id] = dict(data)

    def update(self, data):
        if self._id in self._store:
            self._store[self._id].update(data)
        else:
            self._store[self._id] = dict(data)

    def collection(self, name):
        if name not in self._subcollections:
            self._subcollections[name] = FakeCollection()
        return self._subcollections[name]


class FakeCollection:
    def __init__(self):
        self._docs = {}

    def document(self, doc_id):
        return FakeDocRef(self._docs, doc_id)


class FakeDB:
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollection()
        return self._collections[name]


@pytest.fixture(autouse=True)
def mock_firestore(monkeypatch):
    """Replace get_db() globally for every test with a fresh in-memory FakeDB."""
    fake_db = FakeDB()
    monkeypatch.setattr(_fc_mod, "get_db", lambda: fake_db)
    # Also patch the already-imported references in routers
    import routers.push as push_mod
    import routers.billing as billing_mod
    monkeypatch.setattr(push_mod, "get_db", lambda: fake_db)
    monkeypatch.setattr(billing_mod, "get_db", lambda: fake_db)
    import routers.tunnel as tunnel_mod
    monkeypatch.setattr(tunnel_mod, "get_db", lambda: fake_db)
    return fake_db
