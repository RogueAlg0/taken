# Contributing to taken

Thanks for stopping by. taken is a small tool with a big job: tell a
contributor whether a GitHub issue is actually up for grabs before they invest
an evening in it. Contributions that keep it honest, fast, and easy to audit
are welcome.

## Ways to contribute

- **Code**: bug fixes and small features. One concern per PR keeps reviews fast.
- **Docs**: the README, this file, and the docs site. If behavior changes, the
  docs change in the same PR.
- **Bug reports and ideas**: open an issue with the template. A good report
  says what you ran, what you expected, and what happened instead.

## Getting started

1. Fork the repo and clone your fork.
2. `uv sync` to set up the environment (Python 3.10+).
3. Install the [GitHub CLI](https://cli.github.com/) and run `gh auth login`.
   taken shells out to `gh api` with your own credentials, so the tool cannot
   do anything you could not do yourself.
4. `uv run taken --help` to see it working.
5. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and the
   invariants to keep intact.

## Workflow

- Create a branch from `main` with a short descriptive name
  (`fix/123-short-desc`).
- Make the change, add or update tests in `tests/`, and run the checks below.
- Open a PR against `main` from your fork. Fill in the PR template: what
  changed, why, and the checklist.
- CI runs the test suite on Python 3.10 through 3.13, plus ruff, mypy, and a
  smoke test. Everything goes green before merge.

## Before you push

Run these three commands. CI runs the same ones:

- `uv run ruff check`
- `uv run ruff format --check`
- `uv run pytest`
- `uv run --frozen mypy taken/`

## Changelog fragments

Do not edit `CHANGELOG.md` to describe a change. Add one small file to
`changes/` instead. Name the file `<issue>.<type>.md`, for example
`123.fixed.md`. Use the issue the change closes, or the pull request
number when no issue exists. The type is one of `added`, `changed`,
`deprecated`, `removed`, `fixed`, or `security`, matching the sections
in `CHANGELOG.md`.

Write the entry text only, in the style of the existing entries. Do not
add the `(#123)` reference; the release build appends the reference from
the file name. See `changes/README.md` for the full rules, including a
second fragment for one issue and a change with no issue number.

## Two gotchas

- **Vendored docs copies.** `docs/console/py/checks.py`, `docs/console/py/verdict.py`,
  `docs/console/py/budget.py` and `docs/console/py/transport.py` are byte-copies
  of `taken/checks.py`, `taken/verdict.py`, `taken/budget.py` and
  `taken/transport.py` for the in-browser docs console. If you touch any of
  those files, re-copy them into `docs/console/py/`:
  `cp taken/checks.py taken/verdict.py taken/budget.py taken/transport.py docs/console/py/`.
  CI checks the copies are in sync and fails the PR otherwise. The CLI
  reference `docs/takendocs.md` is generated the same way: after changing a
  flag or its help text, run `uv run python scripts/gen_cli_reference.py` and
  commit the result. CI regenerates the page and fails the PR when it differs.
- **The tool is read-only by design.** It must never write anything to the
  GitHub API: no comments, no labels, no state changes, only GET requests.
  Keep it that way.

## Style

- Keep runtime dependencies minimal. `httpx` (HTTP transport), `tqdm` (progress
  bar), and `mcp` (the MCP server SDK) are the only three; anything new must
  justify its weight. Dev tools (ruff, pytest) live in the uv dev group.
- Small functions, plain names, no cleverness. The verdict logic in
  `taken/verdict.py` should stay easy to audit.
- If you change verdict behavior, update the tests in `tests/` and the "How
  the verdict works" section of the README.
