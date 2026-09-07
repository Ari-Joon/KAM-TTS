"""The listener a phone can reach, and everything it refuses.

This is the only part of KAM that is reachable from the network, so what is
worth testing is what it says no to. The app under test is a plain WSGI callable
and is driven directly here, which means no werkzeug and no socket: the suite
runs on whatever Python is to hand, the same as every other one, and never opens
a port on the machine running it.
"""
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import scan_host as S

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def call(method, path, body=b"", token=None, declared_length=None):
    """Drive the WSGI app straight, and return (status_code, body)."""
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body) if declared_length is None else declared_length),
        "wsgi.input": io.BytesIO(body),
    }
    if token is not None:
        env["HTTP_X_KAM_SCAN"] = token
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(headers)

    out = b"".join(S._phone_app(env, start_response))
    return captured["status"], out


def fake_session(**over):
    """A session exactly as open_session would leave one, minus the listener."""
    d = tempfile.mkdtemp(prefix="kam_scan_test_")
    sess = {"code": "AB12CD", "token": "test-token-value", "dir": d, "pages": [],
            "opened": time.time(), "touched": time.time(), "ip": "192.168.0.2"}
    sess.update(over)
    S._session = sess
    return sess


print("\n=== with no session open there is nothing to talk to ===")
S._session = None
check("the page is gone",   call("GET", "/")[0], 410)
check("so is the upload",   call("POST", "/upload", b"x", token="anything")[0], 410)

print("\n=== the page itself ===")
sess = fake_session()
code, body = call("GET", "/")
check("the phone gets the page", code, 200)
check("and it is the real one", b"KAM TTS" in body and b"Take photo" in body, True)
check("an unknown path is not served", call("GET", "/../server.py")[0], 404)
check("nor is anything else invented", call("GET", "/config")[0], 404)
check("the upload route is POST only", call("GET", "/upload")[0], 404)

print("\n=== the upload gate ===")
# This is the whole security surface: a wrong key must buy nothing.
jpg = b"\xff\xd8\xff" + b"x" * 1000
check("no key at all is refused",  call("POST", "/upload", jpg)[0], 403)
check("a wrong key is refused",    call("POST", "/upload", jpg, token="nope")[0], 403)
check("an empty key is refused",   call("POST", "/upload", jpg, token="")[0], 403)
# A prefix of the real token must not pass, which is what a comparison that
# stops at the first wrong byte would eventually allow.
check("a prefix of the key is refused",
      call("POST", "/upload", jpg, token=sess["token"][:8])[0], 403)
check("the right key is accepted", call("POST", "/upload", jpg, token=sess["token"])[0], 200)
check("and the photo landed", len(sess["pages"]), 1)
check("on disk, at the size sent",
      os.path.getsize(sess["pages"][0]["path"]), len(jpg))

print("\n=== limits, so a stranger on the wifi cannot fill the disk ===")
big = b"\xff\xd8" + b"x" * (S.MAX_IMAGE_BYTES + 1)
check("an oversized image is refused",
      call("POST", "/upload", big, token=sess["token"])[0], 413)
check("and was not written", len(sess["pages"]), 1)
check("an empty body is refused",
      call("POST", "/upload", b"", token=sess["token"])[0], 400)
# A lying Content-Length must not make the read run past its own body or hang.
check("a declared length larger than the body still terminates",
      call("POST", "/upload", b"\xff\xd8short", token=sess["token"],
           declared_length=5000)[0], 200)

sess["pages"] = [{"path": "x", "bytes": 1, "ts": time.time()}] * S.MAX_PAGES
check("past the page limit it stops accepting",
      call("POST", "/upload", jpg, token=sess["token"])[0], 429)

print("\n=== a session that has gone stale is closed, not served ===")
sess = fake_session(touched=time.time() - S.SESSION_TTL - 1)
check("an expired session serves nothing", call("GET", "/")[0], 410)
check("and refuses uploads",
      call("POST", "/upload", jpg, token=sess["token"])[0], 410)

print("\n=== reading pages back, and forgetting them ===")
sess = fake_session()
call("POST", "/upload", jpg, token=sess["token"])
check("the page reads back whole", len(S.read_page(0) or b""), len(jpg))
check("an index past the end is None", S.read_page(9), None)
check("a negative index is None",      S.read_page(-1), None)
check("dropping a page reports it",    S.drop_page(0)["ok"], True)
check("and the file is gone",          os.path.exists(sess["pages"][0]["path"]), False)
check("dropping a page twice is refused", S.drop_page(9)["ok"], False)

print("\n=== closing takes the photos with it ===")
sess = fake_session()
call("POST", "/upload", jpg, token=sess["token"])
d = sess["dir"]
S.close_session()
check("the temp directory is removed", os.path.exists(d), False)
check("the session is forgotten",      S.session_status(), {"open": False})
check("and the port answers nothing",  call("GET", "/")[0], 410)

print("\n=== the address offered to the phone ===")
ip = S.lan_ip()
check("it is a dotted quad", len(ip.split(".")), 4)

# A machine with no network address has nothing to offer a phone, and printing
# 127.0.0.1 on a QR code would send it somewhere it cannot go. Forced here
# rather than asserted about whatever this machine happens to have, so the
# refusal is actually exercised.
_real_lan_ip = S.lan_ip
S.lan_ip = lambda: "127.0.0.1"
try:
    res = S.open_session()
    check("a machine with no network address refuses to open", res["ok"], False)
    check("and says why rather than offering loopback",
          "not on wifi" in res.get("error", ""), True)
    check("no listener was left behind", S.session_status(), {"open": False})
finally:
    S.lan_ip = _real_lan_ip

for leftover in pathlib.Path(tempfile.gettempdir()).glob("kam_scan_test_*"):
    shutil.rmtree(leftover, ignore_errors=True)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
