# MCP tool reference

The server speaks MCP over stdio and has no REST API. It exposes 8 tools. Each tool returns a single JSON-encoded text block. The authoritative schemas are defined in `tool_specs()` in [`src/tai_mcp_ssh/server.py`](../src/tai_mcp_ssh/server.py). The behavioural contract is in [`openspec/specs/`](../openspec/specs/).

| Tool | Arguments (required in **bold**) | Returns |
|---|---|---|
| `hosts` | — | A list of `{alias, host, user, port, auth}` entries. Re-reads `hosts.toml` on every call. |
| `session_list` | — | A list of `{session_id, host, name, created_at, last_used_at, busy}` entries. |
| `session_run` | **`session_id`**, **`command`**, `reason`, `timeout` (default 30 s) | A run result (see below). |
| `session_wait` | **`session_id`**, `timeout` (default 30 s) | A run result. Use it to keep polling a command that is still running or is waiting for input. |
| `session_kill` | **`session_id`** | `{killed}`. Tears down the remote tmux session. |
| `session_reset` | **`session_id`** | `{reset}`. Stops tracking a stuck command without killing the pane, so the working directory, environment, and child processes survive. |
| `put` | **`host`**, **`local_path`**, **`remote_path`**, `reason` | `{bytes, sha256, local_path, remote_path}` |
| `get` | **`host`**, **`remote_path`**, `local_path`, `reason`, `allow_outside` | Same shape as `put`. See [file-transfer.md](./file-transfer.md). |

## Sessions

A `session_id` has the form `<host>/<name>`, for example `pi-living/default`. The `name` part must match `[A-Za-z0-9_-]+`. The first `session_run` on a session creates a tmux session called `tai/<name>` on the remote host. If that tmux session survived a server restart, it is reused. Shell state (working directory, exported variables, activated virtualenvs, functions) persists between calls.

Sessions stay open until they are killed. **The model using the server is responsible for closing sessions:** reuse `<host>/default` for routine work, and call `session_kill` on any other session once its task is finished. The server sends this guidance to the client as MCP server `instructions` when the client connects, and repeats it briefly in the `session_run` and `session_kill` descriptions.

As a backstop, the server cleans up sessions on its own. It kills a `tai/*` session that has been idle for 48 hours (configurable), as long as it is sitting at a shell prompt, has no attached client, and has no background jobs. A running command, a pending sudo prompt, or an attached human keeps a session alive. If a session was cleaned up (or killed by hand), the next `session_run` on it recreates it transparently, but its previous shell state is gone. See [configuration.md](./configuration.md#session-cleanup).

## Run result

```json
{
  "session_id": "pi-living/default",
  "status": "done",
  "exit": 0,
  "head": "...", "tail": "...",
  "bytes": 18234, "truncated": true,
  "log_id": "01J...", "log_path": "/home/pi/.tai-ssh/logs/01J....log",
  "attach_hint": null, "prompt": null
}
```

| `status` | Meaning |
|---|---|
| `done` | The command finished. `exit` holds its exit code. |
| `still_running` | `timeout` elapsed before the command finished. Call `session_wait` to keep polling. |
| `needs_password` / `needs_input` | A sudo, password, host-key, or `[Y/n]` prompt is waiting. Ask the user to run `attach_hint`, answer the prompt, and detach, then call `session_wait`. See [sudo-handoff.md](./sudo-handoff.md). |
| `busy` | Another command is already running in this session. Wait for it, reset the session, or kill it. |

If the output is at most 50 lines and 4 KB, the whole output is returned in `head`. Otherwise `head` and `tail` each contain up to 50 lines or 2 KB, and `truncated` is `true`. The complete output stays in `log_path` on the remote host, and the LLM can read more of it with `tail`, `grep`, or `sed`.
