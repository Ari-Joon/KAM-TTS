"""Phone scanning: a tiny, short-lived HTTP listener the phone can actually reach.

The rest of KAM binds to 127.0.0.1 and nothing else, which is the right posture
and the reason a phone cannot talk to it. So this module opens a second listener
on the machine's LAN address, for as long as a scan session is open and no
longer, and closes it again afterwards.

Three decisions hold the security of this together, and none of them should be
"simplified" away.

1. It serves its own tiny WSGI app, not the main one. The phone can reach the
   scan page and the upload endpoint and there is nothing else on that port to
   reach, so even a mistake in the token check below cannot expose /speak, the
   quality database or /voices/delete. Serving the main app here and filtering by
   path would put one regex between my whole API and the network.

2. It binds the LAN address specifically rather than 0.0.0.0. The socket exists
   on one interface, for one purpose, while a session is open.

3. The phone is given a per-session token, never KAM_TOKEN. It is random, it
   dies with the session, and losing it costs the ability to upload a photo to a
   session that is already open rather than the run of the API.

Sessions expire on their own, since the failure I actually expect is forgetting
the panel is open rather than an attack.
"""
import hmac
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time

# --- Limits ---
# A phone photo is a few megabytes, so the cap is generous but finite; the point
# is that an unbounded POST cannot fill the disk.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PAGES       = 60
SESSION_TTL     = 20 * 60      # seconds of inactivity before it closes itself
SCAN_PORT       = 5051

# The pairing code is what gets typed in by hand when the QR will not scan, so
# it leaves out the characters people misread.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_lock     = threading.Lock()
_session  = None      # dict while open, None while closed
_server   = None      # werkzeug server, only while open
_thread   = None


def lan_ip():
    """The address this machine is reachable at on its own network.

    Found by asking the OS which local address it would use to reach the
    internet, which does not send anything: a UDP socket has no handshake, so
    connect() only picks the route. Falls back to the hostname lookup, and then
    to loopback, in which case the caller has nothing to offer the phone and
    should say so rather than printing an address that cannot work.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def _now():
    return time.time()


def _expired(sess):
    return (_now() - sess["touched"]) > SESSION_TTL


def _touch(sess):
    sess["touched"] = _now()


# --- The app the phone talks to ---
# Written against the WSGI interface directly rather than Flask, so there is no
# routing table to grow and nothing to accidentally register on this port later.

_PHONE_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_phone.html")


def _read_phone_page():
    with open(_PHONE_PAGE, "rb") as f:
        return f.read()


def _token_ok(sess, given):
    """Compare in constant time, so a wrong token cannot be found byte by byte."""
    return bool(given) and hmac.compare_digest(str(given), sess["token"])


def _phone_app(environ, start_response):
    """Two things only: hand over the page, and take an image."""
    path   = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    def reply(status, body, ctype="text/plain; charset=utf-8", extra=None):
        headers = [("Content-Type", ctype),
                   ("Content-Length", str(len(body))),
                   ("Cache-Control", "no-store"),
                   # The page only ever talks to its own origin, so nothing here
                   # needs to be reachable from a website the phone has open.
                   ("X-Content-Type-Options", "nosniff")]
        if extra:
            headers += extra
        start_response(status, headers)
        return [body]

    with _lock:
        sess = _session
        if sess is not None and _expired(sess):
            sess = None

    if sess is None:
        return reply("410 Gone", b"This scan session has closed. "
                                 b"Open the Scan panel on your computer again.")

    if method == "GET" and path in ("/", "/index.html"):
        _touch(sess)
        try:
            return reply("200 OK", _read_phone_page(), "text/html; charset=utf-8")
        except OSError:
            return reply("500 Internal Server Error", b"scan_phone.html is missing")

    if method == "POST" and path == "/upload":
        if not _token_ok(sess, environ.get("HTTP_X_KAM_SCAN")):
            return reply("403 Forbidden", b'{"ok":false,"error":"bad session key"}',
                         "application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return reply("400 Bad Request", b'{"ok":false,"error":"empty"}',
                         "application/json")
        if length > MAX_IMAGE_BYTES:
            return reply("413 Payload Too Large",
                         b'{"ok":false,"error":"image too large"}', "application/json")
        with _lock:
            if len(sess["pages"]) >= MAX_PAGES:
                return reply("429 Too Many Requests",
                             b'{"ok":false,"error":"page limit reached"}',
                             "application/json")
        # Read exactly what was declared, so a lying Content-Length cannot make
        # this block forever or read past its own body.
        data = environ["wsgi.input"].read(length)
        if not data:
            return reply("400 Bad Request", b'{"ok":false,"error":"empty"}',
                         "application/json")
        with _lock:
            idx  = len(sess["pages"])
            name = os.path.join(sess["dir"], f"page_{idx:03d}.jpg")
        try:
            with open(name, "wb") as f:
                f.write(data)
        except OSError as e:
            return reply("500 Internal Server Error",
                         f'{{"ok":false,"error":"{e}"}}'.encode(), "application/json")
        with _lock:
            sess["pages"].append({"path": name, "bytes": len(data), "ts": _now()})
            _touch(sess)
            n = len(sess["pages"])
        print(f"[SCAN] page {n} received ({len(data)//1024} KB)")
        return reply("200 OK", f'{{"ok":true,"n":{n}}}'.encode(), "application/json")

    return reply("404 Not Found", b"not here")


# --- Session control, called from the loopback API ---

def open_session():
    """Start a scan session and the listener that serves it."""
    global _session, _server, _thread

    close_session()      # never two at once, so the old token dies first

    # Decided before the server library is imported, so a machine with nothing
    # to offer the phone fails fast and without loading anything it will not use.
    ip = lan_ip()
    if ip.startswith("127."):
        return {"ok": False, "error":
                "This machine has no network address I can offer the phone, so it "
                "is probably not on wifi. Connect it to the same network as the "
                "phone and try again."}

    from werkzeug.serving import make_server

    code  = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    token = secrets.token_urlsafe(24)
    d     = tempfile.mkdtemp(prefix="kam_scan_")

    try:
        srv = make_server(ip, SCAN_PORT, _phone_app, threaded=True)
    except OSError as e:
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": False, "error": f"Could not open port {SCAN_PORT} on {ip}: {e}"}

    with _lock:
        _session = {"code": code, "token": token, "dir": d, "pages": [],
                    "opened": _now(), "touched": _now(), "ip": ip}
        _server  = srv
    _thread = threading.Thread(target=srv.serve_forever, daemon=True,
                               name="kam-scan-host")
    _thread.start()
    url = f"http://{ip}:{SCAN_PORT}/?k={token}"
    print(f"[SCAN] listening on {ip}:{SCAN_PORT} for session {code}")
    return {"ok": True, "code": code, "url": url, "ip": ip,
            "port": SCAN_PORT, "expires_in": SESSION_TTL}


def close_session():
    """Stop the listener, forget the token, and delete the photos."""
    global _session, _server, _thread
    with _lock:
        srv, sess = _server, _session
        _server, _session = None, None
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    if sess is not None:
        # The photos were only ever a step on the way to text, so they go with
        # the session rather than accumulating in temp.
        shutil.rmtree(sess["dir"], ignore_errors=True)
        print(f"[SCAN] session {sess['code']} closed, {len(sess['pages'])} page(s) discarded")
    _thread = None
    return {"ok": True}


def session_status():
    """What the dashboard polls: is it open, where, and how many pages so far."""
    with _lock:
        sess = _session
        if sess is None:
            return {"open": False}
        if _expired(sess):
            expired = True
        else:
            expired = False
    if expired:
        close_session()
        return {"open": False, "expired": True}
    with _lock:
        return {"open": True, "code": sess["code"], "ip": sess["ip"],
                "port": SCAN_PORT,
                "url": f"http://{sess['ip']}:{SCAN_PORT}/?k={sess['token']}",
                "pages": len(sess["pages"]),
                "seconds_left": max(0, int(SESSION_TTL - (_now() - sess["touched"])))}


def read_page(index):
    """One received photo, for the dashboard to run OCR over. None if absent."""
    with _lock:
        sess = _session
        if sess is None or index < 0 or index >= len(sess["pages"]):
            return None
        path = sess["pages"][index]["path"]
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def drop_page(index):
    """Forget one photo once its text has been read off it."""
    with _lock:
        sess = _session
        if sess is None or index < 0 or index >= len(sess["pages"]):
            return {"ok": False, "error": "no such page"}
        page = sess["pages"][index]
        page["done"] = True
        _touch(sess)
    try:
        os.remove(page["path"])
    except OSError:
        pass
    return {"ok": True}
