# Reincarnate — herdr plugin

Hand a pointer to this session's logs to a new Claude Code session. Useful when context is overfilled.

> You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off.{goal_note} {takeover}

`{goal_note}` names the old session's `/goal`, if one is still in force. Once the new session is working, reincarnate types the same `/goal` into it; if that fails, a herdr notification says so.

`{takeover}` gives the new session permission to kill the old one and lists the old session's background shells for it to take over. Then the old session is killed with SIGKILL, which leaves those shells running. Anything it cannot confirm (the old session's process, a shell's output, the kill) is named in `{takeover}` and in a herdr notification, and an unconfirmed process is never killed.

## Requirements

- herdr `>= 0.9.3` with the Claude Code integration installed
  (`herdr integration install claude`)
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` (Python 3.11+, stdlib only)

## Install

```bash
herdr plugin install malone-c/reincarnate
```
