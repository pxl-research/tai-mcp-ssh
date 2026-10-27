## Context

The audit log answers "what did the LLM do on this host?". Calls that read or leave local state unchanged add noise without answering that question.

## Decisions

- **Narrow the spec instead of adding records.** The code already behaves this way, and this is the scope the operator wants.
- **Rejections and errors are still always audited,** including on otherwise exempt tools. `_dispatch_and_audit` already does this for every tool.
- **`hosts()` stays covered** by its own requirement, "Host-inventory reloads are audited" (`_hosts_reload`).
- **RENAMED + MODIFIED.** The old header promised every invocation, so the header is renamed and the requirement keeps its scenarios. One scenario is added for the exemptions.
