#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Hand the focused pane's Claude Code task to a fresh session in a new split.

The new session gets the old one's launch options, environment and changed
settings, and a prompt naming its transcript and background shells. Once the
new session is working, kills the old one with SIGKILL, which leaves its
background shells running. Writes the prompt and launch command to temp files
and logs to ~/.claude/handoffs/reincarnate.log. Problems show as a toast.
Never exits nonzero: a keybind must not break the multiplexer.
"""

import ctypes
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
log_path = Path("~/.claude/handoffs/reincarnate.log").expanduser()
session_id = "-"


def log(line):
    """Append a line to the reincarnate log and stderr."""
    stamped = f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {session_id} {line}"
    print(stamped, file=sys.stderr)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as log_file:
            log_file.write(stamped + "\n")
    except OSError as error:
        print(f"could not write {log_path}: {error}", file=sys.stderr)


def run_herdr(*args):
    """Run a herdr CLI command and return its result, or None if it failed."""
    completed = subprocess.run([herdr, *args], capture_output=True, text=True)
    if completed.returncode != 0:
        log(f"herdr {' '.join(args[:2])} failed: {(completed.stdout + completed.stderr).strip()}")
        return None
    return json.loads(completed.stdout).get("result")


def toast(title, body=None):
    log(f"{title}{f': {body}' if body else ''}")
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
log(f"reincarnating pane {source_pane_id}")
takeover_problems = []
transcripts = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
transcript_note = f" Its transcript is {transcripts[0]}." if transcripts else ""
if not transcripts:
    takeover_problems.append("found no transcript, so its goal and changed settings were not carried")

# one pass for the goal and the settings-change rows
goal = None
first_rows, last_rows = {}, {}
if transcripts:
    with open(transcripts[0], errors="replace") as transcript:
        for line in transcript:
            if '"type":"goal_status"' in line:
                # the live session may be mid-write on its last row
                try:
                    goal_status = json.loads(line)["attachment"]
                except (ValueError, KeyError):
                    continue
                goal = None if goal_status.get("met") or goal_status.get("failed") else goal_status.get("condition") or None
                continue
            # copies inside message text are escaped, so never match
            if line.startswith('{"type":"permission-mode"'):
                kind = "permission-mode"
            elif '"type":"environment"' in line and '"type":"attachment"' in line:
                kind = "environment"
            elif '"requestedModel":' in line and '"type":"assistant"' in line and '"isSidechain":false' in line:
                kind = "assistant"
            else:
                continue
            first_rows.setdefault(kind, line)
            last_rows[kind] = line
goal_note = (
    f' Its session goal, a /goal stop check, carries over to you: "{goal}". '
    "The check reads only this conversation, so before you stop, show here that the goal holds."
    if goal
    else ""
)

# settings changed since launch; the rest come with argv
changed = {}
for kind, read in (
    ("permission-mode", lambda row: {"permission_mode": row["permissionMode"]}),
    ("environment", lambda row: {"directories": row["attachment"]["snapshot"]["additionalWorkingDirectories"]}),
    ("assistant", lambda row: {"model": row["requestedModel"], "effort": row.get("effort"), "fast": (row["message"].get("usage") or {}).get("speed") == "fast"}),
):
    if kind not in last_rows:
        continue
    try:
        first, last = read(json.loads(first_rows[kind])), read(json.loads(last_rows[kind]))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        takeover_problems.append(f"could not read its {kind} rows ({error!r}), so that setting was not carried")
        continue
    changed |= {key: value for key, value in last.items() if value != first[key] or key == "directories"}

# Find the session's process and background shells; whatever cannot be confirmed goes in process_problems
process_problems = []
shells = []
unchecked_children = []
session_pid = None
session_record = {}
try:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True)
    processes = [fields for fields in (line.split(None, 2) for line in listing.stdout.splitlines()) if len(fields) == 3]
    if listing.returncode != 0 or not processes:
        raise RuntimeError(f"ps listed no processes (exit {listing.returncode}): {listing.stderr.strip()}")
    live_pids = {pid for pid, _, _ in processes}

    # Claude Code records each live session's process and start time in ~/.claude/sessions/<pid>.json
    confirmed_records = []
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
            confirmed_records.append(record)
        else:
            rejected_records.append(f'PID {pid} started at "{start_time}", not "{recorded_start}"')
    if len(confirmed_records) != 1:
        rejections = f" ({'; '.join(rejected_records)})" if rejected_records else ""
        raise RuntimeError(f"found {len(confirmed_records)} live processes recorded for this session in ~/.claude/sessions{rejections}")
    session_record = confirmed_records[0]
    session_pid = str(session_record["pid"])

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
        process_problems.append(f"could not read the output of {len(unchecked_children)} child processes")
except Exception as error:
    process_problems.append(f"process lookup failed: {error}")

# the old process's exact argv and environment
old_argv = old_env = None
if session_pid is not None:
    try:
        if sys.platform == "linux":
            old_argv = [arg.decode(errors="replace") for arg in Path(f"/proc/{session_pid}/cmdline").read_bytes().split(b"\0")[:-1]]
            entries = Path(f"/proc/{session_pid}/environ").read_bytes().split(b"\0")
        else:
            # KERN_PROCARGS2: argc, exec path, padding, argv, env, empty string
            libc = ctypes.CDLL(None, use_errno=True)
            argmax, size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
            if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
                raise OSError(ctypes.get_errno(), "sysctl KERN_ARGMAX failed")
            buffer, size = ctypes.create_string_buffer(argmax.value), ctypes.c_size_t(argmax.value)
            if libc.sysctl((ctypes.c_int * 3)(1, 49, int(session_pid)), 3, buffer, ctypes.byref(size), None, 0) != 0:
                raise OSError(ctypes.get_errno(), "sysctl KERN_PROCARGS2 failed")
            raw = buffer.raw[: size.value]
            argc = int.from_bytes(raw[:4], sys.byteorder)
            fields = raw[4:].split(b"\0", 1)[1].lstrip(b"\0").split(b"\0")
            old_argv = [arg.decode(errors="replace") for arg in fields[:argc]]
            entries = fields[argc : fields.index(b"", argc)]
        old_env = {key: value for key, _, value in (entry.decode(errors="replace").partition("=") for entry in entries if b"=" in entry)}
    except (OSError, ValueError, IndexError) as error:
        takeover_problems.append(f"could not read its launch options and environment ({error}), so they were not carried")
        old_argv = old_env = None

# the new pane sets these itself
per_pane = re.compile(r"HERDR_.*|PWD|OLDPWD|SHLVL|_|TERM_SESSION_ID|ITERM_SESSION_ID|TMUX_PANE|WINDOWID|KITTY_WINDOW_ID|WEZTERM_PANE|ZELLIJ_PANE_ID|SSH_TTY|GPG_TTY|TTY|COLUMNS|LINES")
carried_env = {key: value for key, value in (old_env or {}).items() if not per_pane.fullmatch(key) and os.environ.get(key) != value}
launch_path = carried_env.get("PATH") or os.environ.get("PATH")

# option arity, from the help of the claude that runs
arity = {}
claude_binary = shutil.which("claude", path=launch_path)
if old_argv is not None:
    try:
        help_text = subprocess.run([claude_binary or "claude", "--help"], capture_output=True, text=True, timeout=30).stdout
        for match in re.finditer(r"^  (?:(-\w), )?(--[\w-]+(?:, --[\w-]+)*)(?: ([<\[])[^>\]\s]*?(\.\.\.)?[>\]])?", help_text, re.MULTILINE):
            kind = "flag" if not match.group(3) else "variadic" if match.group(4) else "required" if match.group(3) == "<" else "optional"
            for name in [match.group(1), *match.group(2).split(", ")]:
                if name:
                    arity[name] = kind
        if not arity:
            raise ValueError("claude --help listed no options")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        takeover_problems.append(f"could not read claude --help ({error}), so its launch options were not carried")
        old_argv = None

# launch options minus session pickers, one-shot options and the prompt
one_shot = {"-r", "--resume", "-c", "--continue", "--fork-session", "--session-id", "--from-pr", "--teleport", "--cloud", "--environment", "--bg", "--background", "--desktop", "-p", "--print", "--file"}
chosen_name = session_record.get("name") if session_record.get("nameSource") not in (None, "derived") else None
replaced = ({"-n", "--name"} if chosen_name else set()) | ({"--permission-mode"} if "permission_mode" in changed else set())
replaced |= {flag for flag, key in (("--model", "model"), ("--effort", "effort")) if key in changed}
launch_options = []
unknown_options = []
args = (old_argv or [])[1:]
index = 0
while index < len(args):
    token = args[index]
    # past `--`, or a word no option takes, is the prompt
    if token == "--":
        break
    if not token.startswith("-") or token == "-":
        index += 1
        continue
    name, has_value, _ = token.partition("=")
    kind = arity.get(name)
    if kind is None:
        unknown_options.append(token)
    # mirrors how commander reads option values
    end = index + 1
    if not has_value and kind == "required":
        end = min(index + 2, len(args))
    elif not has_value and kind in ("optional", "variadic"):
        while end < len(args) and not args[end].startswith("-") and (kind == "variadic" or end == index + 1):
            end += 1
    if name not in one_shot and name not in replaced:
        launch_options += args[index:end]
    index = end
if unknown_options:
    takeover_problems.append(f"carried launch options that claude --help does not list, as flags: {' '.join(unknown_options)}")

# the dangerous flag overrides --permission-mode; keep it switchable
if "permission_mode" in changed and "--dangerously-skip-permissions" in launch_options:
    launch_options = ["--allow-dangerously-skip-permissions" if option == "--dangerously-skip-permissions" else option for option in launch_options]
setting_options = []
if "permission_mode" in changed:
    setting_options += ["--permission-mode", changed["permission_mode"]]
if "model" in changed:
    setting_options += ["--model", changed["model"]]
if changed.get("effort") in ("low", "medium", "high", "xhigh", "max"):
    setting_options += ["--effort", changed["effort"]]
elif changed.get("effort") is not None:
    takeover_problems.append(f"its effort {changed['effort']!r} is not a level --effort takes, so it was not carried")
for directory in changed.get("directories") or []:
    if directory not in launch_options:
        setting_options += ["--add-dir", directory]
if "fast" in changed:
    # fast mode is a setting; merge into any launch --settings
    settings_index = next((index for index, option in enumerate(launch_options) if option == "--settings"), None)
    try:
        if settings_index is None:
            setting_options += ["--settings", json.dumps({"fastMode": changed["fast"]})]
        else:
            given = launch_options[settings_index + 1]
            loaded = json.loads(given) if given.lstrip().startswith("{") else json.loads((Path(session_record.get("cwd") or ".") / given).read_text())
            launch_options[settings_index + 1] = json.dumps({**loaded, "fastMode": changed["fast"]})
    except (OSError, ValueError, IndexError) as error:
        takeover_problems.append(f"could not merge fast mode into its --settings ({error}), so fast mode was not carried")
if session_record.get("bridgeSessionId") and "--remote-control" not in launch_options:
    setting_options.append("--remote-control")
if chosen_name:
    setting_options += ["--name", chosen_name]
log(
    f"old argv {old_argv}; carried options {launch_options + setting_options}; "
    f"changed settings {changed}; carried env {sorted(carried_env)}"
)


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
if process_problems:
    takeover += (
        f" Finding its processes went wrong ({'; '.join(process_problems)}), so this account of its processes may be incomplete. "
        f"Its background shells write to files under /tmp/claude-*/*/{session_id}/tasks/: check those and `ps` for anything still running."
    )
elif not shells:
    takeover += " It left no background shells running."
takeover += " Its monitors, scheduled wakeups, cron jobs and subagents stopped with it: restart the ones the task still needs."
prompt = f"You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off.{goal_note} {takeover}"

cwd = source_pane.get("foreground_cwd") or source_pane.get("cwd") or os.path.expanduser("~")
split = run_herdr("pane", "split", source_pane_id, "--direction", "right", "--cwd", cwd, "--focus")
if not split:
    toast("👻 reincarnate failed: could not split the pane")
    raise SystemExit(0)

# macOS cuts typed lines at 1024 bytes, so use files
with tempfile.NamedTemporaryFile("w", prefix=f"reincarnate-{session_id}-", suffix=".txt", delete=False) as prompt_file:
    prompt_file.write(prompt)
prompt_argument = f'"$(cat {shlex.quote(prompt_file.name)})"'
with tempfile.NamedTemporaryFile("w", prefix=f"reincarnate-{session_id}-", suffix=".sh", delete=False) as launch_file:
    if old_argv is None:
        # sourced, so the user's claude alias and PATH apply
        launch_file.write(f"{shlex.join(['claude', *setting_options, '--'])} {prompt_argument}\n")
        launch_line = shlex.join([".", launch_file.name])
    else:
        # the old argv already holds what the alias added
        launch_file.write(f"exec {shlex.join(['env', *(f'{key}={value}' for key, value in carried_env.items()), 'claude', *launch_options, *setting_options, '--'])} {prompt_argument}\n")
        launch_line = shlex.join(["sh", launch_file.name])
new_pane_id = split["pane"]["pane_id"]
subprocess.run([herdr, "pane", "run", new_pane_id, launch_line], capture_output=True)
log(f"started the new session in pane {new_pane_id} with: {launch_line}")

# Keep the old session alive unless herdr sees Claude Code start in the new pane
for _ in range(60):
    if ((run_herdr("pane", "get", new_pane_id) or {}).get("pane") or {}).get("agent") == "claude":
        break
    time.sleep(0.5)
else:
    toast("👻 reincarnate failed: the new session did not start", f"Old session left running. Launch command: {launch_file.name}")
    raise SystemExit(0)

# a session held at a startup dialog may still exit
started = run_herdr("agent", "wait", new_pane_id, "--until", "working", "--until", "blocked", "--timeout", "30000")
started_agent = (started or {}).get("agent") or {}
if started_agent.get("agent_status") != "working":
    takeover_problems.append(
        f"the new session in pane {new_pane_id} {'waits at a dialog' if started_agent.get('agent_status') == 'blocked' else 'has not started working'}, "
        "so the old session was left running: once the new one runs, it stops the old one itself"
    )
elif session_pid is not None:
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

if goal:
    # sent mid-turn, so the takeover prompt runs first
    successor_id = (started_agent.get("agent_session") or {}).get("value")
    goal_typed = successor_id is not None and started_agent.get("agent_status") == "working"
    # Claude Code sends a folded paste of "/goal …" as a message
    for keystroke in (["send-text", new_pane_id, "/goal "], ["send-text", new_pane_id, goal], ["send-keys", new_pane_id, "enter"]):
        if not goal_typed:
            break
        # keys typed into a permission dialog would answer it
        status = ((run_herdr("pane", "get", new_pane_id) or {}).get("pane") or {}).get("agent_status")
        if status != "working":
            takeover_problems.append(f"stopped typing the goal because the new session went {status}" + ("" if keystroke[2] == "/goal " else "; clear its input box"))
            goal_typed = False
            break
        goal_typed = subprocess.run([herdr, "pane", *keystroke], capture_output=True).returncode == 0
        time.sleep(0.3)
    # a goal_status row shows the goal was set
    goal_set = False
    for _ in range(50 if goal_typed else 0):
        successor_transcripts = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{successor_id}.jsonl"))
        goal_set = any('"type":"goal_status"' in Path(path).read_text(errors="replace") for path in successor_transcripts)
        if goal_set:
            break
        time.sleep(0.2)
    if not goal_set:
        takeover_problems.append(f"could not set the goal on the new session, so run /goal there: {goal}")

if process_problems or takeover_problems:
    toast("👻 reincarnate could not hand over everything", "\n".join(process_problems + takeover_problems))
else:
    log("handed over everything")
