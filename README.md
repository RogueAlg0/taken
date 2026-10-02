# taken?

[![PyPI](https://img.shields.io/pypi/v/taken-gh?cacheSeconds=3600)](https://pypi.org/project/taken-gh/)
[![npm](https://img.shields.io/npm/v/taken-gh?cacheSeconds=3600)](https://www.npmjs.com/package/taken-gh)
[![CI](https://github.com/RogueAlg0/taken/actions/workflows/ci.yml/badge.svg)](https://github.com/RogueAlg0/taken/actions)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/RogueAlg0/taken/badge)](https://scorecard.dev/viewer/?uri=github.com/RogueAlg0/taken)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14960/badge)](https://www.bestpractices.dev/projects/14960)
[![SonarCloud Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=RogueAlg0_taken&metric=alert_status)](https://sonarcloud.io/project/overview?id=RogueAlg0_taken)
[![License: MIT](https://img.shields.io/github/license/RogueAlg0/taken)](LICENSE)
<!-- Static python badge: shields' pypi/pyversions reads trove classifiers,
     not requires-python, so it renders "missing" until a release ships with
     the Python classifiers below. -->
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://pypi.org/project/taken-gh/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.RogueAlg0%2Ftaken-blue)](https://registry.modelcontextprotocol.io)
[![Hotspots](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FRogueAlg0%2Ftaken%2Fmain%2Fdocs%2Fbadges%2Fhotspot.json)](https://github.com/RogueAlg0/taken#hotspots)

<!-- mcp-name: io.github.RogueAlg0/taken -->

`taken?` answers one question before you volunteer for a GitHub issue: is it
already taken?

In plain terms, it checks an issue before you start work: is it still open to
contributions, is anyone already on it, and is the repo responsive enough that
your PR will get reviewed.

Try it without installing anything: the [live console](https://roguealg0.github.io/taken/console/)
runs the real checks in your browser.

The catch it was built for: GitHub shows "linked a pull request" events in the
issue timeline, but never in the comments. You can read every comment on an
issue and still miss that someone already opened a PR for it. `taken?` checks
the timeline, scans comments for people claiming the work, looks at assignees,
reads CONTRIBUTING.md for AI contribution policies, and checks whether the
repo is still active. Then it gives a verdict, GO, TAKEN, or CAUTION, with the
evidence cited.

## Requirements

- Python 3.10 or newer
- The [GitHub CLI](https://cli.github.com/) (`gh`), authenticated

`taken?` only makes read-only API calls through your own `gh` login. It never
sees or stores tokens, and it never writes anything to GitHub.

## Install as a tool

    uv tool install taken-gh
    taken owner/repo#123

Or with pipx:

    pipx install taken-gh
    taken owner/repo#123

## Usage

    uv run taken owner/repo#123

A full issue URL works too:

    uv run taken https://github.com/owner/repo/issues/123

Useful flags:

- `--json`: print the full findings as JSON instead of the human summary
- `--me LOGIN`: ignore your own comments when scanning for claimants
- `--file PATH`: read targets from a file, one per line
- `--limit N`: scan mode checks at most N open issues per repo; in `--discover` mode, returns at most N candidates (default: 20)
- `--label LABEL`: scan mode only considers open issues carrying this label
- `--no-cache`: bypass the API response cache
- `--clear-cache`: delete the API response cache and exit
- `--graphql`: force the GraphQL fetch path even when not logged in to GitHub
  (falls back to REST on failure); GraphQL is already the default for
  logged-in users (or `TAKEN_GRAPHQL=1`)
- `--rest`: force the REST fetch path even when logged in; escape hatch for
  the GraphQL default (or `TAKEN_REST=1`)
- `--persistent-session`: run the GraphQL query over one persistent HTTPS
  connection for the whole process; the token comes from `gh auth token`
  and is held in memory only. Opt-in (or `TAKEN_PERSISTENT_SESSION=1`);
  see GRAPHQL_NOTES.md for the security tradeoff
- `--pr-idle-days N`: stale-claim decay: an open linked PR with no activity
  for longer than N days weakens from TAKEN to CAUTION (default: 90)
- `--claim-silence-days N`: stale-claim decay: days one claim blocks as
  CAUTION on a simple issue; the clock resets on any claimant activity
  (default: 7)
- `--claim-silence-complex-days N`: stale-claim decay: days one claim blocks
  as CAUTION on a complex issue (default: 14)
- `--health`: maintainer report for one repo instead of a contributor verdict:
  `taken --health owner/repo` lists claims waiting on a maintainer reply,
  claims gone quiet after a reply, stale PRs, stale `good first issue` /
  `hacktoberfest` labels, and untriaged issues. Read-only, exit 0.
  `--claim-wait-days` (default: 7), `--pr-stale-days` (default: 14), and
  `--gfi-stale-days` (default: 30) tune the thresholds.
- `--version`, `--help`

Exit codes: 0 means GO, 1 means TAKEN, 2 means CAUTION, 3 means something
broke (bad target, no `gh`, API error). With several targets the exit code
is 0 when every target produced a verdict and 3 when any target failed.

## Batch mode and repo scans

Give `taken` several targets and it prints one verdict line per target:

    taken owner/repo#123 owner/repo#124 --me mylogin
    TAKEN   owner/repo#123  assigned to: dk5488
    GO      owner/repo#124  no linked PRs, no assignees, no claimants, repo is active

A bare `owner/repo` scans the repo automatically: its open issues
(most recently updated first, PRs excluded) are each checked:

    taken django/django --label "good first issue" --limit 10

API responses are cached for one hour in `~/.cache/taken`
(override with `TAKEN_CACHE_DIR`), so repeated scans stay cheap.
`--no-cache` skips the cache; `--clear-cache` deletes it.

## Discover mode

`taken --discover` finds contribution candidates across GitHub. It
piggybacks on GitHub's issue search API (the same source the web
aggregators use) for raw candidates, then runs taken's full verification
on each one and ranks the survivors. Only GO verdicts make the list.

    taken --discover --language python --min-contributors 3 --limit 10
      6  octocat/hello-world#42  maintainer replied; updated 1d ago; repo pushed 0d ago
      4  octocat/other-repo#7    updated 3d ago; repo pushed 2d ago

Candidates are scored on the signal no aggregator filters on: maintainer
responsiveness. A non-author, non-bot comment scores +3; an issue updated
in the last 7 days scores +2; a repo pushed in the last 7 days scores +1.
Each line explains its own score. `--label` restricts the search to one
label instead of the default set (`good first issue`, `good-first-issue`,
`beginner friendly`, `help wanted`).

Every candidate is also marked for first-time friendliness: the issue's
own labels (e.g. `[good first issue]`) plus repo-level signs that
contributions are welcome (`[has CONTRIBUTING.md]`, recent merged PRs).
Repo scans annotate the GO recommendations the same way, friendliest
first:

    taken octocat/hello-world
      GO      octocat/hello-world#42  no claimant found
      ...
      1 GO candidate: octocat/hello-world#42 (good first issue)

Candidates are verified in parallel (8 workers by default; `--jobs N`
tunes it). The verify budget is split across repos by Thompson sampling
on each repo's observed GO yield (issue #130): repos that keep producing
candidates get more of the pool, while an exploration floor keeps unseen
repos getting tried. `--allocation recency` restores the old
freshest-first order, and `--explore-floor P` tunes how often the sampler
explores uniformly instead of following sampled yields (default 0.15).
A progress bar on stderr shows live feedback during the run;
`--no-progress` hides it. The bar never touches stdout, so `--json`
stays script-friendly.

## JSON output

`taken --json owner/repo#123` prints an object with three keys:

- `verdict`: one of `GO`, `TAKEN`, `CAUTION`
- `reasons`: list of human-readable strings explaining the verdict
- `findings`: the raw check results:
  - `target`: e.g. `owner/repo#123`
  - `issue`: `number`, `state`, `title`, `labels`, `assignees`,
    `comment_count`, `author`, `url`, `created_at`
  - `linked_prs`: list of `number`, `title`, `state`, `merged`, `author`, `url`
  - `claimants`: list of `author`, `date`, `pattern`, `snippet`, `url`
  - `ai_policy`: `verdict` (`ban`, `disclosure-required`, or `none-found`),
    `snippet`, `source`
  - `repo_health`: `pushed_at`, `pushed_recently`, `recent_merges`, `stars`

## MCP server

`taken` also ships as an MCP server, so coding agents can check issues with a
tool call instead of shelling out to the CLI. It needs the same setup: `gh`
installed and authenticated, and it only makes read-only API calls through
your own login.

Run it directly (the MCP server ships with every install):

    uvx --from "taken-gh[mcp]" taken-mcp

Or add it to your MCP client config:

    {
      "mcpServers": {
        "taken": {
          "command": "uvx",
          "args": ["--from", "taken-gh[mcp]", "taken-mcp"]
        }
      }
    }

Three tools:

- `check_issue(owner, repo, issue_number, me?)`: GO/TAKEN/CAUTION verdict with
  reasons and full findings for one issue.
- `scan_repo(owner, repo, limit?, label?, me?)`: the repo's open issues with
  verdicts, GO first, plus `recommendations` (just the GO targets), a
  verdict `summary`, and per-issue `friendly_labels` / `welcoming` markers
  so the safest issues to adopt stand out.
- `discover_candidates(limit?, language?, label?, min_contributors?, me?)`:
  good-first-issue style candidates, verified and ranked, each marked with
  its first-time-friendly labels and contribution-welcome signals.

`scan_repo` and `discover_candidates` include `effective_parameters` in every
response so clients can see the resolved defaults and filters behind the
results. Their MCP input schemas also describe each optional parameter's
default. Error payloads include a human-readable `error` and an `error_code`:
`rate_limited`, `not_found`, `auth_failed`, `timeout`, or `unknown`.

`taken` is published in the official
[MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.RogueAlg0/taken`.

## Examples

An issue with a PR already fixing it:

    $ uv run taken Exodus-Privacy/exodus#300
    taken? Exodus-Privacy/exodus#300
    verdict: TAKEN
      ...
      why:
        - open PR #700 already covers this: https://github.com/Exodus-Privacy/exodus/pull/700

An issue someone is already assigned to:

    $ uv run taken chahe-dridi/vscode-agent-bell#200
    taken? chahe-dridi/vscode-agent-bell#200
    verdict: TAKEN
      ...
      why:
        - assigned to: dk5488

A clean issue, with your own volunteering comment filtered out:

    $ uv run taken snowflakedb/snowflake-cli#3145 --me RogueAlg0
    taken? snowflakedb/snowflake-cli#3145
    verdict: GO
      ...
      why:
        - no linked PRs, no assignees, no claimants, repo is active

## How the verdict works

TAKEN wins over CAUTION, which wins over GO.

- TAKEN: the issue is closed, an open PR links to it, or someone is assigned.
- CAUTION: a comment says someone wants it (soft claim, they may have moved
  on), a linked PR was merged but the issue is still open, the repo has an AI
  policy to respect, the repo looks inactive, or a beginner-friendly label
  sits alongside a long discussion thread or a design-level label (worth
  skimming before you start).
- GO: none of the above.

The claimant scan is a heuristic over comment text, not proof. The verdict
always prints its evidence so you can judge for yourself.

The stale-claim decay flags `--pr-idle-days`, `--claim-silence-days`, and
`--claim-silence-complex-days` accept non-negative day counts; negative
values are rejected before checks run.

## Development

    uv sync          # install dev tools (ruff, pytest)
    uv run pytest
    uv run ruff check
    uv run ruff format --check

## Hotspots

The hotspots badge tracks files that are both complex and frequently changed:
complexity is cyclomatic complexity summed per file (via radon); churn is the
number of commits touching the file. A file needs attention when it has
complexity >= 50 and >= 5 commits; these are the files where refactoring pays
off most.

To regenerate the badge data locally:

    uv run --python 3.12 --with radon scripts/hotspots.py

## AI assistance

This project is built with AI assistance, and says so openly. Every
contribution is reviewed and understood by its author before it lands.

## Security

See [SECURITY.md](SECURITY.md) for the supported versions and how to report
a vulnerability. Please use private vulnerability reporting, not a public
issue.
