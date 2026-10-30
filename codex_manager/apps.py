"""
Detect and stop running Codex front-ends (desktop app, remote-control daemon, CLI sessions).

A switch rewrites config.toml and toggles threads in state_5.sqlite. A running app-server keeps
the old provider/config in memory and its sidebar cache, so it must be stopped first.
Linux only (/proc); on other platforms detection returns nothing.
"""

import os
import signal
import subprocess
import sys
import time

REMOTE_UNIT = "codex-rc.service"
DESKTOP_EXE_PREFIX = "/usr/lib/chatgpt/ChatGPT"


def _own_lineage():
    """PIDs of this process and its ancestors (never stop the app that launched the switch)."""
    pids = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                pid = int(f.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def _classify(argv):
    """Return 'desktop', 'remote-daemon', 'cli' or None for a process argv."""
    if not argv:
        return None
    if len(argv) == 1 and " " in argv[0]:
        # Electron/Chromium children rewrite their cmdline into one space-separated string
        argv = argv[0].split()
    exe = argv[0]
    if exe.startswith(DESKTOP_EXE_PREFIX) and not any(a.startswith("--type=") for a in argv[1:]):
        return "desktop"
    if os.path.basename(exe) != "codex":
        return None
    if exe.startswith("/usr/lib/chatgpt/"):
        return None  # app-server owned by the desktop app; stops with it
    if "app-server" in argv:
        return "remote-daemon" if "--remote-control" in argv else None
    if len(argv) > 1 and argv[1] in ("remote-control", "login", "logout", "--version", "--help"):
        return None
    return "cli"


def detect_codex_apps():
    """List running Codex front-ends as dicts {pid, kind, cmd}."""
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        return []
    skip = _own_lineage()
    uid = os.getuid()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in skip:
            continue
        pid = int(name)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        kind = _classify(argv)
        if kind:
            found.append({"pid": pid, "kind": kind, "cmd": " ".join(argv)[:120]})
    return found


def _unit_active(unit):
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=10).returncode == 0
    except Exception:
        return False


def _unit_exists(unit):
    try:
        return subprocess.run(["systemctl", "--user", "cat", unit], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def stop_codex_apps(apps, timeout_sec=15.0):
    """
    Stop the given apps. The remote daemon is stopped through its systemd unit when that unit is active.
    Returns the list of systemd units that were active and should be started again after the switch.
    """
    units = []
    # A running daemon is brought back after the switch through its unit, even when the unit itself is
    # inactive/failed (e.g. its start reported a transient 409 while the daemon kept running)
    if any(a["kind"] == "remote-daemon" for a in apps) and _unit_exists(REMOTE_UNIT):
        units.append(REMOTE_UNIT)
        if _unit_active(REMOTE_UNIT):
            try:
                subprocess.run(["systemctl", "--user", "stop", REMOTE_UNIT], capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                print(f"[-] WARNING: stopping {REMOTE_UNIT} timed out; terminating its processes directly.")

    pending = [a["pid"] for a in apps]
    for pid in pending:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        pending = [p for p in pending if os.path.exists(f"/proc/{p}")]
        if not pending:
            break
        time.sleep(0.5)
    for pid in pending:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return units


def ensure_codex_apps_stopped(stop_apps=False):
    """
    Refuse to continue while Codex apps are running unless they are stopped.
    stop_apps=True stops them without asking; otherwise ask on a TTY and reject when non-interactive.
    Returns (ok, units_to_restart).
    """
    apps = detect_codex_apps()
    if not apps:
        return True, []

    labels = {"desktop": "Codex desktop app", "remote-daemon": "Remote-control daemon", "cli": "Codex CLI session"}
    print("\n" + "=" * 76)
    print(" [!] Codex is running. It keeps the old provider/config in memory and must be stopped:")
    for a in apps:
        print(f"     - {labels[a['kind']]} (pid {a['pid']}): {a['cmd']}")
    print("=" * 76)

    if not stop_apps:
        if not sys.stdin.isatty():
            print("[!] REJECTED: close them first, or re-run with --stop-apps to stop them automatically.\n")
            return False, []
        try:
            answer = input("Stop them now and continue? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("[!] Switch cancelled.\n")
            return False, []

    units = stop_codex_apps(apps)
    # A remote daemon may come straight back (e.g. a launcher restarts codex-rc once the desktop closes);
    # that is fine because remote units are restarted after the switch and pick up the new config.
    left = [a for a in detect_codex_apps() if a["kind"] != "remote-daemon"]
    if left:
        print(f"[!] REJECTED: could not stop pid(s) {', '.join(str(a['pid']) for a in left)}.\n")
        return False, units
    print("[+] Codex apps stopped.")
    return True, units
