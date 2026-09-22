"""
updater.py - install the newest KAM TTS over this copy, when the user says so.

Whether there is a newer version is the dashboard's question: it asks GitHub
itself, so it can answer with the server off. This module does the part only
the server can do, which is change the files, and it looks the release up
again itself rather than trusting a download address handed to it.

Two kinds of install are updated differently:

  * A git checkout (a .git folder beside server/ and extension/) is fetched and
    fast-forwarded to the branch it tracks. Anything that would not be a clean
    fast-forward - commits GitHub lacks, local edits the new commits touch - is
    refused with the reason, and the checkout is left exactly as it was.

  * A release zip is downloaded, refused unless its SHA-256 matches the digest
    GitHub publishes for it and it says it is the version the release says, then
    copied over this copy. The files that hold what the app has learned or
    recorded are never replaced. If anything fails part-way, every file already
    replaced is put back.

Python packages come first either way. requirements.txt leaves torch unpinned on
purpose, because it has to match the machine, so an update installs only the
requirement lines that are new, with torch and torchaudio held at the versions
already installed - and does it before any file changes, so a failure leaves
nothing half-updated.

The new code only runs after a restart. The extension does that: it stops the
server, reloads itself, and starts the server again.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO = "Ari-Joon/KAM-TTS"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# The folder that holds server/ and extension/.
APP_ROOT = pathlib.Path(__file__).resolve().parent.parent

# What the app has learned or recorded. A release carries a starting copy of
# some of these; an update never replaces the user's own.
KEEP = ("server/pronunciation_store.json", "server/punctuation_corrections.json", "server/voice_samples/")

# Held at the installed version whatever a requirements change asks for.
HELD = ("torch", "torchaudio")

ASSET = re.compile(r"^KAM-TTS-v\d+\.\d+\.\d+\.zip$", re.IGNORECASE)


class UpdateError(Exception):
    """Why an update did not happen, worded for the person reading the dashboard."""


# ---------------------------------------------------------------- versions ----

def parse_version(text):
    """'v0.10.0' or '0.10.0' -> (0, 10, 0). Anything else, pre-releases included -> None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def current_version(root=APP_ROOT):
    """The version of the copy at root, from the extension's manifest - the one number shown everywhere."""
    manifest = pathlib.Path(root) / "extension" / "manifest.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def install_mode(root=APP_ROOT):
    return "git" if (pathlib.Path(root) / ".git").exists() else "zip"


# ------------------------------------------------------------------ GitHub ----

def _get(url, timeout=30):
    """Bytes from a URL. The one place this module touches the network; tests replace it."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"KAM-TTS/{current_version()}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release():
    """The newest published release: version, title, page, download, sha256."""
    try:
        data = json.loads(_get(LATEST_API))
    except Exception as e:
        raise UpdateError(f"GitHub could not be reached ({e}). Nothing was changed.")
    version = parse_version(data.get("tag_name"))
    if data.get("draft") or data.get("prerelease") or not version:
        raise UpdateError("GitHub's newest release does not name a version. Nothing was changed.")
    asset = next((a for a in data.get("assets") or [] if ASSET.match(a.get("name") or "")), None)
    digest = (asset or {}).get("digest") or ""
    return {
        "version": ".".join(map(str, version)),
        "title": data.get("name") or "",
        "page": data.get("html_url") or RELEASES_PAGE,
        "download": (asset or {}).get("browser_download_url"),
        "sha256": digest[7:] if digest.lower().startswith("sha256:") else None,
    }


# ---------------------------------------------------------------- packages ----

def _requirement_lines(text):
    return {l.strip() for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("#")}


def _requirement_name(line):
    return re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0].strip().lower()


def new_requirements(old_text, new_text):
    """Requirement lines the update adds or changes, never torch or torchaudio."""
    added = _requirement_lines(new_text) - _requirement_lines(old_text)
    return sorted(r for r in added if _requirement_name(r) not in HELD)


def install_requirements(lines, run=subprocess.run):
    """pip install exactly these, with torch and torchaudio held where they are."""
    if not lines:
        return ""
    held = []
    for pkg in HELD:
        try:
            from importlib.metadata import version as _installed
            held.append(f"{pkg}=={_installed(pkg)}")
        except Exception:
            pass  # not installed here; nothing to hold
    with tempfile.TemporaryDirectory(prefix="kam_update_") as d:
        constraints = pathlib.Path(d) / "held.txt"
        constraints.write_text("\n".join(held) + "\n", encoding="utf-8")
        proc = run([sys.executable, "-m", "pip", "install", "-c", str(constraints), *lines],
                   capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        last = [l for l in (proc.stderr or proc.stdout or "").splitlines() if l.strip()][-1:] or ["pip failed"]
        raise UpdateError(f"The new Python packages could not be installed ({last[0].strip()}). Nothing was changed.")
    return proc.stdout or ""


# --------------------------------------------------------------------- git ----

def _git(root, *args, run=subprocess.run):
    git = shutil.which("git")
    if not git:
        raise UpdateError("This copy is a git checkout, but git was not found. Nothing was changed.")
    return run([git, "-C", str(root), *args], capture_output=True, text=True, timeout=300)


def update_git(root=APP_ROOT, run=subprocess.run, install=install_requirements):
    """Fast-forward a checkout to the branch it tracks, or say why not and change nothing."""
    fetched = _git(root, "fetch", "--quiet", run=run)
    if fetched.returncode != 0:
        raise UpdateError(f"git could not fetch from GitHub ({_first(fetched)}). Nothing was changed.")
    upstream = _git(root, "rev-parse", "--abbrev-ref", "@{u}", run=run)
    if upstream.returncode != 0:
        raise UpdateError("This checkout's branch does not track GitHub, so it was left alone. Update it with git yourself.")
    ahead = _git(root, "rev-list", "--count", "@{u}..HEAD", run=run).stdout.strip()
    if ahead not in ("", "0"):
        raise UpdateError("This checkout has commits GitHub does not have, so it was left alone. Update it with git yourself.")

    # Local edits to files the update also changes would stop the merge. Find
    # that out now, before any package is installed for an update that cannot land.
    edited = set(_git(root, "diff", "--name-only", "HEAD", run=run).stdout.split())
    incoming = set(_git(root, "diff", "--name-only", "HEAD", "@{u}", run=run).stdout.split())
    clash = sorted(edited & incoming)
    if clash:
        raise UpdateError(f"This checkout has local changes to {', '.join(clash)}, which the update also changes. "
                          "Your local changes were left exactly as they were; commit or undo them, then update.")

    new_reqs = _git(root, "show", "@{u}:server/requirements.txt", run=run)
    old_reqs = (pathlib.Path(root) / "server" / "requirements.txt")
    if new_reqs.returncode == 0:
        install(new_requirements(old_reqs.read_text(encoding="utf-8") if old_reqs.exists() else "", new_reqs.stdout))

    merged = _git(root, "merge", "--ff-only", "@{u}", run=run)
    if merged.returncode != 0:
        raise UpdateError(f"git could not fast-forward this checkout ({_first(merged)}). "
                          "Your local changes were left exactly as they were.")


def _first(proc):
    lines = [l.strip() for l in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if l.strip()]
    return lines[0] if lines else f"exit {proc.returncode}"


# --------------------------------------------------------------------- zip ----

def download_verified(url, sha256, dest):
    """Save url to dest only if its SHA-256 is the published one. Nothing is left behind otherwise."""
    if not url:
        raise UpdateError("The newest release has no KAM TTS zip to install. Nothing was changed.")
    if not sha256:
        raise UpdateError("GitHub published no checksum for this download, so it was not installed.")
    dest = pathlib.Path(dest)
    partial = dest.with_name(dest.name + ".partial")
    try:
        data = _get(url, timeout=600)
        if hashlib.sha256(data).hexdigest().lower() != sha256.strip().lower():
            raise UpdateError("The download did not match the checksum GitHub published for it, so it was "
                              "deleted. Nothing was changed.")
        partial.write_bytes(data)
        partial.replace(dest)
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def extract_safely(zip_path, dest):
    """Unpack, refusing any entry that would land outside dest. Returns the folder holding server/ and extension/."""
    dest = pathlib.Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise UpdateError("The download contains a path outside its own folder, so it was not installed.")
        z.extractall(dest)
    for candidate in [dest, *sorted(p for p in dest.iterdir() if p.is_dir())]:
        if (candidate / "extension" / "manifest.json").is_file() and (candidate / "server").is_dir():
            return candidate
    raise UpdateError("The download is not a KAM TTS release, so it was not installed.")


def _kept(rel):
    return any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in KEEP)


def swap_in(src, root, backup):
    """Copy every file from src over root, keeping the user's own KEEP files. All or nothing."""
    src, root, backup = pathlib.Path(src), pathlib.Path(root), pathlib.Path(backup)
    replaced = []  # (target, saved copy or None), recorded before each copy so a failed one is undone too
    try:
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = f.relative_to(src).as_posix()
            if rel.startswith(".git/"):
                continue
            target = root / rel
            if _kept(rel) and target.exists():
                continue
            saved = None
            if target.exists():
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            replaced.append((target, saved))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    except Exception as e:
        for target, saved in reversed(replaced):
            try:
                if saved is not None:
                    shutil.copy2(saved, target)
                elif target.exists():
                    target.unlink()
            except Exception:
                pass
        raise UpdateError(f"The update could not be copied in ({e}), so every file was put back as it was.")


def _remove_tree(path):
    """rmtree that also clears the read-only flag Windows puts on some files."""
    def _retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if pathlib.Path(path).exists():
        shutil.rmtree(path, onerror=_retry)


def update_zip(release, root=APP_ROOT, install=install_requirements):
    root = pathlib.Path(root)
    work = root / ".kam_update"
    _remove_tree(work)
    work.mkdir(parents=True)
    try:
        zip_path = work / f"KAM-TTS-v{release['version']}.zip"
        download_verified(release["download"], release["sha256"], zip_path)
        top = extract_safely(zip_path, work / "staged")
        says = current_version(top)
        if says != release["version"]:
            raise UpdateError(f"The download says it is version {says}, not {release['version']}, so it was not installed.")
        old = root / "server" / "requirements.txt"
        new = top / "server" / "requirements.txt"
        install(new_requirements(old.read_text(encoding="utf-8") if old.exists() else "",
                                 new.read_text(encoding="utf-8") if new.exists() else ""))
        swap_in(top, root, work / "backup")
    finally:
        _remove_tree(work)


# ------------------------------------------------------------------- apply ----

def apply_latest(root=APP_ROOT):
    """Install the newest release over this copy. Returns what happened; raises UpdateError when it did not."""
    root = pathlib.Path(root)
    before = current_version(root)
    release = latest_release()
    if not parse_version(before) or parse_version(release["version"]) <= parse_version(before):
        raise UpdateError(f"This copy is already {before}, and GitHub's newest is {release['version']}.")
    mode = install_mode(root)
    if mode == "git":
        update_git(root)
    else:
        update_zip(release, root)
    return {"ok": True, "from": before, "to": current_version(root), "how": mode,
            "restart": True, "page": release["page"]}
