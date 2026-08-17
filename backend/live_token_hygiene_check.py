"""Live proof that a re-registration can no longer wipe a working FCM token.

Runs against the deployed relay. Registers a device with a realistic token,
then re-registers exactly as the Flutter connect screen does (no fcm_token at
all) and confirms the stored token survived.

Before the fix the second call stored None and background push died silently.
"""
import json
import urllib.error
import urllib.request

RELAY = "http://34.173.138.246:8082"
REAL = "z" * 152


def post(path, body):
    req = urllib.request.Request(
        RELAY + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode()


s1, r1 = post("/push/register",
              {"platform": "android", "plan": "byok", "fcm_token": REAL})
print("register with real token ->", s1)
dev = r1["device_token"] if isinstance(r1, dict) else None
print("device:", dev)

s2, _ = post("/push/register",
             {"device_token": dev, "platform": "android", "plan": "byok"})
print("re-register WITHOUT token ->", s2)

s3, _ = post("/push/register",
             {"device_token": dev, "platform": "android", "plan": "byok",
              "fcm_token": "android"})
print("re-register with JUNK token ->", s3)

# A push attempt tells us what's on file: a real token reaches FCM (200/404
# from Google), a cleared one is skipped locally before any network call.
s4, r4 = post("/tunnel/update", {
    "device_token": dev, "run_id": "tokencheck", "seq": 0, "signal": "DONE",
    "event": {"type": "run.completed", "data": {}}, "summary": "check",
})
print("push attempt ->", s4, r4)
print("\nNow check the relay log: it must NOT say 'implausible fcm_token'")
print("and must NOT say 'no fcm_token' — either would mean the token was lost.")
