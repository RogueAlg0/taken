# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed
- `--discover` ranking: candidates with equal scores are now ordered
  most-recently-updated first, matching the documented "score desc, then
  recency desc" order (the old double sort left ties oldest-first).
- Single-issue output (`taken owner/repo#123`) now prints the
  first-time-friendly markers (`first-time friendly:`, `welcoming:`) when
  present, matching what `--discover` lines and the MCP tools already show.
- MCP `check_issue` now returns top-level `friendly_labels` and `welcoming`,
  consistent with `scan_repo` and `discover_candidates`.

### Added
- First-time-friendly recommendations: `taken --discover` lines and MCP
  results now carry `friendly_labels` (the issue's own
  first-time-contributor labels, e.g. `good first issue`) and `welcoming`
  (repo-level signs contributions are welcome: a CONTRIBUTING guide,
  recently merged PRs). These come from data the check suite already
  fetches, so they cost no extra API calls. CLI repo scans annotate the GO
  recommendations with friendly labels and list the friendliest first.

## [0.6.0] - 2026-09-26

### Fixed
- Timeline and comment scans now page through up to 5 pages (500 items)
  instead of trusting the first 100 results: a linked PR or a claimant
  comment hiding on a later page of a busy issue could previously flip a
  verdict to GO silently.
- 404 errors now say `not found: <endpoint>` instead of printing the bare
  API endpoint.

### Added
- Smarter repo scans: `taken owner/repo` now ends with a GO-candidate
  recommendation summary ("2 GO candidates: a/b#1, a/b#2", or
  "no GO candidates in this scan").
- Smarter MCP `scan_repo`: results are ordered GO first, then CAUTION,
  then TAKEN, and the payload adds `recommendations` (just the GO
  targets) plus a verdict `summary`, so agents can pick a candidate
  without parsing every verdict.
- MCP Registry publishing is automated: `publish.yml` gained a
  `publish-registry` job that logs in with GitHub OIDC (no stored secrets, no
  device flow) and publishes `server.json` to the official MCP Registry on
  every release tag. `workflow_dispatch` backfills older releases.
- `taken` is now listed in the official MCP Registry as
  `io.github.RogueAlg0/taken`.

## [0.5.0] - 2026-09-26

### Added
- MCP server: `taken-mcp` exposes taken as tools for coding agents
  (`check_issue`, `scan_repo`, `discover_candidates`) over stdio. Run it with
  `uvx --from taken-gh taken-mcp`; like the CLI it uses your own `gh` login
  and only makes read-only API calls.
- `server.json` registry metadata for the official MCP registry
  (`io.github.RogueAlg0/taken`).

## [0.4.1] - 2026-09-26

### Fixed
- Discover-mode cache redesigned: one file per cache key plus an in-memory
  layer instead of a single 28MB JSON file rewritten under a lock. Parallel
  verification no longer serializes on cache writes; a full 40-candidate run
  dropped from ~6.5 minutes sequential to about a minute with 8 workers.

## [0.4.0] - 2026-09-26

### Added
- Discover mode is now parallel: candidates are verified with 8 workers by
  default (`--jobs N` to tune), cutting a full 40-candidate run from minutes
  to under a minute. The API cache is thread-safe (locked, atomic writes).
- Discover mode shows a progress bar on stderr while verifying, so long runs
  give live feedback. `--no-progress` hides it; `--json` output on stdout is
  unaffected.

## [0.3.0] - 2026-09-26

### Added
- Discover mode: `taken --discover` piggybacks on GitHub's issue search API
  (the same source the web aggregators use) for raw candidates, then runs
  taken's full verification on each and ranks the survivors. No aggregator
  filters on the ranking signal that matters most: maintainer responsiveness.
  Only GO verdicts are ranked, scored on maintainer replies (+3), recent
  updates (+2), and repo push activity (+1), with an explainable breakdown
  per candidate. `--language` and `--min-stars` narrow the search;
  `--limit` caps the output (default 10); `--label` restricts to one label
  instead of the default set.

## [0.2.0] - 2026-09-26

### Added
- Batch mode: pass several targets (or `--file`) and get one verdict line
  per target. Exit code is 0 when every target produced a verdict, 3 when
  any target failed.
- Repo scan: a bare `owner/repo` target automatically discovers the repo's
  open issues (PRs excluded, most recently updated first) and checks each
  one. `--limit N` caps the scan (default 20); `--label` filters by label.
- API response cache: successful `gh api` responses are cached for one hour
  in `~/.cache/taken` (`TAKEN_CACHE_DIR` overrides), so repeated scans stay
  cheap. `--no-cache` bypasses it. The cache never breaks the tool: any
  cache error is ignored and the request goes out normally.

## [0.1.2] - 2026-09-26

### Fixed
- `taken --version` now reports the installed distribution version (read
  from package metadata) instead of a stale hardcoded string.

## [0.1.1] - 2026-09-26

### Added
- Published to PyPI as `taken-gh` via trusted publishing (GitHub Actions
  OIDC). Install with `uv tool install taken-gh` or `pipx install taken-gh`;
  README and the project page now point at PyPI instead of a git URL.

## [Unreleased]

### Fixed
- Fail closed: every GitHub API check now validates the shape of the
  response and raises a hard error (exit code 3) on anything unexpected,
  including unreadable CONTRIBUTING files. A failed check can no longer
  degrade quietly into a GO verdict.

### Added
- Refusal-path tests: AI-policy ban detection on a sample CONTRIBUTING file,
  malformed targets (exit code 3 with a clean error, no traceback), and a
  positive test that the --me filter turns an own-comment-only thread into GO.
- CI smoke job: builds the wheel, installs it into a fresh venv, and
  exercises the installed `taken` entry point (--version, --help, and a
  malformed target expecting exit code 3).
- README documents the --json output schema (field names and verdict values).
- README with usage, three real examples, and the verdict rules;
  CONTRIBUTING guide; pull request template.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`): runs `ruff check`,
  `ruff format --check`, and `pytest` on push and pull requests.
- Pytest suite (`tests/`): 31 tests covering the verdict logic (GO, TAKEN,
  CAUTION, and precedence) and the claimant-pattern matching.
- Command-line interface (`taken/cli.py`): `taken owner/repo#123` (full
  issue URLs also accepted), with `--json`, `--me`, `--version`, and
  `--help`. Exit codes 0 (GO), 1 (TAKEN), 2 (CAUTION), 3 (error).
- Verdict logic (`taken/verdict.py`): GO, TAKEN, and CAUTION, with TAKEN
  winning over CAUTION winning over GO. A claimant comment is a soft signal
  (CAUTION); a closed issue, an open linked PR, or an assignee is a hard
  signal (TAKEN).
- GitHub API checks (`taken/checks.py`): issue basics, timeline
  cross-reference scan, claimant language scan, AI policy detection, and repo
  health. All read-only via the `gh` CLI, using the invoker's own auth.
