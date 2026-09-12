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

Things learned the hard way, all of which cost an afternoon:

  * Chrome starts the host through cmd.exe. The process tree of a working launch
    on Chrome 153 reads chrome.exe -> cmd.exe /d /s /c "...kam_host.exe" ->
    kam_host.exe. An earlier version of this file claimed Chrome had stopped
    using cmd.exe and so refused .bat hosts; the process tree shows that was
    wrong, and the .exe is kept because it works, not because a .bat cannot.
    What going through cmd.exe does mean is that a path containing & | < > ^ %
    or parentheses can be mangled on the way in, and every per-user path holds
    the username, so the manifest records the short 8.3 form of any such path.

  * .NET's Process class cannot give a child the parent's pipes. With
    RedirectStandardInput it inserts a buffer that never reaches the pipe, and
    without it, it passes bInheritHandles=false and the child gets no stdin at
    all. Chrome talks to the host over those pipes, so the launcher calls
    CreateProcess itself with STARTF_USESTDHANDLES and bInheritHandles=true,
    which is exactly what cmd.exe used to do for us.

  * When the button reports "host not found" with a registration that checks
    out, fully quitting Chrome has cleared it every time it has been seen,
    including its background processes, which closing the windows leaves
    running. Why is not established. The launcher writes every launch to
    host.log, so the next occurrence can say whether Chrome started it at all.
"""
import hashlib
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
def launcher_staged(): return os.path.join(install_dir(), "kam_host.new.exe")
def launcher_old():    return os.path.join(install_dir(), "kam_host.exe.old")
def launcher_src():    return os.path.join(install_dir(), "kam_host_launcher.cs")
def config_path():     return os.path.join(install_dir(), "kam_host.cfg")
def manifest_path():   return os.path.join(install_dir(), f"{HOST_NAME}.json")
def launcher_log():    return os.path.join(install_dir(), "host.log")


def launcher_sha():
    """A fingerprint of the launcher source this file would build.

    Recorded in kam_host.cfg when a launcher is installed, so ensure_registered
    can tell an installed launcher is older than the code. Without it a fix to
    the launcher never reaches a machine that is already registered, because
    nothing rebuilds a launcher that exists. A hash rather than a version number
    so there is nothing to remember to bump."""
    return hashlib.sha256(_launcher_source().encode("utf-8")).hexdigest()[:16]


# --- Paths that survive cmd.exe ---------------------------------------------
# Chrome hands the manifest's path to cmd.exe /d /s /c, and cmd treats these as
# operators, escapes or variable markers. Spaces are fine, since Chrome quotes
# the path. Every per-user path contains the username, so someone called
# "Smith & Jones" or "Test (2)" gets a launcher path cmd will cut in half.

_CMD_UNSAFE = set('&|<>^%()')


def _short_path(path):
    """The 8.3 form of an existing path, or the path unchanged if Windows has
    short names turned off for that volume or the call fails."""
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        need = fn(path, None, 0)
        if not need:
            return path
        buf = ctypes.create_unicode_buffer(need)
        return buf.value if fn(path, buf, need) else path
    except Exception:
        return path


def cmd_safe_path(path):
    """path, or its short form when the long one has characters cmd.exe would
    interpret. Returns the long path when no safe form exists, and the caller
    warns, since a path that might break is better than no registration."""
    if not any(ch in _CMD_UNSAFE for ch in path):
        return path
    short = _short_path(path)
    return short if not any(ch in _CMD_UNSAFE for ch in short) else path


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


def write_config(python_exe, host_script, launcher_sha=None):
    """Write the two paths the launcher needs. launcher_sha records which build
    of the launcher is installed; left as None it keeps whatever was recorded,
    so rewriting the paths after a move cannot make a stale launcher look
    current, or a current one look stale."""
    if launcher_sha is None:
        launcher_sha = read_config().get("launcher_sha", "")
    os.makedirs(install_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Written by register_host.py and kept current by server.py.\n")
        f.write("# The launcher reads this at run time, so moving the project\n")
        f.write("# only changes these lines and never needs a recompile.\n")
        f.write(f"python={python_exe}\n")
        f.write(f"host={host_script}\n")
        f.write(f"launcher_sha={launcher_sha}\n")
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

    // Answers "did Chrome even launch this", which is otherwise invisible from
    // outside. Every launch is written, not only failures: a log that only
    // records errors reads exactly like "never launched" when everything is
    // working, which is how a healthy launcher once looked broken for hours.
    // Started afresh past 64KB so it never grows without bound.
    static void Note(string s) {
        try {
            var path = Path.Combine(Dir, "host.log");
            var line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + s + Environment.NewLine;
            if (File.Exists(path) && new FileInfo(path).Length > 64 * 1024) File.WriteAllText(path, line);
            else File.AppendAllText(path, line);
        }
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
        Note("launched " + string.Join(" ", args));
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
    """Compile the launcher to a staging name beside the real one.

    Never straight over the installed launcher: that one may be running right
    now, since the server that calls ensure_registered on boot was usually
    started through it, and a failed build must leave a working button behind.
    Returns the staged path, or None with a reason printed."""
    csc = _find_csc()
    if not csc:
        print("[ERROR] No C# compiler found under %WINDIR%\\Microsoft.NET.")
        print("        That ships with Windows, so something is unusual here.")
        return None
    os.makedirs(install_dir(), exist_ok=True)
    with open(launcher_src(), "w", encoding="utf-8") as f:
        f.write(_launcher_source())
    staged = launcher_staged()
    try:
        os.remove(staged)
    except OSError:
        pass
    r = subprocess.run([csc, "-nologo", "-optimize+", "-target:exe",
                        "-out:" + staged, launcher_src()],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(staged):
        print("[ERROR] The launcher did not compile:")
        print((r.stdout + r.stderr).strip())
        return None
    return staged


def install_built(staged):
    """Move a staged, already-tested launcher into place.

    Windows will not overwrite or delete an executable that is running, but it
    will rename one, and the running process carries on from the renamed file.
    So the current launcher is moved aside to .old first and the new one takes
    its name. If the second step fails the first is undone, so there is never a
    moment where the registered path points at nothing. The .old from a previous
    swap is cleared when it is no longer running. Returns True on success."""
    target, old = launcher_path(), launcher_old()
    try:
        os.remove(old)
    except OSError:
        pass
    moved_aside = False
    if os.path.exists(target):
        try:
            os.replace(target, old)
            moved_aside = True
        except OSError as e:
            print(f"[ERROR] Could not move the current launcher aside: {e}")
            return False
    try:
        os.replace(staged, target)
        return True
    except OSError as e:
        print(f"[ERROR] Could not put the new launcher in place: {e}")
        if moved_aside:
            try:
                os.replace(old, target)
            except OSError:
                pass
        return False


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


def _delete_registry():
    """Remove the key. Every registry touch goes through one of these three
    functions and nothing else calls winreg, so a test can replace all three and
    be certain it cannot reach the real registration. That is not hypothetical
    tidiness: the first version of the suite redirected the install directory
    but left the write going to the real key, so running the tests pointed
    Chrome at a temp folder and then deleted it."""
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}")
        return True
    except FileNotFoundError:
        return False


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

    # Paths first, since the self-test runs the staged launcher and it reads
    # these. The build fingerprint is only recorded once the new launcher is
    # actually in place.
    write_config(py, HOST_SCRIPT)
    staged = build_launcher()
    if not staged:
        return False
    if not self_test(staged):
        print("[ERROR] The launcher was built but did not answer a message.")
        print(f"        Interpreter: {py}")
        print("        Check that it can run kam_host.py by hand before registering.")
        print("        The launcher that was already installed has been left alone.")
        try:
            os.remove(staged)
        except OSError:
            pass
        return False
    if not install_built(staged):
        return False
    write_config(py, HOST_SCRIPT, launcher_sha=launcher_sha())

    exec_path = cmd_safe_path(launcher_path())
    if any(ch in _CMD_UNSAFE for ch in exec_path) and not quiet:
        print("[WARN] The launcher path contains characters cmd.exe treats specially,")
        print("       and Windows has no short name for it on this drive, so Chrome")
        print("       may fail to start it. Setting LOCALAPPDATA to a simpler path")
        print("       and re-running this is the fix if the power button fails.")
    write_manifest(ext_id, exec_path)
    _write_registry()
    if not quiet:
        print(f"[OK] Registered and tested for extension {ext_id}")
        print(f"     Launcher: {launcher_path()}")
        print(f"     Config:   {config_path()}")
        print(f"     Python:   {py}")
        print()
        print("If the power button was already failing, QUIT CHROME COMPLETELY and")
        print("reopen it. Closing the windows is not enough while background apps")
        print("stay on: check chrome://settings/system, or the Chrome icon in the")
        print("tray. host.log beside the launcher records every launch.")
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
        outdated = cfg.get("launcher_sha") != launcher_sha()
        manifest_exec = None
        try:
            with open(manifest_path(), encoding="utf-8") as f:
                manifest_exec = json.load(f).get("path")
        except (OSError, ValueError):
            pass
        missing = (not os.path.exists(launcher_path())
                   or manifest_exec != cmd_safe_path(launcher_path())
                   or _registry_value() != manifest_path())

        # Clear the launcher a previous upgrade moved aside, once it has
        # stopped running. Harmless if it is still in use: that just fails.
        try:
            os.remove(launcher_old())
        except OSError:
            pass

        if not stale and not missing and not outdated:
            return True

        if missing or outdated:
            if verbose:
                why = ("is missing or points elsewhere" if missing
                       else "was built from older code")
                print(f"[HOST] Native-messaging launcher {why}; rebuilding it.")
            ok = register(PINNED_EXTENSION_ID, py, quiet=not verbose)
            if verbose and ok:
                print("[HOST] Repaired. Fully quit Chrome once if the power button was failing.")
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
        print("[OK] Registry key removed." if _delete_registry()
              else "[--] No registry key found.")
    else:
        for base in ("~/Library/Application Support/Google/Chrome/NativeMessagingHosts",
                     "~/.config/google-chrome/NativeMessagingHosts"):
            p = os.path.join(os.path.expanduser(base), f"{HOST_NAME}.json")
            if os.path.exists(p):
                os.remove(p)
                print(f"[OK] Removed {p}")
    for p in (manifest_path(), launcher_path(), launcher_staged(), launcher_old(),
              launcher_src(), config_path(), launcher_log()):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass   # still running; it goes on the next remove


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
