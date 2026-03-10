"""GitHub CLI helpers — thin wrappers around `gh`."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger("agent-loop")


def gh(*args: str, timeout: int = 30) -> str:
    """Run a gh command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            if result.stderr:
                log.debug("gh %s failed: %s", args[0], result.stderr.strip())
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("gh command failed: %s", e)
        return ""


def gh_json(*args: str, timeout: int = 30) -> Any:
    """Run a gh command and parse JSON output. Returns None on failure."""
    output = gh(*args, timeout=timeout)
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def issue_list(repo: str, *, label: str = "", state: str = "open",
               fields: str = "number,title,labels", jq: str = "",
               limit: int = 30) -> list[dict]:
    """List issues from a repo."""
    args = ["issue", "list", "-R", repo, "--state", state,
            "--json", fields, "--limit", str(limit)]
    if label:
        args.extend(["-l", label])
    if jq:
        args.extend(["-q", jq])
        return gh(*args) or ""  # type: ignore[return-value]
    result = gh_json(*args)
    return result if isinstance(result, list) else []


def issue_view(repo: str, num: int, *, fields: str = "body", jq: str = "") -> str:
    """View an issue field."""
    args = ["issue", "view", str(num), "-R", repo, "--json", fields]
    if jq:
        args.extend(["-q", jq])
    return gh(*args)


def issue_comment(repo: str, num: int, body: str) -> bool:
    """Post a comment on an issue."""
    return bool(gh("issue", "comment", str(num), "-R", repo, "-b", body))


def issue_edit_labels(repo: str, num: int, *,
                      add: list[str] | None = None,
                      remove: list[str] | None = None) -> bool:
    """Edit issue labels."""
    args = ["issue", "edit", str(num), "-R", repo]
    for label in (add or []):
        args.extend(["--add-label", label])
    for label in (remove or []):
        args.extend(["--remove-label", label])
    return bool(gh(*args))


def issue_close(repo: str, num: int) -> bool:
    """Close an issue."""
    return bool(gh("issue", "close", str(num), "-R", repo))


def pr_list(repo: str, *, state: str = "open", fields: str = "number,body",
            jq: str = "") -> list[dict]:
    """List PRs."""
    args = ["pr", "list", "-R", repo, "--state", state, "--json", fields]
    if jq:
        args.extend(["-q", jq])
        return gh(*args) or ""  # type: ignore[return-value]
    result = gh_json(*args)
    return result if isinstance(result, list) else []


def pr_diff(repo: str, num: int) -> str:
    """Get PR diff."""
    return gh("pr", "diff", str(num), "-R", repo, timeout=60)


def pr_view(repo: str, num: int, *, fields: str = "comments", jq: str = "") -> str:
    """View PR fields."""
    args = ["pr", "view", str(num), "-R", repo, "--json", fields]
    if jq:
        args.extend(["-q", jq])
    return gh(*args)


def pr_comment(repo: str, num: int, body: str) -> bool:
    """Post a comment on a PR."""
    return bool(gh("pr", "comment", str(num), "-R", repo, "--body", body))


def pr_merge(repo: str, num: int, *, squash: bool = True) -> bool:
    """Merge a PR."""
    args = ["pr", "merge", str(num), "-R", repo]
    if squash:
        args.append("--squash")
    return bool(gh(*args))


def pr_checks(repo: str, num: int) -> str:
    """Get PR check status.

    Note: ``gh pr checks`` exits non-zero when any check fails, so we
    cannot use the generic ``gh()`` wrapper (which returns "" on failure).
    We need stdout regardless of exit code.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", str(num), "-R", repo],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("gh pr checks failed: %s", e)
        return ""


def pr_checks_billing_failure(repo: str, num: int) -> bool:
    """Check if ALL CI failures are billing/infrastructure issues (not code failures).

    Returns True if every failing check failed due to account billing, meaning
    the CI results are not meaningful for code quality assessment.
    """
    pr_ref = gh("pr", "view", str(num), "-R", repo,
                "--json", "headRefOid", "--jq", ".headRefOid")
    if not pr_ref:
        return False
    pr_ref = pr_ref.strip()
    check_runs_json = gh("api",
                         f"repos/{repo}/commits/{pr_ref}/check-runs",
                         "--jq", ".check_runs")
    if not check_runs_json:
        return False
    try:
        check_runs = json.loads(check_runs_json)
    except (ValueError, TypeError):
        return False

    failing = [cr for cr in check_runs
               if cr.get("conclusion") == "failure"]
    if not failing:
        return False

    # Check annotations for each failing run
    for cr in failing:
        cr_id = cr.get("id")
        if not cr_id:
            return False
        annotations_json = gh("api",
                              f"repos/{repo}/check-runs/{cr_id}/annotations")
        if not annotations_json:
            return False
        try:
            annotations = json.loads(annotations_json)
        except (ValueError, TypeError):
            return False
        # ALL annotations must be billing-related for this to be a billing failure
        if not annotations:
            return False
        if not all("payment" in (a.get("message", "") or "").lower()
                   or "spending limit" in (a.get("message", "") or "").lower()
                   for a in annotations):
            return False

    log.info("All %d failing CI checks are billing/infrastructure failures", len(failing))
    return True


def graphql(query: str, **variables: str) -> dict | None:
    """Run a GraphQL query."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        args.extend(["-f", f"{key}={val}"])
    return gh_json(*args, timeout=30)


def find_pr_for_issue(repo: str, issue_num: int) -> int | None:
    """Find the open PR number linked to an issue."""
    prs = pr_list(repo)
    for pr in prs:
        body = pr.get("body", "")
        if f"#{issue_num}" in body:
            return pr.get("number")
    return None
