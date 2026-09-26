# taken?

`taken?` answers one question before you volunteer for a GitHub issue: is it
already taken?

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
- `--limit N`: scan mode checks at most N open issues per repo (default: 20)
- `--label LABEL`: scan mode only considers open issues carrying this label
- `--no-cache`: bypass the API response cache
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
`--no-cache` skips the cache.

## Discover mode

`taken --discover` finds contribution candidates across GitHub. It
piggybacks on GitHub's issue search API (the same source the web
aggregators use) for raw candidates, then runs taken's full verification
on each one and ranks the survivors. Only GO verdicts make the list.

    taken --discover --language python --min-stars 50 --limit 10
      6  octocat/hello-world#42  maintainer replied; updated 1d ago; repo pushed 0d ago
      4  octocat/other-repo#7    updated 3d ago; repo pushed 2d ago

Candidates are scored on the signal no aggregator filters on: maintainer
responsiveness. A non-author, non-bot comment scores +3; an issue updated
in the last 7 days scores +2; a repo pushed in the last 7 days scores +1.
Each line explains its own score. `--label` restricts the search to one
label instead of the default set (`good first issue`, `good-first-issue`,
`beginner friendly`, `help wanted`).

Candidates are verified in parallel (8 workers by default; `--jobs N`
tunes it). A progress bar on stderr shows live feedback during the run;
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
  policy to respect, or the repo looks inactive.
- GO: none of the above.

The claimant scan is a heuristic over comment text, not proof. The verdict
always prints its evidence so you can judge for yourself.

## Development

    uv sync          # install dev tools (ruff, pytest)
    uv run pytest
    uv run ruff check
    uv run ruff format --check

## AI assistance

This project is built with AI assistance, and says so openly. Every
contribution is reviewed and understood by its author before it lands.

<!-- copilot-probe: temporary line to verify automatic code review -->
