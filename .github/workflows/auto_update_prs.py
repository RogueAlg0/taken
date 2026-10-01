"""Keep open PR branches up to date with main.

Runs on every push to main. For each open, non-draft PR whose branch is
behind main but merges cleanly, merges main into the PR branch via the
update-branch API. PRs with real content conflicts are left alone for
their author.

Every failure mode is a skip, never an error: a PR that cannot be updated
(maintainer edits disabled on the fork, the author pushed concurrently,
the fork was deleted, mergeability still unknown) is simply retried on
the next push to main.
"""

import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
# GitHub computes mergeability asynchronously; right after a push to main
# open PRs report "unknown" for a short while.
MERGEABILITY_RETRIES = 6
MERGEABILITY_SLEEP = 10


def api(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            detail = json.loads(raw).get("message", raw)
        except ValueError:
            detail = raw
        return exc.code, {"error": detail}


def behind_prs():
    """Yield (number, head_sha) for open, non-draft PRs behind main."""
    status, prs = api("GET", f"/repos/{REPO}/pulls?state=open&per_page=100")
    if status != 200:
        raise SystemExit(f"could not list PRs: {status} {prs}")
    for pr in prs:
        if pr.get("draft"):
            continue
        number = pr["number"]
        state, head_sha = "unknown", None
        for _ in range(MERGEABILITY_RETRIES):
            status, full = api("GET", f"/repos/{REPO}/pulls/{number}")
            if status != 200:
                print(f"PR #{number}: unreadable ({status}); skipping")
                break
            state = full.get("mergeable_state")
            head_sha = full["head"]["sha"]
            if state != "unknown":
                break
            time.sleep(MERGEABILITY_SLEEP)
        else:
            print(f"PR #{number}: mergeability still unknown; skipping")
            continue
        if state == "behind":
            yield number, head_sha
        # "clean" is up to date; "dirty" has real conflicts for the author.


def main():
    updated = 0
    for number, head_sha in behind_prs():
        # expected_head_sha makes a concurrent author push fail the call
        # instead of racing it; that PR is retried on the next push.
        status, resp = api(
            "PUT",
            f"/repos/{REPO}/pulls/{number}/update-branch",
            {"expected_head_sha": head_sha},
        )
        if status in (200, 202):
            print(f"PR #{number}: branch updated to main")
            updated += 1
        else:
            print(f"PR #{number}: skipped ({status}: {resp.get('error')})")
    print(f"done: {updated} PR branch(es) updated")


if __name__ == "__main__":
    main()
