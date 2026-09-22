"""Test updater.py: which versions count as newer, what GitHub's reply is read
as, and the two ways a copy is updated - a git checkout fast-forwarded, a
release zip swapped in - including every way each must refuse and leave the
copy exactly as it was. Nothing here touches the network or the real install:
GitHub is a stub, the git remote is a bare repo in a temp folder, and pip is
never run."""
import hashlib, io, json, os, pathlib, shutil, stat, subprocess, sys, tempfile, zipfile

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import updater as U  # noqa: E402

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want: PASS += 1; print(f"  ok   {label}")
    else: FAIL += 1; print(f"  FAIL {label}\n         got  {got}\n         want {want}")

def refused(fn):
    """The UpdateError message, or None if fn did not refuse."""
    try:
        fn()
    except U.UpdateError as e:
        return str(e)
    return None

def write(root, rel, text):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")

def read(root, rel):
    """The file's text, or None if it is gone - so a lost file fails a check rather than the run."""
    p = pathlib.Path(root) / rel
    return p.read_text(encoding="utf-8") if p.exists() else None

def manifest(v):
    return json.dumps({"manifest_version": 3, "name": "KAM TTS", "version": v})

# --- versions -----------------------------------------------------------------
print("versions")
check("v0.10.0 reads as (0, 10, 0)", U.parse_version("v0.10.0"), (0, 10, 0))
check("0.10.0 is newer than 0.9.9, not older", U.parse_version("0.10.0") > U.parse_version("0.9.9"), True)
check("a pre-release is not a version", U.parse_version("v1.0.0-rc1"), None)
check("nor is a two-part number", U.parse_version("0.9"), None)

# --- requirements -------------------------------------------------------------
print("requirements")
old = "# comment\ntorch\ntorchaudio\nflask\ncoqui-tts==0.27.5\n"
new = "# comment changed\ntorch>=2.9\ntorchaudio\nflask\ncoqui-tts==0.28.0\nlibrosa\n"
check("only new or changed lines, and never torch", U.new_requirements(old, new), ["coqui-tts==0.28.0", "librosa"])
check("nothing new, nothing to install", U.new_requirements(old, old), [])

calls = []
class Done:  # a finished process
    def __init__(self, code=0, out="", err=""): self.returncode, self.stdout, self.stderr = code, out, err
def fake_run(cmd, **kw):
    calls.append(cmd)
    held = pathlib.Path(cmd[cmd.index("-c") + 1]).read_text(encoding="utf-8")
    calls.append(held)
    return Done()
U.install_requirements(["librosa"], run=fake_run)
check("pip is asked for exactly the new lines", calls[0][-1:], ["librosa"])
check("with a constraints file holding torch where it is", "-c" in calls[0], True)
check("nothing to install runs nothing", (U.install_requirements([], run=lambda *a, **k: 1 / 0)), "")
check("a pip failure is a refusal", refused(lambda: U.install_requirements(["x"], run=lambda c, **k: Done(1, "", "ERROR: no such package"))) is not None, True)

# --- GitHub's reply -----------------------------------------------------------
print("GitHub's reply")
def release_json(version, zip_bytes=b"", digest=None, **extra):
    d = {"tag_name": f"v{version}", "name": f"KAM TTS {version} - a change", "html_url": "https://example.invalid/rel",
         "draft": False, "prerelease": False,
         "assets": [{"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/sums"},
                    {"name": f"KAM-TTS-v{version}.zip", "browser_download_url": f"https://example.invalid/KAM-TTS-v{version}.zip",
                     "digest": "sha256:" + (digest or hashlib.sha256(zip_bytes).hexdigest())}]}
    d.update(extra)
    return json.dumps(d).encode()

serve = {}
U._get = lambda url, timeout=30: serve[url] if url in serve else (_ for _ in ()).throw(OSError(f"no stub for {url}"))
serve[U.LATEST_API] = release_json("0.10.0", b"zip")
r = U.latest_release()
check("the version comes from the tag", r["version"], "0.10.0")
check("the download is the zip, not the checksum file", r["download"], "https://example.invalid/KAM-TTS-v0.10.0.zip")
check("the checksum is GitHub's digest", r["sha256"], hashlib.sha256(b"zip").hexdigest())
serve[U.LATEST_API] = release_json("0.10.0", b"zip", prerelease=True)
check("a pre-release is not offered", refused(U.latest_release) is not None, True)

# --- a release zip ------------------------------------------------------------
print("a release zip")
def make_install(root):
    write(root, "extension/manifest.json", manifest("0.9.0"))
    write(root, "extension/dashboard.js", "old dashboard")
    write(root, "server/server.py", "old server")
    write(root, "server/requirements.txt", "flask\n")
    write(root, "server/pronunciation_store.json", "what I taught it")
    write(root, "server/voice_samples/a.wav", "my recording")
    write(root, "server/zz_last.txt", "old last")

def make_zip(version="0.10.0", says=None, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        files = {
            "extension/manifest.json": manifest(says or version),
            "extension/dashboard.js": "new dashboard",
            "server/server.py": "new server",
            "server/requirements.txt": "flask\nlibrosa\n",
            "server/pronunciation_store.json": "the shipped defaults",
            "server/voice_samples/a.wav": "shipped sample",
            "server/voice_samples/b.wav": "a new shipped sample",
            "server/zz_last.txt": "new last",
        }
        files.update(extra or {})
        for rel, text in files.items():
            z.writestr(f"KAM TTS/{rel}", text)
    return buf.getvalue()

def release_for(zip_bytes, version="0.10.0", digest=None):
    serve[U.LATEST_API] = release_json(version, zip_bytes, digest)
    serve[f"https://example.invalid/KAM-TTS-v{version}.zip"] = zip_bytes
    return U.latest_release()

installed = []
root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
U.update_zip(release_for(make_zip()), root, install=installed.extend)
check("the code is replaced", (read(root, "server/server.py"), read(root, "extension/dashboard.js")), ("new server", "new dashboard"))
check("the copy now says the new version", U.current_version(root), "0.10.0")
check("what it has learned is kept", read(root, "server/pronunciation_store.json"), "what I taught it")
check("a recording is kept", read(root, "server/voice_samples/a.wav"), "my recording")
check("a new shipped sample is added", read(root, "server/voice_samples/b.wav"), "a new shipped sample")
check("the new package is installed before anything changes", installed, ["librosa"])
check("no working files are left behind", (root / ".kam_update").exists(), False)

def untouched(root):
    manifest_text = read(root, "extension/manifest.json")
    return (read(root, "server/server.py"), json.loads(manifest_text)["version"] if manifest_text else None,
            read(root, "server/zz_last.txt"), (root / ".kam_update").exists())
BEFORE = ("old server", "0.9.0", "old last", False)

root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
why = refused(lambda: U.update_zip(release_for(make_zip(), digest="0" * 64), root, install=lambda l: None))
check("a download that fails its checksum is refused", "did not match the checksum" in (why or ""), True)
check("...and the copy is exactly as it was", untouched(root), BEFORE)

root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
why = refused(lambda: U.update_zip(release_for(make_zip(says="0.9.5")), root, install=lambda l: None))
check("a zip that says it is another version is refused", "says it is version 0.9.5" in (why or ""), True)
check("...and the copy is exactly as it was", untouched(root), BEFORE)

root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
why = refused(lambda: U.update_zip(release_for(make_zip(extra={"../../escaped.txt": "x"})), root, install=lambda l: None))
check("a zip reaching outside its folder is refused", "outside its own folder" in (why or ""), True)
check("...and wrote nothing outside", (root / ".kam_update" / "escaped.txt").exists() or (root / "escaped.txt").exists(), False)

root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
why = refused(lambda: U.update_zip(release_for(make_zip()), root,
                                   install=lambda l: (_ for _ in ()).throw(U.UpdateError("pip said no"))))
check("a package that will not install stops it before any file changes", (why, untouched(root)), ("pip said no", BEFORE))

root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
last = root / "server" / "zz_last.txt"
os.chmod(last, stat.S_IREAD)  # the last file cannot be written, after the others have been
why = refused(lambda: U.update_zip(release_for(make_zip()), root, install=lambda l: None))
os.chmod(last, stat.S_IWRITE | stat.S_IREAD)
check("a copy that fails part-way is refused", "put back" in (why or ""), True)
check("...and every file already replaced is put back", untouched(root), BEFORE)

# --- a git checkout -----------------------------------------------------------
print("a git checkout")
for k, v in {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
    os.environ[k] = v
def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

base = pathlib.Path(tempfile.mkdtemp(prefix="kam_git_"))
origin = base / "origin.git"; git(base, "init", "--bare", "-q", "-b", "main", str(origin))
dev = base / "dev"; git(base, "clone", "-q", str(origin), str(dev))
make_install(dev); git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "0.9.0"); git(dev, "push", "-q", "origin", "HEAD:main")
user = base / "user"; git(base, "clone", "-q", str(origin), str(user))
stale = base / "stale"; git(base, "clone", "-q", str(origin), str(stale))
ahead = base / "ahead"; git(base, "clone", "-q", str(origin), str(ahead))
write(dev, "extension/manifest.json", manifest("0.10.0")); write(dev, "server/server.py", "new server")
write(dev, "server/requirements.txt", "flask\nlibrosa\n")
git(dev, "commit", "-q", "-am", "0.10.0"); git(dev, "push", "-q", "origin", "HEAD:main")

installed = []
U.update_git(user, install=installed.extend)
check("a clean checkout is fast-forwarded", (read(user, "server/server.py"), U.current_version(user)), ("new server", "0.10.0"))
check("its new package is installed first", installed, ["librosa"])

write(stale, "server/server.py", "my own edit")
installed = []
why = refused(lambda: U.update_git(stale, install=installed.extend))
check("a local edit the update touches is refused", why is not None and "server/server.py" in why, True)
check("...before any package is installed for it", installed, [])
check("...and the edit is still there", read(stale, "server/server.py"), "my own edit")

write(ahead, "server/notes.txt", "mine"); git(ahead, "add", "-A"); git(ahead, "commit", "-q", "-m", "local work")
why = refused(lambda: U.update_git(ahead, install=lambda l: None))
check("a checkout with its own commits is left alone", why is not None and "commits GitHub does not have" in why, True)
check("...at its own commit", git(ahead, "log", "-1", "--format=%s").strip(), "local work")

# --- deciding ---------------------------------------------------------------
print("deciding")
root = pathlib.Path(tempfile.mkdtemp(prefix="kam_upd_")); make_install(root)
serve[U.LATEST_API] = release_json("0.9.0", b"zip")
why = refused(lambda: U.apply_latest(root))
check("the same version is not installed over itself", "already 0.9.0" in (why or ""), True)
check("a folder with .git is a checkout, one without is a zip install", (U.install_mode(user), U.install_mode(root)), ("git", "zip"))

for d in [base]:
    shutil.rmtree(d, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
