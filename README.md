# Reincarnate — herdr plugin

Hand a pointer to this session's logs to a new Claude Code session. Useful when context is overfilled.

> You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off.{goal_note} {takeover}

`{goal_note}` names the old session's `/goal`, if one is still in force. Once the new session is working, reincarnate types the same `/goal` into it; if that fails, a herdr notification says so.

`{takeover}` gives the new session permission to kill the old one and lists the old session's background shells for it to take over. Once the new session is working, the old one is killed with SIGKILL, which leaves those shells running. Anything it cannot confirm (the old session's process, a shell's output, the kill) is named in `{takeover}` and in a herdr notification, and an unconfirmed process is never killed. A new session held at a startup dialog leaves the old one running; once it runs, it stops the old one itself.

## What carries over

The new session starts the way the old one did:

- Its launch options, read from the old process, without `--resume`, `--continue`, `--fork-session`, `--print` and the other options that pick a session or run once, and without its first prompt.
- Its environment, so variables set on its command line (`ANTHROPIC_BASE_URL=… claude`) come along. Variables the new pane sets for itself, such as `HERDR_PANE_ID` and `PWD`, are left to the new pane.

Then what changed while it ran, read from its transcript and its record in `~/.claude/sessions`:

| Setting | Changed with | Passed as |
| --- | --- | --- |
| Permission mode | shift+tab | `--permission-mode` |
| Model | `/model` | `--model` |
| Effort | `/effort` | `--effort` |
| Fast mode | `/fast` | `--settings '{"fastMode": …}'`, merged into any `--settings` |
| Added directories | `/add-dir` | `--add-dir` |
| Remote Control | `/remote-control` | `--remote-control` |
| Name | `/rename` | `--name` |
| Goal | `/goal` | typed into the new session |

Monitors, scheduled wakeups, cron jobs and subagents stop with the old session; the prompt tells the new one to restart the ones it needs.

Each run appends what it read and carried, and every problem, to `~/.claude/handoffs/reincarnate.log`.

## Requirements

- herdr `>= 0.9.3` with the Claude Code integration installed
  (`herdr integration install claude`)
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` (Python 3.11+, stdlib only)

## Install

```bash
herdr plugin install malone-c/reincarnate
```
