#!/usr/bin/env python3
"""
KAM TTS — Native Messaging Host registration
============================================

Chrome cannot launch a Python script, so it launches a small executable that
launches Python. This sets that up:

  1. Installs a launcher into %LOCALAPPDATA%\\KAMTTS, away from the project, so
     moving or renaming the project folder cannot break it.
  2. Writes kam_host.cfg beside the launcher saying which interpreter to use and
     where kam_host.py lives. The launcher reads that at RUN time, so a move
     only rewrites two lines of text and never needs a recompile.
  3. Writes the Chrome manifest and the registry key that points at it.

Normally you never run this. server.py checks its own registration on every boot
and repairs it, so installing once and then moving the project is enough: start
the server and it fixes itself. See ensure_registered().

    python register_host.py            install or repair
    python register_host.py --remove   uninstall
    python register_host.py <ID>       only if you replaced the key in manifest.json

Three things learned the hard way, all of which cost an afternoon:

  * Chrome 113 and later invoke native hosts as executables directly instead of
    through cmd.exe, so the .bat this used to write is refused at manifest level
    with "Specified native messaging host not found" even when every path is
    perfect. It has to be a real .exe.

  * .NET's Process class cannot give a child the parent's pipes. With
    RedirectStandardInput it inserts a buffer that never reaches the pipe, and
    without it, it passes bInheritHandles=false and the child gets no stdin at
    all. Chrome talks to the host over those pipes, so the launcher calls
    CreateProcess itself with STARTF_USESTDHANDLES and bInheritHandles=true,
    which is exactly what cmd.exe used to do for us.

  * Chrome caches the host lookup for the life of the browser process, and
    closing every window does not end that process when background apps are on.
    A registration change needs Chrome fully quit, not just closed.
"""
import json
import os
import re
import stat
import struct
import subprocess
import sys
import threading

# The help text has non-ASCII in it, and a legacy console codepage turns that
# into a UnicodeEncodeError before the first print. So this runs first.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST_NAME = "com.kam.tts"

# Extension ids are always 32 letters in a-p. Worth checking, since a typo here
# registers a host nothing can talk to and the failure only shows up much later
# as the power button doing nothing.
_EXT_ID_RE = re.compile(r"^[a-p]{32}$")

# manifest.json pins a public key, so Chrome derives the same id on every
# machine and there is nothing to look up.
PINNED_EXTENSION_ID = "mdhbimlofbadmgombcdmnmnebgglalob"

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(HERE, "kam_host.py")


# --- Where the launcher lives -----------------------------------------------
# Deliberately not in the project. The project is something you move, rename and
# sync; the registry points at an absolute path and does not follow it. Keeping
# the launcher in a fixed place under LOCALAPPDATA means a move only has to
# update the config file it reads, which server.py does on its own.
#
# No spaces in the directory name, since every layer here has to quote paths
# correctly and the one native host on this machine that never broke has no
# spaces in its path either.

def install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "KAMTTS")


def launcher_path():   return os.path.join(install_dir(), "kam_host.exe")
def launcher_src():    return os.path.join(install_dir(), "kam_host_launcher.cs")
def config_path():     return os.path.join(install_dir(), "kam_host.cfg")
def manifest_path():   return os.path.join(install_dir(), f"{HOST_NAME}.json")
def launcher_log():    return os.path.join(install_dir(), "host.log")


def _python_exe():
    """The interpreter to run kam_host.py with.

    Defaults to the one running this, which is right in every normal case: you
    install with the Python you set the project up in, and that is the one with
    torch. server.py overrides it with its own sys.executable on every boot, so
    even a wrong answer here is corrected the first time the server starts."""
    return sys.executable or "python"


# --- The config the launcher reads at run time -------------------------------
# A flat key=value file rather than JSON, because the launcher is C# compiled
# against the .NET Framework, which has no JSON parser without dragging in a
# reference. Two keys do not justify that.

def read_config():
    out = {}
    try:
        with open(config_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_config(python_exe, host_script):
    os.makedirs(install_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Written by register_host.py and kept current by server.py.\n")
        f.write("# The launcher reads this at run time, so moving the project\n")
        f.write("# only changes these two lines and never needs a recompile.\n")
        f.write(f"python={python_exe}\n")
        f.write(f"host={host_script}\n")
    return config_path()


# --- The launcher -------------------------------------------------------------

def _find_csc():
    """The C# compiler inside the .NET Framework, which every Windows has, so
    the launcher can be built without downloading a toolchain."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for tree in ("Framework64", "Framework"):
        p = os.path.join(windir, "Microsoft.NET", tree, "v4.0.30319", "csc.exe")
        if os.path.exists(p):
            return p
    return None


def _launcher_source():
    """The launcher's source. It hardcodes nothing except its own config file.

    CreateProcess is called directly rather than through System.Diagnostics
    because Chrome hands the host its pipes as standard handles and the child
    has to inherit those exact handles. See the module docstring."""
    return r'''// Generated by register_host.py. Re-run that rather than editing this.
// Launches kam_host.py with the interpreter named in kam_host.cfg, handing the
// child the pipes Chrome gave us. Nothing about the project is compiled in, so
// moving it only changes the .cfg.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

class KamHostLauncher {
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb; public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string app, StringBuilder cmd, IntPtr pa, IntPtr ta,
        bool inherit, uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GetStdHandle(int n);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetHandleInformation(IntPtr h, uint mask, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr h, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr h);

    const int STD_INPUT = -10, STD_OUTPUT = -11, STD_ERROR = -12;
    const uint HANDLE_FLAG_INHERIT = 1, STARTF_USESTDHANDLES = 0x100,
               CREATE_NO_WINDOW = 0x08000000, INFINITE = 0xFFFFFFFF;

    static string Dir { get { return Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location); } }

    // Kept small and always overwritten: it exists to answer "did Chrome even
    // launch this", which is otherwise invisible from outside.
    static void Note(string s) {
        try { File.AppendAllText(Path.Combine(Dir, "host.log"),
              DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + s + Environment.NewLine); }
        catch (Exception) { }
    }

    static Dictionary<string, string> ReadConfig() {
        var cfg = new Dictionary<string, string>();
        var path = Path.Combine(Dir, "kam_host.cfg");
        foreach (var raw in File.ReadAllLines(path)) {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#")) continue;
            var i = line.IndexOf('=');
            if (i > 0) cfg[line.Substring(0, i).Trim()] = line.Substring(i + 1).Trim();
        }
        return cfg;
    }

    static int Main(string[] args) {
        string py, host;
        try {
            var cfg = ReadConfig();
            py = cfg.ContainsKey("python") ? cfg["python"] : null;
            host = cfg.ContainsKey("host") ? cfg["host"] : null;
        } catch (Exception e) {
            Note("cannot read kam_host.cfg: " + e.Message); return 1;
        }
        if (string.IsNullOrEmpty(py) || string.IsNullOrEmpty(host)) { Note("kam_host.cfg is incomplete"); return 1; }
        if (!File.Exists(py))   { Note("interpreter is missing: " + py + " (start the server once to refresh this)"); return 1; }
        if (!File.Exists(host)) { Note("kam_host.py is missing: " + host + " (start the server once to refresh this)"); return 1; }

        var si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        si.hStdInput  = GetStdHandle(STD_INPUT);
        si.hStdOutput = GetStdHandle(STD_OUTPUT);
        si.hStdError  = GetStdHandle(STD_ERROR);
        si.dwFlags    = (int)STARTF_USESTDHANDLES;
        // Chrome's pipes have to be marked inheritable or the child cannot get
        // them, and then Chrome talks to a process that never answers.
        foreach (var h in new[] { si.hStdInput, si.hStdOutput, si.hStdError })
            if (h != IntPtr.Zero && h.ToInt64() != -1)
                SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);

        var cmd = new StringBuilder();
        cmd.Append('"').Append(py).Append("\" \"").Append(host).Append('"');
        foreach (var a in args) cmd.Append(" \"").Append(a.Replace("\"", "\\\"")).Append('"');
        Environment.SetEnvironmentVariable("KAM_PYTHON", py);   // inherited: env block stays null

        PROCESS_INFORMATION pi;
        bool ok = CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, true, CREATE_NO_WINDOW,
                                IntPtr.Zero, Path.GetDirectoryName(host), ref si, out pi);
        if (!ok) { Note("CreateProcess failed, error " + Marshal.GetLastWin32Error()); return 1; }
        WaitForSingleObject(pi.hProcess, INFINITE);
        uint code; GetExitCodeProcess(pi.hProcess, out code);
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        return (int)code;
    }
}
'''


def build_launcher():
    """Compile the launcher. Returns its path, or None with a reason printed."""
    csc = _find_csc()
    if not csc:
        print("[ERROR] No C# compiler found under %WINDIR%\\Microsoft.NET.")
        print("        That ships with Windows, so something is unusual here.")
        return None
    os.makedirs(install_dir(), exist_ok=True)
    with open(launcher_src(), "w", encoding="utf-8") as f:
        f.write(_launcher_source())
    r = subprocess.run([csc, "-nologo", "-optimize+", "-target:exe",
                        "-out:" + launcher_path(), launcher_src()],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(launcher_path()):
        print("[ERROR] The launcher did not compile:")
        print((r.stdout + r.stderr).strip())
        return None
    return launcher_path()


def self_test(exe, timeout=15):
    """Send a real message and require a real reply.

    Two exchanges, not one. The version of this that only sent a ping passed
    against a launcher that could not relay anything afterwards, because the
    reply arrived on shutdown rather than live. Requiring an answer to "status"
    within a few seconds, while the pipe is still open, is what proves the
    thing actually works."""
    def frame(obj):
        b = json.dumps(obj).encode("utf-8")
        return struct.pack("<I", len(b)) + b

    try:
        p = subprocess.Popen([exe, f"chrome-extension://{PINNED_EXTENSION_ID}/"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        print(f"[ERROR] The launcher would not start: {e}")
        return False

    replies = []

    def reader():
        while True:
            head = p.stdout.read(4)
            if len(head) < 4:
                break
            body = p.stdout.read(struct.unpack("<I", head)[0])
            try:
                replies.append(json.loads(body.decode("utf-8")))
            except Exception:
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        p.stdin.write(frame({"cmd": "status"}))
        p.stdin.flush()
    except OSError:
        pass
    import time
    deadline = time.time() + timeout
    while time.time() < deadline and not replies:
        time.sleep(0.2)
    live = bool(replies)
    if live:
        try:
            p.stdin.write(frame({"cmd": "ping"}))
            p.stdin.flush()
        except OSError:
            pass
        deadline = time.time() + 5
        while time.time() < deadline and len(replies) < 2:
            time.sleep(0.2)
    try:
        p.stdin.close()
        p.wait(timeout=6)
    except Exception:
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return len(replies) >= 2


# --- The Chrome side ----------------------------------------------------------

def write_manifest(ext_id, exec_path):
    os.makedirs(install_dir(), exist_ok=True)
    manifest = {
        "name": HOST_NAME,
        "description": "KAM TTS server launcher",
        "path": exec_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }
    with open(manifest_path(), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    return manifest_path()


def _write_registry():
    import winreg
    key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, manifest_path())


def _registry_value():
    import winreg
    try:
        key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, "")[0]
    except OSError:
        return None


def register(ext_id, python_exe=None, quiet=False):
    """Install or repair everything. Returns True when Chrome can use it."""
    py = python_exe or _python_exe()

    if os.name != "nt":
        # POSIX: Chrome runs the script directly, so there is no launcher.
        os.chmod(HOST_SCRIPT, os.stat(HOST_SCRIPT).st_mode | stat.S_IEXEC)
        write_manifest(ext_id, HOST_SCRIPT)
        target_dir = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/NativeMessagingHosts"
            if sys.platform == "darwin" else
            "~/.config/google-chrome/NativeMessagingHosts")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, f"{HOST_NAME}.json"), "w") as f:
            json.dump(json.load(open(manifest_path())), f, indent=2)
        if not quiet:
            print(f"[OK] Registered for extension {ext_id}")
        return True

    write_config(py, HOST_SCRIPT)
    if not build_launcher():
        return False
    if not self_test(launcher_path()):
        print("[ERROR] The launcher was built but did not answer a message.")
        print(f"        Interpreter: {py}")
        print("        Check that it can run kam_host.py by hand before registering.")
        return False
    write_manifest(ext_id, launcher_path())
    _write_registry()
    if not quiet:
        print(f"[OK] Registered and tested for extension {ext_id}")
        print(f"     Launcher: {launcher_path()}")
        print(f"     Config:   {config_path()}")
        print(f"     Python:   {py}")
        print()
        print("If the power button was already failing, QUIT CHROME COMPLETELY and")
        print("reopen it. Chrome caches this lookup for the life of the browser, and")
        print("closing every window does not end it when background apps stay on:")
        print("check chrome://settings/system, or the Chrome icon in the tray.")
    return True


def ensure_registered(python_exe=None, verbose=True):
    """Check the registration and repair it if it has gone stale.

    Called by server.py on every boot, which is what makes moving the project
    survivable: the server knows where it lives and which interpreter it is
    running under, so it can correct both without anyone being told to run a
    script. Cheap in the normal case, since nothing is rebuilt when the config
    already matches. Never raises: a broken power button must not stop the
    server from serving."""
    if os.name != "nt":
        return True
    try:
        py = python_exe or _python_exe()
        cfg = read_config()
        stale = (cfg.get("python") != py or cfg.get("host") != HOST_SCRIPT)
        missing = (not os.path.exists(launcher_path())
                   or not os.path.exists(manifest_path())
                   or _registry_value() != manifest_path())

        if not stale and not missing:
            return True

        if missing:
            if verbose:
                print("[HOST] Native-messaging registration is missing or points elsewhere; "
                      "rebuilding it.")
            ok = register(PINNED_EXTENSION_ID, py, quiet=not verbose)
            if verbose and ok:
                print("[HOST] Repaired. Fully quit Chrome once for it to take effect.")
            return ok

        # Only the paths moved, so the existing launcher is fine and the config
        # is all that has to change. No compile, no registry write.
        write_config(py, HOST_SCRIPT)
        if verbose:
            print(f"[HOST] Registration updated for this location ({HERE}).")
        return True
    except Exception as e:
        if verbose:
            print(f"[HOST] Could not verify the native-messaging registration ({e}). "
                  f"The power button may not work; everything else is unaffected.")
        return False


def remove(ext_id):
    """Uninstall: registry key, manifest, launcher, config and log."""
    if os.name == "nt":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}")
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
    for p in (manifest_path(), launcher_path(), launcher_src(), config_path(), launcher_log()):
        if os.path.exists(p):
            os.remove(p)


def _parse_ext_id(raw):
    """Pull the extension id out of whatever was pasted: the bare id, or the
    dashboard URL, since copying that from the address bar is easier."""
    for part in re.split(r"[/\s]+", raw.strip()):
        part = part.strip().lower()
        if _EXT_ID_RE.match(part):
            return part
    return None


def main():
    args = list(sys.argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--python":
            skip = True
        elif not a.startswith("-"):
            positional.append(a)
    if not positional:
        ext_id = PINNED_EXTENSION_ID
        print(f"Using the pinned extension id: {ext_id}")
    else:
        ext_id = _parse_ext_id(positional[0])
        if not ext_id:
            print(f"ERROR: '{positional[0]}' is not a Chrome extension id.")
            print("Expected 32 letters in the range a-p, copied from chrome://extensions")
            return 2
    if "--remove" in args:
        remove(ext_id)
        return 0
    python_exe = None
    if "--python" in args:
        i = args.index("--python")
        if i + 1 < len(args):
            python_exe = args[i + 1]
    # A failed self-test is a real failure, and setup_kam.py reads this code.
    return 0 if register(ext_id, python_exe) else 3


if __name__ == "__main__":
    sys.exit(main())
