# Architecture

How `taken` is put together. This is a map for contributors: where the
decision happens, where the data comes from, and which invariants must not
break.

## The pipeline

```
fetch  ->  decide  ->  present
```

`taken` answers one question about a GitHub issue: is it already taken?
The pipeline has three stages, kept deliberately separate:

1. **Fetch** (`taken/checks.py`, or `taken/graphql.py`): gather evidence
   from the GitHub API into a plain `findings` dict.
2. **Decide** (`taken/verdict.py`): a pure function over `findings`
   returning `(verdict, reasons)`. No network, no I/O.
3. **Present** (`taken/cli.py`): format the verdict for humans or as JSON.

The `findings` dict is the contract between fetch and decide. Anything that
gathers evidence must produce the same shape, so verdict logic can never
drift between fetch paths.

## Modules

- `taken/cli.py` - argument parsing, output formatting (human and
  `--json`), and the run modes: single issue, batch (multiple targets),
  `--discover`, `--clear-cache`. Entry point for the `taken` command.
- `taken/checks.py` - the REST fetch layer (the largest module). It wraps
  `gh api`, owns the cache, paces search requests, retries on rate limits,
  and builds findings: `check_issue`, `check_timeline`, `fetch_comments` /
  `find_claimant_hits`, `check_ai_policy` / `classify_policy`,
  `check_repo_health` / `count_recent_contributors`. `run_checks` runs them
  all for one issue.
- `taken/verdict.py` - `decide(findings)`. Pure logic, fully unit-tested.
  Precedence is fixed: TAKEN wins over CAUTION wins over GO. Rules live
  here: linked PRs, assignees, claimant comments, AI policy, truncation
  (a scan that hit the page cap downgrades to CAUTION rather than risk a GO
  on partial evidence), repo health, and difficulty-fit (a beginner label
  next to a long thread or a design-level label).
- `taken/graphql.py` - opt-in fetch path (`--graphql`). One GraphQL query
  per issue instead of ~10 REST calls, then maps every result back into the
  REST findings shape. `--persistent-session` reuses one HTTPS keep-alive
  connection. Because both paths feed the same `decide()`, the verdict
  cannot differ by transport.
- `taken/discover.py` - `--discover` mode. Builds one OR-combined search
  query across beginner labels, verifies each candidate with the full
  single-issue pipeline in parallel workers, and ranks by maintainer
  responsiveness (`score_candidate`).
- `taken/mcp_server.py` - the `taken-mcp` stdio server. Three tools:
  `check_issue`, `scan_repo`, `discover_candidates`. Thin wrappers over the
  same functions the CLI uses.
- `taken/budget.py` - API budget tiers (anonymous vs authenticated), paging caps, and worker sizing.
- `taken/health.py` - the `--health` maintainer report: unattended claims, stale PRs, untouched beginner issues, and untriaged issues.

## Invariants

These are load-bearing. Do not break them without discussion.

- **Read-only against GitHub.** `taken` only issues GETs. It never creates,
  edits, or closes anything. There is no write path to remove.
- **No tokens in-process.** API calls shell out to `gh api` with the
  invoker's own auth; `taken` never sees a token. The one exception is
  `--persistent-session`, which holds the token from `gh auth token` in
  memory only, never on disk or in logs.
- **Fail closed.** A scan that stopped early (page cap reached), a cache
  that cannot be read, or missing evidence downgrades toward CAUTION, never
  toward GO on thinner proof.
- **Cache is a performance layer, not a truth layer.** API responses are
  cached for 1 hour on disk (`~/.cache/taken`, overridable with
  `TAKEN_CACHE_DIR`) plus an in-memory layer. `--no-cache` bypasses it.
  A stale cache can hide fresh state; that is documented, not hidden.
- **Vendored console copies.** `docs/console/py/checks.py`, `docs/console/py/verdict.py`
  and `docs/console/py/budget.py` are byte-identical copies of `taken/` for the
  in-browser Pyodide console. CI fails if they drift. Re-copy after any change to
  those modules:
  `cp taken/checks.py docs/console/py/checks.py && cp taken/verdict.py docs/console/py/verdict.py && cp taken/budget.py docs/console/py/budget.py`.
- **Version strings move together.** `pyproject.toml` and the
  `taken --version` string in `docs/console/py/webshim.py` must match; CI enforces
  this.

## Data flow: one issue

1. `cli.py` parses the target (a URL or `owner/repo#number`).
2. `run_checks` (REST) or `run_checks_graphql` (GraphQL) builds `findings`:
   `issue` (state, labels, assignees), `linked_prs` (from the timeline, not
   just comments), `claimants` (comments matching claim patterns),
   `ai_policy` (CONTRIBUTING/README classification), `repo_health`
   (recent pushes and merges), and comment totals.
3. `decide(findings)` returns the verdict with cited reasons.
4. `cli.py` prints it, or `--json` for machines.

## Data flow: --discover

1. `build_query` makes one search across beginner-label variants.
2. `_collect_candidates` pages the search results.
3. `_verify_candidate` runs the full single-issue pipeline per candidate in
   worker threads (this is the expensive part by design: ~4-6 API calls
   each, paced for the search rate limit).
4. `score_candidate` ranks by maintainer engagement signals; results print
   ranked.

## Testing

- `tests/` mirrors the modules. Verdict rules get focused unit tests with
  constructed findings (no network). Fetch-layer tests mock `gh api`.
- New or changed decision logic needs focused coverage; the bar is
  meaningful cases (boundaries, precedence, no-fire cases), not a
  repository-wide percentage.
