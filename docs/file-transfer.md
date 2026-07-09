# File transfer (`put` / `get`)

## Stage-and-move for root-owned files

`put` runs as the SSH user, not root. To install a config file into `/etc/...`:

```
1. put("pi-living", "nginx.conf", "/tmp/nginx.conf.new")
2. session_run("pi-living/default", "sudo mv /tmp/nginx.conf.new /etc/nginx/nginx.conf")
```

Both halves are independently audited.

## Downloads confinement

`get` writes under the downloads dir (`~/.local/state/tai-mcp-ssh/downloads/<host>/`)
by default. A `local_path` that resolves outside that tree — after collapsing `..`
and symlinks — is rejected before any connection is opened, so a download (whose
bytes the remote host controls) cannot overwrite `~/.ssh/authorized_keys`, a crontab,
or similar. To land a file elsewhere deliberately, pass `allow_outside: true`; the
out-of-tree write is then permitted and audited as such.
