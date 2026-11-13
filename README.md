# Reincarnate — herdr plugin

Hand a pointer to this session's logs to a new Claude Code session. Useful when context is overfilled.

> You are continuing Claude Code session {session_id}.{transcript_note} Pick up where it left off.

## Requirements

- herdr `>= 0.9.3` with the Claude Code integration installed
  (`herdr integration install claude`)
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` (Python 3.11+, stdlib only)

## Install

```bash
herdr plugin install malone-c/reincarnate
```
