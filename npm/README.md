# taken-gh

The npm installer for [taken](https://github.com/RogueAlg0/taken), the small CLI that answers "taken?" for any GitHub issue before you volunteer for it.

## What this installs

Installing this package runs a postinstall step that makes sure the real `taken` Python CLI is on your PATH:

- If `taken --version` already works, nothing happens.
- Otherwise it runs `python3 -m pip install --user "taken-gh==<this version>"`.

The postinstall never fails your npm install: if Python or pip is missing, or the pip install fails, it prints the manual install command and moves on.

## Binaries

- `taken` — check whether a GitHub issue is already taken: `taken check owner/repo#123`
- `taken-mcp` — the MCP server exposing the same checks to AI assistants

Both are thin shims: they find the real Python-installed binary on your PATH and forward arguments, stdin/stdout/stderr, signals, and the exit code.

## Versioning

This package's version always matches the `taken-gh` PyPI release of the same number. The postinstall installs exactly that version, so the two can never drift.
