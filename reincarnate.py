#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Hand the focused pane's Claude Code task to a fresh session in a new split.

The new pane starts `claude` with a prompt naming the old session and its
transcript, so it can read where that session left off. Failures show as a
toast. Never exits nonzero: a keybind must not break the multiplexer.
"""

import glob
import json
import os
import shlex
import subprocess

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
prompt = f"You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off."

cwd = source_pane.get("foreground_cwd") or source_pane.get("cwd") or os.path.expanduser("~")
split = run_herdr("pane", "split", source_pane_id, "--direction", "right", "--cwd", cwd, "--focus")
if not split:
    toast("👻 reincarnate failed: could not split the pane")
    raise SystemExit(0)

# Typed into the new shell so the user's claude alias and PATH apply
subprocess.run([herdr, "pane", "run", split["pane"]["pane_id"], f"claude {shlex.quote(prompt)}"], capture_output=True)
