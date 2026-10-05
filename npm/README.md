[![npm](https://img.shields.io/npm/v/taken-gh?cacheSeconds=3600)](https://www.npmjs.com/package/taken-gh)

# taken-gh

The npm installer for [taken](https://github.com/RogueAlg0/taken), the small CLI that answers "taken?" for any GitHub issue before you volunteer for it.

## What this installs

Installing this package runs a postinstall step that makes sure the real `taken` Python CLI is on your PATH:

- If `taken` is missing or older than this package's version, postinstall runs `python3 -m pip install --user "taken-gh==<this version>"`, retrying once with `--break-system-packages` on systems where pip refuses user installs (Debian 12+, Ubuntu 23.04+).
- If `taken --version` reports this package's version, postinstall skips the pip install.
- If `taken` is newer, postinstall prints a note and skips the pip install, leaving the newer version in place.

The postinstall never fails your npm install: if Python or pip is missing, or the pip install fails, it prints the manual install command and moves on.

## Binaries

- `taken` — check whether a GitHub issue is already taken: `taken check owner/repo#123`
- `taken-mcp` — the MCP server exposing the same checks to AI assistants

Both are thin shims: they find the real Python-installed binary on your PATH and forward arguments, stdin/stdout/stderr, signals, and the exit code.

## Versioning

This package's version always matches the `taken-gh` PyPI release of the same number. When postinstall runs pip, it installs that version; an already installed newer version is left in place.
