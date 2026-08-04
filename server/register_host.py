#!/usr/bin/env python3
"""
KAM TTS — Native Messaging Host registration (one-time setup)
=============================================================

Run this ONCE so Chrome trusts the KAM host. It:
  1. Writes the native-messaging host manifest (com.kam.tts.json) next to the
     host, pointing at kam_host.py.
  2. Registers it with Chrome:
       • Windows: a registry key under
         HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.kam.tts
       • macOS/Linux: drops the manifest into Chrome's NativeMessagingHosts dir.

Usage:
    python register_host.py <EXTENSION_ID>

Find <EXTENSION_ID> at chrome://extensions (toggle Developer mode) — it's the
long id under "KAM TTS", e.g. meikdhhiiofnpfidepkcgghjpafoegcj. It is also the
host segment of the dashboard URL: chrome-extension://<EXTENSION_ID>/player.html

To unregister, pass --remove:
    python register_host.py <EXTENSION_ID> --remove
"""
import sys
import os
import json
import stat

HOST_NAME = "com.kam.tts"
HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(HERE, "kam_host.py")
MANIFEST_PATH = os.path.join(HERE, f"{HOST_NAME}.json")


def _python_exe():
    """Absolute path to the interpreter running this script, so the
    native-messaging manifest points at the same Python the server uses."""
    return sys.executable or "python"


def _write_launcher_windows(python_exe=None):
    """On Windows the manifest 'path' must point at an executable. Python .py
    files aren't directly launchable by Chrome, so we emit a small .bat wrapper
    that runs the host with the chosen interpreter. CRITICAL: this must be the
    SAME Python that has torch/TTS installed and runs your server — otherwise
    the server crashes on import. Defaults to the Python running this script."""
    py = python_exe or _python_exe()
    bat = os.path.join(HERE, "kam_host.bat")
    with open(bat, "w") as f:
        # Pass the chosen Python to the host via env so it launches the server
        # with the exact same interpreter.
        f.write(f'@echo off\r\nset "KAM_PYTHON={py}"\r\n"{py}" "{HOST_SCRIPT}" %*\r\n')
    return bat


def _write_manifest(ext_id, exec_path):
    """Write the Chrome native-messaging manifest for this machine.
    Regenerated per install (absolute paths + extension ID), which is why
    com_kam_tts.json is gitignored rather than committed."""
    manifest = {
        "name": HOST_NAME,
        "description": "KAM TTS server launcher",
        "path": exec_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    return MANIFEST_PATH


def register(ext_id, python_exe=None):
    """Install the native-messaging host so the dashboard power button can
    start and stop the server. Writes the manifest and registers it with Chrome."""
    if os.name == "nt":
        exec_path = _write_launcher_windows(python_exe)
    else:
        # POSIX: make the host directly executable.
        os.chmod(HOST_SCRIPT, os.stat(HOST_SCRIPT).st_mode | stat.S_IEXEC)
        exec_path = HOST_SCRIPT
    _write_manifest(ext_id, exec_path)

    if os.name == "nt":
        import winreg
        key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MANIFEST_PATH)
        print(f"[OK] Registered (registry) for extension {ext_id}")
        print(f"     Manifest: {MANIFEST_PATH}")
        print(f"     Launcher: {exec_path}")
    else:
        # Chrome looks in a per-user dir; copy the manifest there.
        if sys.platform == "darwin":
            target_dir = os.path.expanduser(
                "~/Library/Application Support/Google/Chrome/NativeMessagingHosts")
        else:
            target_dir = os.path.expanduser(
                "~/.config/google-chrome/NativeMessagingHosts")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, f"{HOST_NAME}.json")
        with open(target, "w") as f:
            json.dump(json.load(open(MANIFEST_PATH)), f, indent=2)
        print(f"[OK] Registered for extension {ext_id}")
        print(f"     Manifest: {target}")
    print("Restart Chrome, then use the power button in the KAM TTS dashboard.")


def remove(ext_id):
    """Uninstall the native-messaging host and delete the generated manifest."""
    if os.name == "nt":
        import winreg
        key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            print("[OK] Registry key removed.")
        except FileNotFoundError:
            print("[--] No registry key found.")
    else:
        for base in ("~/Library/Application Support/Google/Chrome/NativeMessagingHosts",
                     "~/.config/google-chrome/NativeMessagingHosts"):
            p = os.path.join(os.path.expanduser(base), f"{HOST_NAME}.json")
            if os.path.exists(p):
                os.remove(p)
                print(f"[OK] Removed {p}")
    if os.path.exists(MANIFEST_PATH):
        os.remove(MANIFEST_PATH)


def main():
    """CLI entry point: `register_host.py` installs, `--remove` uninstalls."""
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    ext_id = args[0].strip().strip("/").split("/")[-1]
    if "--remove" in args:
        remove(ext_id)
        return
    # Optional explicit Python path: --python "C:\path\to\python.exe"
    python_exe = None
    if "--python" in args:
        i = args.index("--python")
        if i + 1 < len(args):
            python_exe = args[i + 1]
    register(ext_id, python_exe)


if __name__ == "__main__":
    main()