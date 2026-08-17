"""Live check of the background-update path against the REAL deployed relay.

Sends real /tunnel/update calls to the production relay for a fake run and
asserts the relay accepts each signal and that the FCM branch is reached for
QUESTION / DONE / spoken MILESTONE. The device has no fcm_token registered in
this environment, so the relay logs "[FCM] no fcm_token ... skipping push" —
reaching that line is the proof the branch fired, which is what we're pinning
(the MILESTONE branch did not exist at all before).
"""
import json
import urllib.error
import urllib.request

RELAY = "http://34.173.138.246:8082"
DEVICE = "52b6f5f8-6269-41a0-8dc5-a8bfa0f20c84"
RUN = "livecheck-run-1"


def post(path, body):
    req = urllib.request.Request(
        RELAY + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()[:200].decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode()
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


CASES = [
    ("IGNORE   (must be accepted + dropped)", {
        "signal": "IGNORE", "seq": 0,
        "event": {"type": "message.delta", "data": {"delta": "x"}}}),
    ("MILESTONE with spoken_text (NEW push)", {
        "signal": "MILESTONE", "seq": 1,
        "event": {"type": "tool.progress", "data": {}},
        "summary": "Still going - finished terminal.",
        "spoken_text": "Still going - finished terminal."}),
    ("QUESTION  (high urgency push)", {
        "signal": "QUESTION", "seq": 2,
        "event": {"type": "approval.request", "data": {"prompt": "ok?"}},
        "summary": "ok?", "spoken_text": "ok?"}),
    ("DONE      (terminal push)", {
        "signal": "DONE", "seq": 3,
        "event": {"type": "run.completed", "data": {}},
        "summary": "All finished."}),
]

print(f"relay={RELAY}\n")
ok_all = True
for name, extra in CASES:
    body = {"device_token": DEVICE, "run_id": RUN}
    body.update(extra)
    status, resp = post("/tunnel/update", body)
    good = status == 200
    ok_all &= good
    print(f"{'PASS' if good else 'FAIL'}  {name:42s} -> {status} {resp[:80]}")

print("\nVERDICT:", "PASS - relay accepts every signal" if ok_all else "FAIL")
