# The sudo handoff

When the LLM runs a command that needs a password, the MCP detects the prompt within milliseconds and returns something like:

```json
{
  "status": "needs_password",
  "attach_hint": "ssh pi-living -t tmux attach -t tai/default",
  "prompt": "[sudo] password for pi:"
}
```

The LLM will tell you what to do. The flow:

1. Run the `attach_hint` command in your own terminal.
2. Type your sudo password into the live `tmux` pane.
3. Detach with `Ctrl-B` then `D`.
4. Tell the LLM to continue. It will call `session_wait` and pick up where it left off.

**Tip**: for routine LLM admin work, configuring `NOPASSWD` `sudoers` entries for the specific commands you trust (e.g. `systemctl restart nginx`, `apt update`) sidesteps the handoff entirely and is more auditable.

## Recommended: NOPASSWD sudoers for routine ops

The sudo-handoff flow exists for ad-hoc commands, but for the small set of operations the LLM repeats often you can sidestep it entirely by granting `NOPASSWD` for those exact commands. Drop a file at `/etc/sudoers.d/tai-mcp` on each managed host (edit with `sudo visudo -f /etc/sudoers.d/tai-mcp`):

```sudoers
# Allow the SSH user to run a curated set of admin commands without a password.
# Replace `pi` with your SSH user; tighten the list to what you actually trust.
pi ALL=(root) NOPASSWD: /usr/bin/apt update
pi ALL=(root) NOPASSWD: /usr/bin/apt-get update
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx
pi ALL=(root) NOPASSWD: /usr/bin/systemctl status *
pi ALL=(root) NOPASSWD: /usr/bin/journalctl *
pi ALL=(root) NOPASSWD: /usr/bin/docker ps, /usr/bin/docker logs *
```

Two reasons to do this:

- **Auditability**: the allowed commands are explicitly enumerated in `/etc/sudoers.d/tai-mcp`, so you and any human auditor can read off exactly what elevated power the LLM has.
- **Smoothness**: those commands no longer trigger the `needs_password` handoff. The LLM just runs them.

For everything else, the `tmux attach` handoff still works.
