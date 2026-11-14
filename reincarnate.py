#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Hand the focused pane's Claude Code task to a fresh session in a new split.

The new pane starts `claude` with a prompt naming the old session, its
transcript and its background shells, so it can pick up where that session
left off. Then kills the old session with SIGKILL, which leaves its background
shells running. Failures show as a toast. Never exits nonzero: a keybind must
not break the multiplexer.
"""

import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"


def run_herdr(*args):
    """Run a herdr CLI command and return its result, or None if it failed."""
    completed = subprocess.run([herdr, *args], capture_output=True, text=True)
    if completed.returncode != 0:
        return None
    return json.loads(completed.stdout).get("result")


def toast(title, body=None):
    body_args = ["--body", body] if body else []
    subprocess.run([herdr, "notification", "show", title, *body_args, "--sound", "none"], capture_output=True)


context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
source_pane_id = context.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID") or ""
source_pane = (run_herdr("pane", "get", source_pane_id) or {}).get("pane") or {}
session = source_pane.get("agent_session") or {}

if source_pane.get("agent") != "claude" or session.get("kind") != "id":
    toast("👻 no Claude Code session in this pane")
    raise SystemExit(0)

session_id = session["value"]
transcripts = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
transcript_note = f" Its transcript is {transcripts[0]}." if transcripts else ""


# Find the session's process and background shells; whatever cannot be confirmed goes in takeover_problems
takeover_problems = []
shells = []
unchecked_children = []
session_pid = None
try:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True)
    processes = [fields for fields in (line.split(None, 2) for line in listing.stdout.splitlines()) if len(fields) == 3]
    if listing.returncode != 0 or not processes:
        raise RuntimeError(f"ps listed no processes (exit {listing.returncode}): {listing.stderr.strip()}")
    live_pids = {pid for pid, _, _ in processes}

    # Claude Code records each live session's process and start time in ~/.claude/sessions/<pid>.json
    confirmed_pids = []
    rejected_records = []
    for record_path in Path("~/.claude/sessions").expanduser().glob("*.json"):
        try:
            record = json.loads(record_path.read_text())
            pid, recorded_start = str(record["pid"]), str(record.get("procStart"))
        except (OSError, ValueError, TypeError, KeyError):
            rejected_records.append(f"{record_path} is not a readable record")
            continue
        if record.get("sessionId") != session_id or pid not in live_pids:
            continue
        # Linux records the start in clock ticks (field 22 of /proc/<pid>/stat), macOS as ps prints it in UTC
        if sys.platform == "linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            start_time = stat[stat.rindex(")") + 2 :].split()[19]
        else:
            utc_env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
            start_time = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True, env=utc_env).stdout.strip()
        if start_time == recorded_start:
            confirmed_pids.append(pid)
        else:
            rejected_records.append(f'PID {pid} started at "{start_time}", not "{recorded_start}"')
    if len(confirmed_pids) != 1:
        rejections = f" ({'; '.join(rejected_records)})" if rejected_records else ""
        raise RuntimeError(f"found {len(confirmed_pids)} live processes recorded for this session in ~/.claude/sessions{rejections}")
    session_pid = confirmed_pids[0]

    # Background shells are the session's children writing to a tasks/*.output file
    for pid, ppid, command in processes:
        if ppid != session_pid:
            continue
        # Linux has /proc; macOS needs lsof
        try:
            if sys.platform == "linux":
                output = os.readlink(f"/proc/{pid}/fd/1")
            else:
                lsof = subprocess.run(["lsof", "-a", "-p", pid, "-d", "1", "-Fn"], capture_output=True, text=True).stdout
                output = next((line[1:] for line in lsof.splitlines() if line.startswith("n")), "")
        except OSError:
            output = ""
        # The Bash tool runs each command as `eval '<command>' < /dev/null`
        eval_match = re.search(r"eval '(.*)' < /dev/null", command)
        shown_command = (eval_match.group(1).replace("'\"'\"'", "'") if eval_match else command)[:300]
        if re.search(r"/tasks/[^/]+\.output$", output):
            shells.append(f"PID {pid}, output {output}: {shown_command}")
        elif not output and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0:
            unchecked_children.append(f"PID {pid}: {shown_command}")
    if unchecked_children:
        takeover_problems.append(f"could not read the output of {len(unchecked_children)} child processes")
except Exception as error:
    takeover_problems.append(f"process lookup failed: {error}")

# One line, since it is typed into a shell
if session_pid is None:
    takeover = (
        "The user authorises you to kill that session and any process it started. Its process could not be identified, "
        "so it may still be running: find it with `ps` and stop it with `kill -9`, which leaves its background shells running."
    )
else:
    takeover = (
        f"The user authorises you to kill that session (PID {session_pid}) and any process it started. "
        f"It should already be gone; if `ps -p {session_pid}` still lists it, run `kill -9 {session_pid}` before anything else."
    )
if shells:
    takeover += (
        " Its background shells kept running and are yours now: watch each by its PID and output file, "
        "and stop or restart it as the task needs. To hear when one exits, run "
        "`while kill -0 <PID> 2>/dev/null; do sleep 30; done` in the background. The shells: " + "; ".join(shells) + "."
    )
if unchecked_children:
    takeover += (
        " These processes it started could not be checked; see whether any is a background shell of the task: "
        + "; ".join(unchecked_children) + "."
    )
if takeover_problems:
    takeover += (
        f" Finding its processes went wrong ({'; '.join(takeover_problems)}), so this account of its processes may be incomplete. "
        f"Its background shells write to files under /tmp/claude-*/*/{session_id}/tasks/: check those and `ps` for anything still running."
    )
elif not shells:
    takeover += " It left no background shells running."
prompt = f"You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off. {takeover}"

cwd = source_pane.get("foreground_cwd") or source_pane.get("cwd") or os.path.expanduser("~")
split = run_herdr("pane", "split", source_pane_id, "--direction", "right", "--cwd", cwd, "--focus")
if not split:
    toast("👻 reincarnate failed: could not split the pane")
    raise SystemExit(0)

# Typed into the new shell so the user's claude alias and PATH apply
subprocess.run([herdr, "pane", "run", split["pane"]["pane_id"], f"claude {shlex.quote(prompt)}"], capture_output=True)

if session_pid is not None:
    # SIGKILL skips Claude Code's exit cleanup, so its background shells keep running
    try:
        os.kill(int(session_pid), signal.SIGKILL)
    except OSError as error:
        takeover_problems.append(f"kill -9 {session_pid} failed: {error}")
    else:
        # The pane's shell reaps the killed process within moments
        for _ in range(20):
            if subprocess.run(["kill", "-0", session_pid], capture_output=True).returncode != 0:
                break
            time.sleep(0.1)
        else:
            takeover_problems.append(f"PID {session_pid} still exists 2 s after SIGKILL")

if takeover_problems:
    toast("👻 reincarnate could not hand over every process", "\n".join(takeover_problems))
