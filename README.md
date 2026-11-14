# Reincarnate — herdr plugin

Hand a pointer to this session's logs to a new Claude Code session. Useful when context is overfilled.

> You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off. {takeover}

`{takeover}` gives the new session permission to kill the old one and lists the old session's background shells for it to take over. Then the old session is killed with SIGKILL, which leaves those shells running.

## Requirements

- herdr `>= 0.9.3` with the Claude Code integration installed
  (`herdr integration install claude`)
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` (Python 3.11+, stdlib only)

## Install

```bash
herdr plugin install malone-c/reincarnate
```
