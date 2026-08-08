# General tools

Otto exposes a small permanent execution surface and keeps domain workflows in
Skills. A Skill can describe how to inspect a project, transform a document, or
run a build without adding a new function schema for every workflow.

## Tool contracts

| Tool | Intended use | Runtime boundary |
|---|---|---|
| `read_file` | Read exact text with offset pagination | Existing file inside `OTTO_WORKSPACE` |
| `list_files` | Discover paths by directory, glob, and recursion | Inside the workspace; hidden paths are omitted by default |
| `search_files` | Literal or regex source search with line numbers | UTF-8 files up to 2 MB; skips state/vendor directories and symlinks |
| `write_file` | Create a file or explicitly overwrite it | Atomic workspace write; human approval required |
| `apply_patch` | Replace an exact block in an existing file | Atomic workspace write; ambiguous matches fail; human approval required |
| `run_command` | Build, test, or invoke a CLI | Host shell in a workspace directory; human approval required |
| `python` | Calculation and bounded code interpretation | OS sandbox; no network; workspace is read-only; writes go to a run directory |
| `fetch_url` | Read a known public URL | GET only, bounded response, public HTTP(S) destinations only |

All path resolution uses canonical paths. Absolute paths, `..`, and symlinks
cannot be used to escape `OTTO_WORKSPACE` through the file tools.

## Terminal versus Python

`run_command` is intentionally an approved **host terminal**, not a sandbox.
Otto removes inherited credentials and most environment variables, uses a
bounded timeout, captures output, and applies the central permission policy.
The policy hard-denies common privilege escalation, shutdown, disk formatting,
and direct disk overwrite patterns. Approval is still the actual trust boundary:
a shell parser cannot prove arbitrary commands safe.

`python` is the code interpreter. On macOS it uses `sandbox-exec`; on Linux it
uses Bubblewrap (`bwrap`). It receives a minimal environment, cannot access the
network, can read but not modify the workspace, and can write only under:

```text
OTTO_HOME/python-runs/<run-id>/
```

The result lists artifacts created there. If the platform has no supported OS
sandbox, the tool returns an error instead of silently executing on the host.
The interpreter starts with `-I -S`, so it is isolated from user Python settings
and exposes the standard library rather than Otto's installed packages.

## Fetch safety

`fetch_url` accepts only credential-free `http://` and `https://` URLs. It
resolves and rejects loopback, private, link-local, multicast, reserved, and
unspecified addresses, applies the same validation to redirects and the final
URL, and caps both time and bytes. Use `search_web` to discover a URL, then
`fetch_url` when its page text is needed.

## Skill pattern

A Skill should describe the domain workflow and call these primitives only when
needed. For example, a release-check Skill can say:

1. Use `read_file` for the package configuration.
2. Use `search_files` to locate existing release checks.
3. Use `run_command` for the project's test command after approval.
4. Use `apply_patch` for a precise edit and rerun the focused test.

This keeps the tool schemas stable for prompt caching while the Skill remains
easy to update as the workflow changes.
