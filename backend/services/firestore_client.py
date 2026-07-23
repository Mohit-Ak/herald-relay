"""Lazy Firestore client initialised once per process."""
import os
import firebase_admin
from firebase_admin import credentials, firestore

_app = None
_db = None


def get_db():
    global _app, _db
    if _db is None:
        if not firebase_admin._apps:
            # Use application default credentials (works on GCE automatically)
            cred = credentials.ApplicationDefault()
            _app = firebase_admin.initialize_app(cred, {
                "projectId": os.getenv("GCP_PROJECT_ID", "eh-voice-ai"),
            })
        _db = firestore.client()
    return _db
