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
from pathlib import Path

herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"


def run_herdr(*args):
    """Run a herdr CLI command and return its result, or None if it failed."""
    completed = subprocess.run([herdr, *args], capture_output=True, text=True)
    if completed.returncode != 0:
        return None
    return json.loads(completed.stdout).get("result")


def toast(title):
    subprocess.run([herdr, "notification", "show", title, "--sound", "none"], capture_output=True)


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

# Claude Code records each live session's process in ~/.claude/sessions/<pid>.json
recorded_pids = set()
for record_path in Path("~/.claude/sessions").expanduser().glob("*.json"):
    try:
        if json.loads(record_path.read_text()).get("sessionId") == session_id:
            recorded_pids.add(record_path.stem)
    except (OSError, ValueError):
        continue
ps_lines = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout.splitlines()
processes = [fields for fields in (line.split(None, 2) for line in ps_lines) if len(fields) == 3]
session_pid = next((pid for pid, _, command in processes if pid in recorded_pids and "claude" in command), None)

# Background shells are the session's children writing to a tasks/*.output file
shells = []
for pid, ppid, command in processes:
    if ppid != session_pid:
        continue
    if sys.platform == "linux":
        try:
            output = os.readlink(f"/proc/{pid}/fd/1")
        except OSError:
            output = ""
    else:
        lsof = subprocess.run(["lsof", "-a", "-p", pid, "-d", "1", "-Fn"], capture_output=True, text=True).stdout
        output = next((line[1:] for line in lsof.splitlines() if line.startswith("n")), "")
    if re.search(r"/tasks/[^/]+\.output$", output):
        # The Bash tool runs each command as `eval '<command>' < /dev/null`
        eval_match = re.search(r"eval '(.*)' < /dev/null", command)
        shown_command = (eval_match.group(1).replace("'\"'\"'", "'") if eval_match else command)[:300]
        shells.append(f"PID {pid}, output {output}: {shown_command}")

# One line, since it is typed into a shell
if session_pid is None:
    takeover = "The user authorises you to kill that session, and any process it started, should you find it still running."
else:
    takeover = (
        f"The user authorises you to kill that session (PID {session_pid}) and any process it started. "
        f"It should already be gone; if `ps -p {session_pid}` still lists it, run `kill -9 {session_pid}` before anything else. "
    )
    if shells:
        takeover += (
            "Its background shells kept running and are yours now: watch each by its PID and output file, "
            "and stop or restart it as the task needs. To hear when one exits, run "
            "`while kill -0 <PID> 2>/dev/null; do sleep 30; done` in the background. The shells: " + "; ".join(shells)
        )
    else:
        takeover += "It left no background shells running."
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
    except ProcessLookupError:
        pass
