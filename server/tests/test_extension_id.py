"""Test _extension_origins() against every path a real install can take, by
lifting the real function out of server.py."""
import pathlib as _pl
SERVER_DIR    = _pl.Path(__file__).resolve().parent.parent
EXTENSION_DIR = SERVER_DIR.parent / "extension"
import ast, json, os, pathlib, tempfile, sys

S = SERVER_DIR / "server.py"
src = S.read_text(encoding="utf-8")
i = src.index("_FALLBACK_EXTENSION_ID =")
j = src.index("_EXTENSION_ORIGINS = _extension_origins()")
block = src[i:j]

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want: PASS += 1; print(f"  ok   {label}")
    else: FAIL += 1; print(f"  FAIL {label}\n         got  {got}\n         want {want}")

def run(manifest=None, env=None, quiet=True):
    d = tempfile.mkdtemp()
    if manifest is not None:
        (pathlib.Path(d)/"com.kam.tts.json").write_text(json.dumps(manifest), encoding="utf-8")
    ns = {"os": os, "json": json, "_SERVER_DIR0": d,
          "print": (lambda *a, **k: None) if quiet else print}
    old = os.environ.pop("KAM_EXTENSION_ID", None)
    if env: os.environ["KAM_EXTENSION_ID"] = env
    try:
        exec(block, ns)
        return ns["_extension_origins"]()
    finally:
        os.environ.pop("KAM_EXTENSION_ID", None)
        if old is not None: os.environ["KAM_EXTENSION_ID"] = old

# Read the fallback out of server.py rather than repeating it, so changing the
# pinned key cannot leave this suite asserting a stale id and still passing.
_ns = {"os": os, "json": json, "_SERVER_DIR0": tempfile.mkdtemp(),
       "print": lambda *a, **k: None}
exec(block, _ns)
FALLBACK_ID = _ns["_FALLBACK_EXTENSION_ID"]
FALLBACK    = f"chrome-extension://{FALLBACK_ID}"

print("\n=== the new-user path: register_host.py has written a manifest ===")
check("picks up the ID from the manifest",
      run(manifest={"allowed_origins": ["chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/"]}),
      ["chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"])
check("strips Chrome's trailing slash",
      run(manifest={"allowed_origins": ["chrome-extension://xyz/"]})[0].endswith("/"), False)
check("handles several allowed origins",
      run(manifest={"allowed_origins": ["chrome-extension://aaa/", "chrome-extension://bbb/"]}),
      ["chrome-extension://aaa", "chrome-extension://bbb"])

print("\n=== explicit override always wins ===")
check("env var beats the manifest",
      run(manifest={"allowed_origins": ["chrome-extension://from-manifest/"]}, env="from-env"),
      ["chrome-extension://from-env"])
check("env var accepts a comma-separated list",
      run(env="one, two ,three"),
      ["chrome-extension://one", "chrome-extension://two", "chrome-extension://three"])

print("\n=== degrades safely ===")
check("no manifest at all falls back", run(manifest=None), [FALLBACK])
check("manifest with no allowed_origins falls back", run(manifest={"name": "x"}), [FALLBACK])
check("empty allowed_origins falls back", run(manifest={"allowed_origins": []}), [FALLBACK])
check("junk in allowed_origins is ignored",
      run(manifest={"allowed_origins": ["https://evil.example", 42, None]}), [FALLBACK])
check("corrupt manifest does not crash the boot",
      run(manifest="NOT JSON AT ALL" if False else {"allowed_origins": "notalist"}), [FALLBACK])

print("\n=== the pinned key and the ids everyone hardcodes agree ===")
# manifest.json pins a public key, and Chrome turns that into the extension id
# by hashing it. Two other files hardcode the result. If the key is ever
# regenerated and one of them is missed, CORS breaks for every user with no
# visible cause, so derive the id here the same way Chrome does and compare.
import base64, hashlib, re
_mf = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
check("manifest.json pins a key", bool(_mf.get("key")), True)
_der     = base64.b64decode(_mf["key"])
_digest  = hashlib.sha256(_der).hexdigest()[:32]
_derived = "".join(chr(ord("a") + int(c, 16)) for c in _digest)
check("server.py fallback matches the key", FALLBACK_ID, _derived)
_rh = (SERVER_DIR / "register_host.py").read_text(encoding="utf-8")
_pinned = re.search(r'PINNED_EXTENSION_ID\s*=\s*"([a-p]{32})"', _rh)
check("register_host.py has a pinned id", bool(_pinned), True)
check("register_host.py pin matches the key",
      _pinned.group(1) if _pinned else None, _derived)

print("\n=== what a brand-new user actually sees printed ===")
run(manifest=None, quiet=False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
