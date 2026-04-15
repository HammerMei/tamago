#!/usr/bin/env python3
"""Restart a CLI agent session via AppleScript (iTerm2/Terminal.app) or cmux.

Supports: iTerm2, macOS Terminal.app, cmux
Requires: macOS, Python 3

Usage: python3 restart-cli.py <CLI_PID> <restart_cmd> [follow_up_prompt]
From Bash tool context: python3 restart-cli.py $PPID "claude -c" "繼續之前的任務"
"""

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time

TERMINAL_ITERM2 = "iterm2"
TERMINAL_APP = "terminal"
TERMINAL_CMUX = "cmux"
TERMINAL_UNKNOWN = "unknown"

_CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"
_cmux_surface_ref = None


# ---------------------------------------------------------------------------
# Terminal detection
# ---------------------------------------------------------------------------

def get_cmux_surface():
    try:
        r = subprocess.run([_CMUX_BIN, "identify", "--json"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return json.loads(r.stdout).get("caller", {}).get("surface_ref")
    except Exception:
        pass
    return None


def detect_terminal():
    if get_cmux_surface() is not None:
        return TERMINAL_CMUX
    pid = os.getppid()
    while pid > 1:
        try:
            comm = subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="],
                                           text=True, stderr=subprocess.DEVNULL).strip()
            if "iTerm2" in comm:
                return TERMINAL_ITERM2
            if "Terminal" in comm:
                return TERMINAL_APP
            pid = int(subprocess.check_output(["ps", "-p", str(pid), "-o", "ppid="],
                                               text=True, stderr=subprocess.DEVNULL).strip())
        except (subprocess.CalledProcessError, ValueError):
            break
    return TERMINAL_UNKNOWN


# ---------------------------------------------------------------------------
# TTY / PGID helpers
# ---------------------------------------------------------------------------

def get_tty():
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    pid = os.getpid()
    while pid > 1:
        try:
            out = subprocess.check_output(["ps", "-p", str(pid), "-o", "tty=,ppid="],
                                          text=True, stderr=subprocess.DEVNULL).strip()
            parts = out.split()
            tty = parts[0] if parts else "??"
            if tty and tty != "??":
                return "/dev/" + tty
            pid = int(parts[1]) if len(parts) >= 2 else 1
        except (subprocess.CalledProcessError, ValueError):
            break
    return None


def get_foreground_pgid(tty_path):
    tty_name = os.path.basename(tty_path)
    try:
        r = subprocess.run(["ps", "-t", tty_name, "-o", "pgid=,stat="],
                           capture_output=True, text=True)
        for line in r.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and "+" in parts[1]:
                return int(parts[0])
    except (ValueError, OSError):
        pass
    return None


def wait_for_foreground_pgid(tty_path, expected_pgid=None, timeout=10,
                              interval=0.2, stable_reads=3):
    deadline = time.monotonic() + timeout
    last_pgid = None
    stable_count = 0
    while time.monotonic() < deadline:
        pgid = get_foreground_pgid(tty_path)
        if pgid is None or (expected_pgid is not None and pgid != expected_pgid):
            last_pgid = None
            stable_count = 0
        elif pgid == last_pgid:
            stable_count += 1
            if stable_count >= stable_reads:
                return pgid
        else:
            last_pgid = pgid
            stable_count = 1
        time.sleep(interval)
    return None


def wait_for_new_foreground(tty_path, shell_pgid, timeout=30, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pgid = get_foreground_pgid(tty_path)
        if pgid is not None and pgid != shell_pgid:
            time.sleep(0.3)
            return True
        time.sleep(interval)
    return False


def wait_for_pid_gone(pid, timeout=5, interval=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# cmux path: SIGTSTP approach
#
# Strategy:
#   1. Spawn helper WHILE claude is alive (helper stays in claude's process tree
#      so cmux auth still works from the helper).
#   2. Helper sends SIGTSTP to claude → claude suspends → shell regains foreground.
#   3. Helper injects a compound shell command via cmux send (still valid because
#      claude is alive/suspended, not dead).
#   4. Shell executes: kill suspended claude, schedule follow-up injector, run restart_cmd.
#   5. Follow-up injector is a shell-spawned background job → inherits shell's cmux
#      credentials → cmux send works perfectly.
# ---------------------------------------------------------------------------

def spawn_cmux_helper(cli_pid, surface_ref, tty_path, restart_cmd, follow_up):
    tty_name = os.path.basename(tty_path) if tty_path else ""
    cmux = _CMUX_BIN

    # --- Follow-up watcher script ---
    # Spawned by the shell → shell is its parent → valid cmux credentials.
    if follow_up:
        watcher_script = "\n".join([
            "#!/bin/bash",
            f'CMUX={shlex.quote(cmux)}',
            f'SURFACE={shlex.quote(surface_ref)}',
            f'TTY_NAME={shlex.quote(tty_name)}',
            f'FOLLOW_UP={shlex.quote(follow_up + chr(10))}',
            f'SHELL_PGID={shlex.quote("$1")}',  # passed as $1
            'LOG=/tmp/restart-cli-debug.log',
            'get_fg_pgid() { ps -t "$1" -o pgid=,stat= 2>/dev/null | awk \'$2 ~ /\\+/ { print $1; exit }\'; }',
            '# Wait for new CLI to take foreground (PGID changes from shell PGID)',
            'DEADLINE=$(( $(date +%s) + 30 ))',
            'while [ $(date +%s) -lt $DEADLINE ]; do',
            '  PGID=$(get_fg_pgid "$TTY_NAME")',
            '  [ -n "$PGID" ] && [ "$PGID" != "$1" ] && break',
            '  sleep 0.2',
            'done',
            'echo "[$(date +%H:%M:%S)] watcher: new cli pgid=$PGID" >> "$LOG"',
            '# Wait for that PGID to stabilize (CLI at prompt)',
            'PREV=""; STABLE=0',
            'DEADLINE=$(( $(date +%s) + 60 ))',
            'while [ $(date +%s) -lt $DEADLINE ]; do',
            '  PGID=$(get_fg_pgid "$TTY_NAME")',
            '  if [ "$PGID" = "$PREV" ]; then STABLE=$(( STABLE + 1 )); [ $STABLE -ge 10 ] && break',
            '  else PREV="$PGID"; STABLE=1; fi',
            '  sleep 0.3',
            'done',
            'echo "[$(date +%H:%M:%S)] watcher: stable pgid=$PGID, sending follow-up" >> "$LOG"',
            '"$CMUX" send --surface "$SURFACE" "$FOLLOW_UP"',
            'echo "[$(date +%H:%M:%S)] watcher: done rc=$?" >> "$LOG"',
        ])
        fd, watcher_path = tempfile.mkstemp(suffix=".sh", prefix="restart-cli-watcher-")
        os.write(fd, watcher_script.encode())
        os.close(fd)
        os.chmod(watcher_path, stat.S_IRWXU)
    else:
        watcher_path = None

    # --- Launcher script ---
    # Instead of injecting a long compound command directly into the shell,
    # write the real work into a short-lived launcher script. Then helper only
    # injects: /bin/bash <launcher_path> "$SHELL_PGID"
    if watcher_path:
        launcher_script = "\n".join([
            "#!/bin/bash",
            f"kill SIGTERM {cli_pid}",
            f'( {shlex.quote(watcher_path)} "$1" >/dev/null 2>&1 ) &',
            restart_cmd,
        ])
    else:
        launcher_script = "\n".join([
            "#!/bin/bash",
            f"kill SIGTERM {cli_pid}",
            restart_cmd,
        ])

    fd, launcher_path = tempfile.mkstemp(suffix=".sh", prefix="restart-cli-run-")
    os.write(fd, launcher_script.encode())
    os.close(fd)
    os.chmod(launcher_path, stat.S_IRWXU)

    # --- Main helper script ---
    # Spawned before killing claude so it stays in claude's process tree.
    helper_script = "\n".join([
        "#!/bin/bash",
        'LOG=/tmp/restart-cli-debug.log',
        'exec >> "$LOG" 2>&1',
        f'echo "[$(date +%H:%M:%S)] helper started (sigtstp approach)"',
        f'CLI_PID={cli_pid}',
        f'SURFACE={shlex.quote(surface_ref)}',
        f'TTY_NAME={shlex.quote(tty_name)}',
        f'CMUX={shlex.quote(cmux)}',
        'get_fg_pgid() { ps -t "$1" -o pgid=,stat= 2>/dev/null | awk \'$2 ~ /\\+/ { print $1; exit }\'; }',
        '',
        '# Get claude PGID before suspending',
        'CLI_PGID=$(ps -p $CLI_PID -o pgid= 2>/dev/null | tr -d " ")',
        'echo "[$(date +%H:%M:%S)] cli pgid=$CLI_PGID"',
        '',
        '# Suspend claude so shell regains foreground',
        'kill -TSTP $CLI_PID 2>/dev/null',
        'echo "[$(date +%H:%M:%S)] SIGTSTP sent"',
        '',
        '# Wait for shell to become foreground',
        'DEADLINE=$(( $(date +%s) + 10 ))',
        'while [ $(date +%s) -lt $DEADLINE ]; do',
        '  PGID=$(get_fg_pgid "$TTY_NAME")',
        '  [ -n "$PGID" ] && [ "$PGID" != "$CLI_PGID" ] && break',
        '  sleep 0.1',
        'done',
        'SHELL_PGID="$PGID"',
        'echo "[$(date +%H:%M:%S)] shell foreground pgid=$SHELL_PGID"',
        '',
        '# Inject only a short launcher command into shell via cmux',
        f'LAUNCHER={shlex.quote(launcher_path)}',
        '"$CMUX" send --surface "$SURFACE" "/bin/bash $LAUNCHER \\\"$SHELL_PGID\\\""',
        '"$CMUX" send-key --surface "$SURFACE" enter',
        'echo "[$(date +%H:%M:%S)] launcher injected rc=$?"',
        '',
        '# Helper done — shell takes it from here',
    ])

    fd, helper_path = tempfile.mkstemp(suffix=".sh", prefix="restart-cli-helper-")
    os.write(fd, helper_script.encode())
    os.close(fd)
    os.chmod(helper_path, stat.S_IRWXU)

    with open("/tmp/restart-cli-debug.log", "w") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] spawning cmux helper: {helper_path}\n")
        if watcher_path:
            f.write(f"[{time.strftime('%H:%M:%S')}] watcher script: {watcher_path}\n")
        f.write(f"[{time.strftime('%H:%M:%S')}] launcher script: {launcher_path}\n")

    subprocess.Popen(
        ["/bin/bash", helper_path],
        stdout=open("/tmp/restart-cli-debug.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=False,
        close_fds=True,
    )

    return helper_path


# ---------------------------------------------------------------------------
# AppleScript helpers (iTerm2 / Terminal.app)
# ---------------------------------------------------------------------------

def escape_applescript(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def run_applescript(script, capture_output=False):
    result = subprocess.run(["osascript"], input=script, text=True, capture_output=True)
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        return None if capture_output else False
    return result.stdout if capture_output else True


def send_to_terminal(terminal, text, tty_path, newline=True):
    escaped = escape_applescript(text)
    tty = escape_applescript(tty_path) if tty_path else None

    if terminal == TERMINAL_ITERM2:
        if tty:
            write_cmd = (
                f'                    tell s to write text "{escaped}"\n' if newline
                else f'                    tell s to write text "{escaped}" newline NO\n'
            )
            script = (
                'tell application "iTerm2"\n'
                "    repeat with w in windows\n"
                "        repeat with t in tabs of w\n"
                "            repeat with s in sessions of t\n"
                f'                if tty of s is "{tty}" then\n'
                + write_cmd
                + "                    return\n"
                + "                end if\n"
                + "            end repeat\n"
                + "        end repeat\n"
                + "    end repeat\n"
                + "end tell"
            )
            return run_applescript(script)
        else:
            nl_suffix = "" if newline else " newline NO"
            return run_applescript(
                f'tell application "iTerm2" to tell current session '
                f'of current window to write text "{escaped}"{nl_suffix}'
            )

    elif terminal == TERMINAL_APP:
        if tty:
            return run_applescript(
                'tell application "Terminal"\n'
                "    repeat with w in windows\n"
                "        repeat with t in tabs of w\n"
                f'            if tty of t is "{tty}" then\n'
                f'                do script "{escaped}" in t\n'
                "                return\n"
                "            end if\n"
                "        end repeat\n"
                "    end repeat\n"
                "end tell"
            )
        else:
            return run_applescript(
                f'tell application "Terminal" to do script "{escaped}" in front window'
            )
    return False


def get_session_snapshot(terminal, tty_path):
    tty = escape_applescript(tty_path) if tty_path else None
    if not tty:
        return None
    if terminal == TERMINAL_ITERM2:
        script = (
            'tell application "iTerm2"\n'
            "    repeat with w in windows\n"
            "        repeat with t in tabs of w\n"
            "            repeat with s in sessions of t\n"
            f'                if tty of s is "{tty}" then\n'
            "                    return (is processing of s as string) & linefeed & (contents of s)\n"
            "                end if\n"
            "            end repeat\n"
            "        end repeat\n"
            "    end repeat\n"
            "end tell"
        )
    elif terminal == TERMINAL_APP:
        script = (
            'tell application "Terminal"\n'
            "    repeat with w in windows\n"
            "        repeat with t in tabs of w\n"
            f'            if tty of t is "{tty}" then\n'
            "                return (busy of t as string) & linefeed & (contents of t)\n"
            "            end if\n"
            "        end repeat\n"
            "    end repeat\n"
            "end tell"
        )
    else:
        return None
    payload = run_applescript(script, capture_output=True)
    if not payload:
        return None
    first_line, _, rest = payload.partition("\n")
    return {"active": first_line.strip().lower() == "true", "contents": rest}


def normalize_whitespace(text):
    return " ".join(text.split())


def prompt_probe(text, max_chars=24):
    return normalize_whitespace(text)[:max_chars]


def wait_for_session_quiet(terminal, tty_path, timeout=30, interval=0.2, stable_reads=2):
    deadline = time.monotonic() + timeout
    last_contents = None
    stable_count = 0
    saw_activity = False
    while time.monotonic() < deadline:
        snapshot = get_session_snapshot(terminal, tty_path)
        if snapshot is None:
            stable_count = 0
            last_contents = None
            time.sleep(interval)
            continue
        contents = snapshot["contents"]
        active = snapshot["active"]
        if active:
            saw_activity = True
        if contents == last_contents and (saw_activity or not active):
            stable_count += 1
            if stable_count >= stable_reads:
                return True
        else:
            last_contents = contents
            stable_count = 1
        time.sleep(interval)
    return False


def wait_for_prompt_visible(terminal, tty_path, prompt_text, timeout=15,
                             interval=0.2, stable_reads=2):
    probe = prompt_probe(prompt_text)
    if not probe:
        return True
    deadline = time.monotonic() + timeout
    stable_count = 0
    while time.monotonic() < deadline:
        snapshot = get_session_snapshot(terminal, tty_path)
        if snapshot and probe in normalize_whitespace(snapshot["contents"]):
            stable_count += 1
            if stable_count >= stable_reads:
                return True
        else:
            stable_count = 0
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: restart-cli.py <CLI_PID> <restart_cmd> [follow_up_prompt]",
              file=sys.stderr)
        sys.exit(1)

    try:
        cli_pid = int(sys.argv[1])
    except ValueError:
        print("ERROR: CLI_PID must be an integer.", file=sys.stderr)
        sys.exit(1)

    restart_cmd = sys.argv[2]
    follow_up = sys.argv[3] if len(sys.argv) > 3 else None

    terminal = detect_terminal()
    if terminal == TERMINAL_UNKNOWN:
        print("ERROR: Unsupported terminal. Supports iTerm2, Terminal.app, and cmux.",
              file=sys.stderr)
        sys.exit(1)

    global _cmux_surface_ref
    if terminal == TERMINAL_CMUX:
        _cmux_surface_ref = get_cmux_surface()
        if not _cmux_surface_ref:
            print("ERROR: Could not identify cmux caller surface.", file=sys.stderr)
            sys.exit(1)

    tty_path = get_tty()

    # -----------------------------------------------------------------------
    # cmux path: SIGTSTP-based injection
    # Spawn helper BEFORE doing anything to claude — while helper is still
    # in claude's process tree, cmux auth works.
    # Helper suspends claude, waits for shell, injects compound command.
    # -----------------------------------------------------------------------
    if terminal == TERMINAL_CMUX:
        spawn_cmux_helper(cli_pid, _cmux_surface_ref, tty_path, restart_cmd, follow_up)
        # Give helper time to send SIGTSTP before we exit
        time.sleep(1.0)
        sys.exit(0)

    # -----------------------------------------------------------------------
    # iTerm2 / Terminal.app path (unchanged)
    # -----------------------------------------------------------------------
    if os.fork() > 0:
        sys.exit(0)

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        os.kill(cli_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    if not wait_for_pid_gone(cli_pid):
        try:
            os.kill(cli_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wait_for_pid_gone(cli_pid, timeout=5)

    shell_pgid = wait_for_foreground_pgid(tty_path, timeout=10) if tty_path else None

    if not send_to_terminal(terminal, restart_cmd, tty_path):
        print("ERROR: Failed to send restart command.", file=sys.stderr)
        sys.exit(1)

    if follow_up:
        delivered = False
        if shell_pgid:
            delivered = wait_for_new_foreground(tty_path, shell_pgid, timeout=30)
            if delivered:
                if wait_for_session_quiet(terminal, tty_path, timeout=30):
                    if terminal == TERMINAL_ITERM2:
                        delivered = send_to_terminal(terminal, follow_up, tty_path, newline=False)
                        if delivered:
                            delivered = wait_for_prompt_visible(terminal, tty_path, follow_up)
                        if delivered:
                            delivered = send_to_terminal(terminal, "", tty_path)
                    else:
                        delivered = send_to_terminal(terminal, follow_up, tty_path)
                else:
                    delivered = False
        if not delivered:
            time.sleep(2)
            delivered = send_to_terminal(terminal, follow_up, tty_path)
            if not delivered:
                print("ERROR: Failed to send follow-up prompt.", file=sys.stderr)
            else:
                print("WARN: used fallback follow-up injection.", file=sys.stderr)


if __name__ == "__main__":
    main()
