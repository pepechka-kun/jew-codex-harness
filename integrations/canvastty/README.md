# Legacy CanvasTTY bridge adapter

These Python helpers implement the older `/g2/api/*` HTTP bridge and its own
terminal bindings. They are retained for compatibility with that bridge and are
not the integration path for current CanvasTTY sessions.

Inside current CanvasTTY use its `canvastty_agents` MCP tools. CanvasTTY owns
permissions, profiles, task delivery and terminal lifecycle; the harness router
selects model and reasoning depth. Do not silently fall back to this adapter if
a native MCP operation is denied or unavailable.

Standalone `jev-workers` can still reference these helpers when explicitly
configured with the legacy bridge. No legacy bridge is installed by the current
cross-platform installer.


### Codex lifecycle transport

The installer connects the Jev lifecycle handlers through Codex's supported
`mcp_tool` hooks on the required `jev-workers` stdio server. Its `env_vars`
allowlist forwards CanvasTTY runtime identity only to that server; the generated
shell policy excludes `CANVASTTY_RUNTIME_CAPABILITY` from ordinary commands and
preserves existing inheritance/exclusion choices. No capability value is stored
in TOML, hook definitions, arguments or diagnostic output.

CanvasTTY sessions and bounded workers expose only the internal lifecycle callback
from `jev-workers`, not its standalone worker creation tools. Agent calls to that
callback are denied; Codex lifecycle invocations do not recurse through PreToolUse.
Bounded workers retain the callback when other MCP tools are disabled.
The merge helpers upgrade only exact installer-owned legacy command/MCP entries;
foreign modifications still require a reviewed migration. Changed hook definitions
must be reviewed and trusted in Codex before use. MCP hook transport failures follow
Codex's lifecycle semantics; client and OS permission boundaries remain authoritative.

Local verification with an installed CLI and synthetic Responses only:
`python3 scripts/smoke_codex_hook_context.py --codex /absolute/path/to/codex`.
This covers filtered-context control, CanvasTTY ownership, standalone behavior and
absence of the capability in ordinary shell commands without using model accounts.
See [Codex MCP tool hooks](https://developers.openai.com/codex/hooks#mcp-tool-hooks).
