/* Live taken? checks, right in the browser.
 *
 * Read-only calls to api.github.com, unauthenticated (60 requests/hour per
 * IP, no login needed for public repos). Ports the core of taken/checks.py,
 * taken/verdict.py, and the human output format of taken/cli.py.
 *
 * This is a faithful but simplified copy: no request cache, no discover
 * mode (the search API would eat the hourly budget), no repo scans.
 */
(function () {
  "use strict";

  var API = "https://api.github.com";
  var HEALTH_WINDOW_DAYS = 30;

  var CLAIMANT_PATTERNS = [
    "assign me",
    "can i work on",
    "i'd like to take",
    "i would like to work",
    "please assign",
    "working on this",
    "i'll take this",
    "i will take",
    "i'd love to take",
    "i'd love to work on",
    "could you assign",
    "assign this issue to me",
  ];

  var BAN_PHRASES = [
    "does not accept ai",
    "do not accept ai",
    "will not accept ai",
    "no ai-generated",
  ];

  var DISCLOSURE_PHRASES = [
    "assisted-by",
    "ai-assisted",
    "disclose",
    "generative ai",
  ];

  var CONTRIBUTING_PATHS = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
  ];

  var PR_URL_RE = /^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/;

  function NotFound(message) { this.message = message; this.name = "NotFound"; }
  function RateLimited(resetAt) { this.resetAt = resetAt; this.name = "RateLimited"; }
  function ApiError(message) { this.message = message; this.name = "ApiError"; }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function api(path) {
    var res;
    try {
      res = await fetch(API + path, {
        headers: { "Accept": "application/vnd.github+json" },
      });
    } catch (e) {
      throw new ApiError("network error talking to api.github.com");
    }
    if (res.status === 404) throw new NotFound(path);
    if (res.status === 403) {
      var remaining = res.headers.get("X-RateLimit-Remaining");
      if (remaining === "0") {
        throw new RateLimited(res.headers.get("X-RateLimit-Reset"));
      }
      throw new ApiError("GitHub API refused the request (403)");
    }
    if (!res.ok) throw new ApiError("GitHub API returned HTTP " + res.status);
    return res.json();
  }

  function parseTarget(text) {
    text = text.trim();
    var m = text.match(/^https?:\/\/github\.com\/([^/\s]+)\/([^/\s]+)\/issues\/(\d+)\/?$/)
      || text.match(/^([^/\s#]+)\/([^/\s#]+)#(\d+)$/);
    if (!m) return null;
    return { owner: m[1], repo: m[2], number: parseInt(m[3], 10) };
  }

  async function checkIssue(owner, repo, number) {
    var data = await api("/repos/" + owner + "/" + repo + "/issues/" + number);
    return {
      number: number,
      state: data.state,
      title: data.title,
      labels: (data.labels || []).map(function (l) { return l.name; }),
      assignees: (data.assignees || []).map(function (u) { return u.login; }),
      comment_count: data.comments || 0,
      author: (data.user || {}).login,
      url: data.html_url,
      created_at: data.created_at,
    };
  }

  async function checkTimeline(owner, repo, number) {
    var events = await api(
      "/repos/" + owner + "/" + repo + "/issues/" + number + "/timeline?per_page=100");
    var linked = [], seen = {};
    for (var i = 0; i < events.length; i++) {
      var event = events[i] || {};
      if (event.event !== "cross-referenced" && event.event !== "connected") continue;
      var src = ((event.source || {}).issue || {});
      var match = PR_URL_RE.exec(src.html_url || "");
      if (!match) continue;
      var key = match[1] + "/" + match[2] + "#" + match[3];
      if (seen[key]) continue;
      seen[key] = true;
      var pr = await api("/repos/" + match[1] + "/" + match[2] + "/pulls/" + match[3]);
      linked.push({
        number: parseInt(match[3], 10),
        title: pr.title,
        state: pr.state,
        merged: !!pr.merged_at,
        author: (pr.user || {}).login,
        url: pr.html_url,
      });
    }
    return linked;
  }

  function findClaimantHits(comments, me) {
    var hits = [], meLower = (me || "").toLowerCase();
    comments.forEach(function (comment) {
      var author = ((comment.user || {}).login) || "";
      if (meLower && author.toLowerCase() === meLower) return;
      var body = comment.body || "";
      var lowered = body.toLowerCase();
      var matched = null;
      for (var i = 0; i < CLAIMANT_PATTERNS.length; i++) {
        if (lowered.indexOf(CLAIMANT_PATTERNS[i]) !== -1) { matched = CLAIMANT_PATTERNS[i]; break; }
      }
      if (!matched) return;
      hits.push({
        author: author,
        date: (comment.created_at || "").slice(0, 10),
        pattern: matched,
        snippet: body.split(/\s+/).join(" ").slice(0, 160),
        url: comment.html_url,
      });
    });
    return hits;
  }

  function firstLineWith(text, phrase) {
    var lines = text.split("\n");
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].toLowerCase().indexOf(phrase) !== -1) return lines[i].trim().slice(0, 160);
    }
    return "";
  }

  function classifyPolicy(text) {
    var i, snippet;
    for (i = 0; i < BAN_PHRASES.length; i++) {
      snippet = firstLineWith(text, BAN_PHRASES[i]);
      if (snippet) return { verdict: "ban", snippet: snippet };
    }
    for (i = 0; i < DISCLOSURE_PHRASES.length; i++) {
      snippet = firstLineWith(text, DISCLOSURE_PHRASES[i]);
      if (snippet) return { verdict: "disclosure-required", snippet: snippet };
    }
    return { verdict: "none-found", snippet: "" };
  }

  function b64decodeUtf8(b64) {
    var bin = atob(b64.replace(/\s/g, ""));
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  async function checkAiPolicy(owner, repo) {
    for (var i = 0; i < CONTRIBUTING_PATHS.length; i++) {
      var path = CONTRIBUTING_PATHS[i];
      var data;
      try {
        data = await api("/repos/" + owner + "/" + repo + "/contents/" + path);
      } catch (e) {
        if (e.name === "NotFound") continue;
        throw e;
      }
      var text = b64decodeUtf8(data.content || "");
      var cls = classifyPolicy(text);
      return { verdict: cls.verdict, snippet: cls.snippet, source: path };
    }
    return { verdict: "none-found", snippet: "", source: null };
  }

  async function checkRepoHealth(owner, repo) {
    var data = await api("/repos/" + owner + "/" + repo);
    var pushedAt = data.pushed_at || "";
    var pushedRecently = false;
    if (pushedAt) {
      pushedRecently = (Date.now() - new Date(pushedAt).getTime())
        <= HEALTH_WINDOW_DAYS * 24 * 3600 * 1000;
    }
    var cutoff = Date.now() - HEALTH_WINDOW_DAYS * 24 * 3600 * 1000;
    var recentMerges = 0;
    for (var page = 1; page <= 2; page++) {
      var prs = await api("/repos/" + owner + "/" + repo +
        "/pulls?state=closed&per_page=50&page=" + page + "&sort=updated&direction=desc");
      if (!prs.length) break;
      prs.forEach(function (pr) {
        if (pr.merged_at && new Date(pr.merged_at).getTime() >= cutoff) recentMerges++;
      });
      if (prs.length < 50) break;
    }
    return {
      pushed_at: pushedAt.slice(0, 10),
      pushed_recently: pushedRecently,
      recent_merges: recentMerges,
      stars: data.stargazers_count || 0,
    };
  }

  function decide(findings) {
    var takenReasons = [], cautionReasons = [];
    var issue = findings.issue;

    if (issue.state === "closed") {
      takenReasons.push("issue is closed: " + issue.url);
    }
    findings.linked_prs.forEach(function (pr) {
      if (pr.state === "open") {
        takenReasons.push("open PR #" + pr.number + " already covers this: " + pr.url);
      } else if (pr.merged) {
        cautionReasons.push("PR #" + pr.number + " was merged but the issue is still open (stale?): " + pr.url);
      }
    });
    if (issue.assignees.length) {
      takenReasons.push("assigned to: " + issue.assignees.join(", "));
    }
    findings.claimants.forEach(function (hit) {
      cautionReasons.push(hit.author + ' expressed interest on ' + hit.date + ': "' +
        hit.snippet + '" (' + hit.url + ")");
    });
    var policy = findings.ai_policy.verdict;
    if (policy === "ban") cautionReasons.push("repo bans AI-generated contributions");
    else if (policy === "disclosure-required") cautionReasons.push("repo requires AI disclosure on contributions");
    var health = findings.repo_health;
    if (!health.pushed_recently && health.recent_merges === 0) {
      cautionReasons.push("repo looks stale: no pushes or merges in the last 30 days");
    }

    if (takenReasons.length) return { verdict: "TAKEN", reasons: takenReasons };
    if (cautionReasons.length) return { verdict: "CAUTION", reasons: cautionReasons };
    return { verdict: "GO", reasons: ["no linked PRs, no assignees, no claimants, repo is active"] };
  }

  function verdictClass(v) {
    return v === "GO" ? "v-go" : v === "TAKEN" ? "v-taken" : "v-caution";
  }

  function formatHuman(findings, verdict, reasons) {
    var issue = findings.issue, health = findings.repo_health, policy = findings.ai_policy;
    var out = [];
    out.push("taken? " + esc(findings.target));
    out.push('verdict: <span class="' + verdictClass(verdict) + '">' + verdict + "</span>");
    out.push("");
    out.push('  issue: ' + esc(issue.state) + ', "' + esc(issue.title) + '"');
    out.push("         " + esc(issue.url) + " (" + issue.comment_count + " comments)");
    if (findings.linked_prs.length) {
      findings.linked_prs.forEach(function (pr) {
        var status = pr.state === "open" ? "open" : pr.merged ? "merged" : "closed";
        out.push('  linked PR: #' + pr.number + ' "' + esc(pr.title) + '" (' + status + ")");
        out.push("             " + esc(pr.url));
      });
    } else {
      out.push("  linked PRs: none found in timeline");
    }
    out.push("  assignees: " + (issue.assignees.length ? esc(issue.assignees.join(", ")) : "none"));
    if (findings.claimants.length) {
      findings.claimants.forEach(function (hit) {
        out.push('  claimant: ' + esc(hit.author) + " on " + esc(hit.date) +
          ' (matched "' + esc(hit.pattern) + '")');
        out.push('            "' + esc(hit.snippet) + '"');
      });
    } else {
      out.push("  claimants: none found in comments");
    }
    if (policy.source) {
      out.push("  AI policy: " + esc(policy.verdict) + " (" + esc(policy.source) + ")");
      if (policy.snippet) out.push('             "' + esc(policy.snippet) + '"');
    } else {
      out.push("  AI policy: none found (no CONTRIBUTING file)");
    }
    out.push("  repo health: pushed " + esc(health.pushed_at || "unknown") + ", " +
      health.recent_merges + " PRs merged in last 30 days, " + health.stars + " stars");
    out.push("");
    out.push("why:");
    reasons.forEach(function (r) { out.push("  - " + esc(r)); });
    out.push("");
    out.push('<span class="dim">live check via GitHub\'s public API, no login. ' +
      "Simplified port of taken " + esc(window.TAKEN_VERSION || "") + ": " +
      "scans the first 100 timeline events and comments (API budget); " +
      "the CLI scans up to 5 pages and caches.</span>");
    return out.join("\n");
  }

  async function runCheck(targetText, me) {
    var target = parseTarget(targetText);
    if (!target) {
      return { error: "could not parse " + JSON.stringify(targetText) +
        "; use owner/repo#123 or a GitHub issue URL" };
    }
    try {
      var results = await Promise.all([
        checkIssue(target.owner, target.repo, target.number),
        checkTimeline(target.owner, target.repo, target.number),
        api("/repos/" + target.owner + "/" + target.repo +
          "/issues/" + target.number + "/comments?per_page=100"),
        checkAiPolicy(target.owner, target.repo),
        checkRepoHealth(target.owner, target.repo),
      ]);
      var issue = results[0];
      var linkedPrs = results[1];
      var comments = results[2];
      var aiPolicy = results[3];
      var repoHealth = results[4];

      var findings = {
        target: target.owner + "/" + target.repo + "#" + target.number,
        issue: issue,
        linked_prs: linkedPrs,
        claimants: findClaimantHits(comments, me),
        ai_policy: aiPolicy,
        repo_health: repoHealth,
      };
      var d = decide(findings);
      return { html: formatHuman(findings, d.verdict, d.reasons), verdict: d.verdict };
    } catch (e) {
      if (e.name === "NotFound") {
        return { error: "not found: " + target.owner + "/" + target.repo +
          "#" + target.number + ". Check the owner, repo, and issue number." };
      }
      if (e.name === "RateLimited") {
        var when = e.resetAt ? new Date(e.resetAt * 1000).toLocaleTimeString() : "a little while";
        return { error: "GitHub API rate limit reached (60/hr without login). Try again after " + when + "." };
      }
      return { error: String((e && e.message) || e) };
    }
  }

  window.TakenLive = { parseTarget: parseTarget, runCheck: runCheck };
})();
