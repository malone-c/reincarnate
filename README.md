# Handoff — herdr plugin

One action, `handoff`, that hands the task in the focused pane's Claude Code
session to a fresh session in a new split to the right. The new pane runs
`claude` from the same directory with a first prompt naming the old session's
ID and transcript, telling it to pick up where that session left off.

Use it when a session's context is getting full and you want a clean one to
carry on, without compacting. The old session is left as it is.

herdr's Claude Code integration already records each pane's session ID, so no
extra hooks are needed. On a pane with no Claude Code session it shows a toast
and does nothing.

## Requirements

- herdr `>= 0.9.3` with the Claude Code integration installed
  (`herdr integration install claude`)
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` (Python 3.11+, stdlib only)

## Install

```bash
herdr plugin install malone-c/herdr-handoff
```

Or link a clone if you want to hack on it:

```bash
herdr plugin link /path/to/herdr-handoff
```

Bind it in `config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "chrismalone.handoff.handoff"
description = "hand off claude session"
```

The command is typed into the new pane's shell, so a `claude` alias with your
usual flags still applies.
