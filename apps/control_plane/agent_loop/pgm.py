"""PGM health check, sprint advance, MD drift, physical test helpers."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone

from . import github as gh
from .config import Config
from .llm import run_llm
from .state import delete_tsv_entries

log = logging.getLogger("agent-loop")


class PGMManager:
    """PGM health checks and periodic tasks."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.last_run = 0
        self.interval = 300  # 5 minutes
        self.timeout = 300

        self.sprint_last_run = 0
        self.sprint_interval = 300

    def run_health_check(self) -> None:
        """Run PGM health check if enough time has elapsed."""
        now = int(time.time())
        if now - self.last_run < self.interval:
            return
        self.last_run = now

        # Scheduled daily summary — runs at 6am/12pm/6pm regardless of changes
        self._maybe_send_daily_summary()

        # Change detection: skip if nothing changed
        if not self._issues_changed():
            log.info("PGM: no issue changes detected — skipping")
            return

        log.info("Running PGM health check...")

        # Bash pre-check tier: handle routine cases without LLM
        needs_llm = self._run_bash_pre_checks()
        if not needs_llm:
            log.info("PGM bash: all checks handled — no LLM needed")
            return

        log.info("PGM bash: escalating to LLM for complex analysis")
        prompt = self._build_pgm_prompt()

        output, exit_code = run_llm(
            self.cfg, "light", prompt,
            timeout=self.timeout,
            agent_role="pgm",
            issue_key="agent:pgm",
            cwd=self.cfg.repo_dir,
        )
        if exit_code != 0:
            log.warning("PGM health check failed (exit code: %d)", exit_code)
        else:
            log.info("PGM health check completed")

    def run_sprint_advance(self) -> None:
        """Run sprint advancement check."""
        now = int(time.time())
        if now - self.sprint_last_run < self.sprint_interval:
            return
        self.sprint_last_run = now

        script = self.cfg.repo_dir / "scripts/sprint-advance.sh"
        if not script.exists():
            log.warning("sprint-advance.sh not found")
            return

        log.info("Running sprint advancement check...")
        try:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=60,
                cwd=str(self.cfg.repo_dir),
            )
            if result.stdout:
                log.info("%s", result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("sprint-advance.sh failed: %s", e)

    def check_md_drift(self) -> None:
        """No-op: MD drift checks removed — single agent-loop on NUC, no second agent to drift."""
        pass

    def notify_pending_physical_tests(self) -> None:
        """Reset signal gate for new Physical Test Request comments."""
        gate_file = self.cfg.state_dir / "pgm-signal-sent.tsv"

        for repo in [self.cfg.nuc_repo]:
            issues = gh.issue_list(repo, label="blocker:needs-human",
                                   fields="number,title")
            for issue in issues:
                num = issue.get("number")
                if not num:
                    continue

                gate_key = f"physical-{num}"

                # Find latest Physical Test Request comment timestamp
                comments_raw = gh.gh(
                    "issue", "view", str(num), "-R", repo,
                    "--json", "comments",
                    "--jq", '.comments[] | "\\(.createdAt)\\t\\(.body | split("\\n") | .[0])"',
                )
                latest_pt_ts = 0
                for line in (comments_raw or "").splitlines():
                    if "Physical Test Request" not in line:
                        continue
                    try:
                        ts_str = line.split("\t")[0]
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts = int(dt.timestamp())
                        if ts > latest_pt_ts:
                            latest_pt_ts = ts
                    except (ValueError, IndexError):
                        pass

                # Track which PT request we already processed
                ack_file = self.cfg.state_dir / "pgm-physical-ack.tsv"
                ack_file.touch(exist_ok=True)
                ack_ts = 0
                for line in ack_file.read_text().splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[0] == gate_key:
                        try:
                            ack_ts = int(parts[1])
                        except ValueError:
                            pass

                if latest_pt_ts > ack_ts:
                    delete_tsv_entries(gate_file, rf"^{re.escape(gate_key)}\t")
                    # Record that we processed this PT request
                    delete_tsv_entries(ack_file, rf"^{re.escape(gate_key)}\t")
                    with open(ack_file, "a") as f:
                        f.write(f"{gate_key}\t{latest_pt_ts}\n")
                    log.info("Reset gate for %s: new Physical Test Request", gate_key)

    def unblock_resolved_physical_tests(self) -> None:
        """Remove blocker:needs-human if PASS/FAIL result found in comments."""
        log.info("Checking blocker:needs-human issues for unprocessed PASS/FAIL results...")

        for repo in [self.cfg.nuc_repo]:
            issues = gh.issue_list(repo, label="blocker:needs-human",
                                   fields="number,title")
            for issue in issues:
                num = issue.get("number")
                if not num:
                    continue

                comments_raw = gh.gh(
                    "issue", "view", str(num), "-R", repo,
                    "--json", "comments",
                    "--jq", '.comments[-10:][] | "\\(.body | split("\\n") | .[0])"',
                )
                if not comments_raw:
                    continue

                has_pt_request = False
                has_pass = False
                for line in comments_raw.splitlines():
                    if "Physical Test Request" in line:
                        has_pt_request = True
                    if re.search(r"Verdict.*\bPASS\b|Test PASSED", line, re.IGNORECASE):
                        has_pass = True

                # Only unblock on PASS — keep blocker on FAIL so user can retry
                if has_pt_request and has_pass:
                    log.info("Unblocking #%d on %s: found PASS result comment", num, repo)
                    gh.issue_edit_labels(repo, num,
                                         remove=["blocker:needs-human", "stuck"])

    def _maybe_send_daily_summary(self) -> None:
        """Send a daily status summary at 6am, 12pm, 6pm.

        Uses the same board format as #status: grouped by repo+sprint,
        sorted by status (done → in progress → blocked → queued).
        """
        hour = datetime.now().hour
        if hour not in (6, 12, 18):
            return

        cfg = self.cfg
        greeting = {6: "Good morning", 12: "Midday update", 18: "Evening update"}

        def _label_names(issue):
            return [label.get("name", "") if isinstance(label, dict) else str(label)
                    for label in issue.get("labels", [])]

        def _short_title(issue):
            t = issue.get("title", "")
            return t.split(" — ")[-1][:50] if " — " in t else t[:50]

        def _classify(repo, issue):
            labels = _label_names(issue)
            if any(label.startswith("blocker:") for label in labels):
                return "🚫"
            if "stuck" in labels:
                return "🚫"
            pr_num = gh.find_pr_for_issue(repo, issue["number"])
            if pr_num:
                return "🔄"
            return "⏳"

        # Build board for both repos
        lines = []
        order = {"✅": 0, "🔄": 1, "🚫": 2, "⏳": 3}

        for repo, repo_label in [(cfg.nuc_repo, "NUC")]:
            open_issues = gh.issue_list(repo, state="open",
                                        fields="number,title,labels", limit=30)
            closed_issues = gh.issue_list(repo, state="closed",
                                          fields="number,title,labels", limit=10)

            # Group by sprint label
            sprints: dict[str, list[tuple[str, dict]]] = {}
            for issue in open_issues:
                for name in _label_names(issue):
                    if name.startswith("sprint-"):
                        sprints.setdefault(name, []).append((_classify(repo, issue), issue))
            for issue in closed_issues:
                for name in _label_names(issue):
                    if name.startswith("sprint-"):
                        sprints.setdefault(name, []).append(("✅", issue))

            for sprint_label in sorted(sprints.keys()):
                tagged = sprints[sprint_label]
                tagged.sort(key=lambda x: order.get(x[0], 9))
                lines.append(f"{repo_label} ({sprint_label}):")
                for icon, issue in tagged:
                    lines.append(f"  {icon} #{issue['number']} {_short_title(issue)}")

        if not lines:
            lines.append("No issues found.")

        # Detect primary sprint for header
        sprint = "unknown"
        for line in lines:
            m = re.search(r"sprint-(\d+)", line)
            if m:
                sprint = m.group(1)
                break

        header = f"📊 PGM: {greeting.get(hour, 'Update')} — Sprint {sprint}\n"
        summary = header + "\n".join(lines)
        self._signal_gate("general", "0", summary)
        log.info("PGM: sent scheduled %s summary", greeting.get(hour, "update").lower())

    def _run_bash_pre_checks(self) -> bool:
        """Handle routine PGM cases without LLM. Returns True if LLM is needed."""
        cfg = self.cfg
        reported_file = cfg.state_dir / "pgm-reported-closures.txt"
        reported_file.touch(exist_ok=True)
        reported = set(reported_file.read_text().splitlines())
        now = int(time.time())

        # 1. New closures → templated Signal notification
        for repo, prefix in [(cfg.nuc_repo, "nuc")]:
            closed_raw = gh.issue_list(
                repo, state="closed", limit=10,
                fields="number,title,labels",
                jq='.[] | "\\(.number)\\t\\(.title)\\t\\([.labels[].name] | join(","))"',
            )
            for line in (closed_raw or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                num, title = parts[0], parts[1]
                labels = parts[2] if len(parts) > 2 else ""
                key = f"{prefix}-{num}"
                if key not in reported:
                    sprint = ""
                    m = re.search(r"sprint-\S+", labels)
                    if m:
                        sprint = f" ({m.group()})"
                    self._signal_gate("closed", num,
                                      f"📊 PGM: {prefix}#{num} closed ✅ — {title}{sprint}")
                    with open(reported_file, "a") as f:
                        f.write(key + "\n")
                    reported.add(key)
                    log.info("PGM: reported closure %s", key)

        # 2. Blockers needing human — include summary so Ophir can approve from Signal
        for repo, prefix in [(cfg.nuc_repo, "nuc")]:
            blocker_raw = gh.issue_list(
                repo, label="blocker:needs-human",
                fields="number,title",
                jq='.[] | "\\(.number)\\t\\(.title)"',
            )
            for line in (blocker_raw or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                num, title = parts[0], parts[1]
                # Fetch first 200 chars of issue body for context
                body = ""
                try:
                    raw_body = gh.issue_view(repo, int(num), fields="body", jq=".body")
                    if raw_body:
                        # Extract the "What needs updating" or "Summary" line
                        for bline in raw_body.splitlines():
                            bline = bline.strip()
                            if bline.startswith("**What needs"):
                                body = bline[:200]
                                break
                            if bline.startswith("## Summary"):
                                continue
                            if bline and not bline.startswith("#") and not bline.startswith("PR #") and not bline.startswith("_"):
                                body = bline[:200]
                                break
                except Exception:
                    pass
                msg = f"📊 PGM: #{num} needs approval — {title}"
                if body:
                    msg += f"\n\n{body}"
                msg += f"\n\nReply #approve {num} to approve."
                self._signal_gate("blocker", num, msg)

        # 3. Stuck detection — no update in 8+ hours
        stuck_threshold = now - 28800
        for repo, prefix in [(cfg.nuc_repo, "nuc")]:
            issues_raw = gh.issue_list(
                repo, label="assigned:worker",
                fields="number,title,updatedAt",
                jq='.[] | "\\(.number)\\t\\(.title)\\t\\(.updatedAt)"',
            )
            for line in (issues_raw or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                num, title, updated = parts[0], parts[1], parts[2]
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    updated_epoch = int(dt.timestamp())
                except ValueError:
                    continue
                if updated_epoch < stuck_threshold:
                    self._signal_gate("stuck", num,
                                      f"📊 PGM: {prefix}#{num} may be stuck "
                                      f"(no update for 8+ hours) — {title}")

        # 4. Orphaned issues — open >24h with no routing label
        exact_routing = {"assigned:worker", "stuck"}
        prefix_routing = ("blocker:", "sprint-")  # any blocker:* or deferred sprint
        orphan_threshold = now - 86400  # 24 hours
        for repo, prefix in [(cfg.nuc_repo, "nuc")]:
            all_open_raw = gh.issue_list(
                repo, state="open",
                fields="number,title,labels,createdAt",
                jq='.[] | "\\(.number)\\t\\(.title)\\t\\([.labels[].name] | join(","))\\t\\(.createdAt)"',
            )
            for line in (all_open_raw or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                num, title, labels_str, created = parts[0], parts[1], parts[2], parts[3]
                issue_labels = set(labels_str.split(",")) if labels_str else set()
                # Skip if it has any routing label (exact or prefix match)
                if issue_labels & exact_routing:
                    continue
                if any(label.startswith(prefix_routing) for label in issue_labels):
                    continue
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    created_epoch = int(dt.timestamp())
                except ValueError:
                    continue
                if created_epoch < orphan_threshold:
                    gh.issue_edit_labels(repo, int(num), add=["blocker:needs-human"])
                    self._signal_gate("blocker", num,
                                      f"📊 PGM: {prefix}#{num} has no routing label "
                                      f"(open >24h, needs triage) — {title}")
                    log.info("PGM: flagged orphaned issue %s#%s", prefix, num)

        # 5. CI failures → escalate to LLM
        for repo in [cfg.nuc_repo]:
            runs_raw = gh.gh(
                "run", "list", "-R", repo, "-L", "5",
                "--json", "conclusion",
                "--jq", '[.[] | select(.conclusion == "failure")] | length',
            )
            if runs_raw and int(runs_raw) > 0:
                log.info("PGM: CI failures detected on %s — escalating to LLM", repo)
                return True

        return False

    def _signal_gate(self, event_type: str, issue_id: str, message: str) -> None:
        """Send Signal notification via the rate-limited gate script."""
        gate_script = self.cfg.repo_dir / "scripts/pgm-signal-gate.sh"
        if gate_script.exists():
            try:
                subprocess.run(
                    ["bash", str(gate_script), event_type, str(issue_id), message],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.cfg.repo_dir),
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                log.warning("Signal gate failed: %s", e)

    def _issues_changed(self) -> bool:
        """Check if issues have changed since last PGM run."""
        fp_file = self.cfg.state_dir / "pgm-fingerprint.txt"

        nuc_fp = gh.issue_list(
            self.cfg.nuc_repo,
            fields="number,labels,updatedAt",
            jq='[.[] | "\\(.number):\\([.labels[].name]|sort|join(",")):\\(.updatedAt)"] | sort | join("|")',
        ) or ""
        nuc_closed_fp = gh.issue_list(
            self.cfg.nuc_repo, state="closed",
            fields="number,closedAt", limit=5,
            jq='[.[] | "\\(.number):\\(.closedAt)"] | join("|")',
        ) or ""

        current_fp = f"{nuc_fp}##{nuc_closed_fp}"
        prev_fp = fp_file.read_text().strip() if fp_file.exists() else ""

        if current_fp == prev_fp:
            return False

        fp_file.write_text(current_fp)
        return True

    def _build_pgm_prompt(self) -> str:
        """Build the PGM health check prompt."""
        cfg = self.cfg

        pgm_md = cfg.repo_dir / ".claude/agents/pgm.md"
        pgm_content = pgm_md.read_text() if pgm_md.exists() else ""

        nuc_issues = gh.issue_list(
            cfg.nuc_repo,
            fields="number,title,labels,updatedAt,createdAt",
            jq='.[] | "Issue #\\(.number): \\(.title)\\n  Labels: \\([.labels[].name] | join(", "))\\n  Updated: \\(.updatedAt)\\n  Created: \\(.createdAt)"',
        ) or "No issues found"

        nuc_closed = gh.issue_list(
            cfg.nuc_repo, state="closed", limit=20,
            fields="number,title,labels,closedAt",
            jq='.[] | "Issue #\\(.number): \\(.title)\\n  Labels: \\([.labels[].name] | join(", "))\\n  Closed: \\(.closedAt)"',
        ) or "None"

        reported_file = cfg.state_dir / "pgm-reported-closures.txt"
        reported_file.touch()
        already_reported = reported_file.read_text().strip()

        # Issue details (recent comments)
        nuc_details = self._gather_issue_details(cfg.nuc_repo)

        # Log tail
        log_tail = ""
        if cfg.log_file.exists():
            lines = cfg.log_file.read_text().splitlines()
            log_tail = "\n".join(lines[-50:])

        # Gate state
        gate_state = ""
        gate_file = cfg.state_dir / "pgm-signal-sent.tsv"
        if gate_file.exists():
            for line in gate_file.read_text().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        ts = int(parts[1])
                        dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                        gate_state += f"    {parts[0]}: last sent {dt_str}\n"
                    except ValueError:
                        pass

        # Token report
        token_report = ""
        token_script = cfg.repo_dir / "scripts/token-report.sh"
        if token_script.exists():
            try:
                result = subprocess.run(
                    ["bash", str(token_script)],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(cfg.repo_dir),
                )
                token_report = result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return f"""=== YOUR ROLE DEFINITION (from pgm.md) ===
{pgm_content}
=== END ROLE DEFINITION ===

CURRENT TIME: {now_utc}

=== REPO ({cfg.nuc_repo}) OPEN ISSUES ===
{nuc_issues if isinstance(nuc_issues, str) else ""}
NOTE: Issues labeled component:vector are for the robot (apps/vector/ subdirectory).

=== ISSUE RECENT COMMENTS ===
{nuc_details}

=== RECENTLY CLOSED ISSUES (last 20) ===
{nuc_closed if isinstance(nuc_closed, str) else ""}

=== ALREADY-REPORTED CLOSURES (do NOT re-notify for these) ===
{already_reported}
NOTE: After sending a closure/sprint notification, append the issue key (e.g. "nuc-57" or "vector-85") to {cfg.state_dir}/pgm-reported-closures.txt so you don't re-notify.
Command: echo "nuc-57" >> {cfg.state_dir}/pgm-reported-closures.txt

=== NUC AGENT-LOOP RECENT LOG (last 50 lines) ===
{log_tail}

ADDITIONAL INSTRUCTIONS (issue-specific context for this run):
- Repo: {cfg.nuc_repo} (monorepo — robot code lives in apps/vector/ subdirectory)
- Issues labeled component:vector are for the robot
- **SIGNAL SEND COMMAND — MANDATORY: Use the rate-limited gate script instead of raw Signal commands:**
  bash scripts/pgm-signal-gate.sh <event_type> <issue_id> "<message>"

  **Ophir's notification policy (the gate enforces these — just call it, don't do your own timing):**
  - closed    — once per issue, never repeat (gate blocks all retries)
  - stuck     — 24h reminder per issue (flat, no backoff)
  - blocker   — 24h reminder per issue (flat, no backoff)
  - physical  — once per issue, never repeat (mentioned in general status updates only)
  - idle      — once per idle window (resets when work is found)
  - general   — 3x/day at 6am, 12pm, 6pm only (gate blocks outside those hours)
  - premature — 24h per issue
  - pipeline  — SUPPRESSED (do NOT send)
  - ci        — SUPPRESSED (do NOT send)
  - board-status — handled by agent-loop, not PGM

  Event types you should use: stuck, physical, closed, idle, blocker, general, premature
  Do NOT send pipeline or ci events. Do NOT send board-status (agent-loop handles it).
  Examples:
    bash scripts/pgm-signal-gate.sh stuck 61 "📊 PGM: Issue #61 is STUCK..."
    bash scripts/pgm-signal-gate.sh physical 71 "📊 PGM: Reminder — Physical test waiting..."
    bash scripts/pgm-signal-gate.sh closed 39 "📊 PGM: Issue #39 closed..."
  Do NOT use the raw python3/docker Signal command — it has no rate limiting.
  ALWAYS call the gate for every notification — it handles all rate limiting.
  Current gate state (what was actually sent and when):
{gate_state}  If an event key is NOT listed above, it has NEVER been sent — you MUST send it now.
- Use: gh issue edit <num> -R {cfg.nuc_repo} --remove-label <old> --add-label <new>
- Use: gh issue comment <num> -R {cfg.nuc_repo} -b 'message'

OVERRIDE — What requires Ophir vs what is autonomous:
- **PR merges are AUTONOMOUS.** The merge gate handles all PR merges automatically (PR Review Hook APPROVED + CI passes → auto-merge). NEVER ask Ophir to approve, merge, or run `gh pr merge`. NEVER suggest "#go" or "approve" for merges.
- **Physical tests require Ophir.** ONLY when the robot physically moves and someone must watch. Use blocker:needs-human + Physical Test Request comment.
- **#go is ONLY for physical tests.** It means "I'm standing in front of the robot, start the test." It is NOT for merge approval.
- **If a PR has merge conflicts**, the worker will rebase on next dispatch. Do NOT ask Ophir to fix conflicts.
- **If blocker:needs-human is on an issue that does NOT need physical testing**, remove the label yourself: gh issue edit NUMBER -R REPO --remove-label blocker:needs-human

OVERRIDE — Premature Close Detection exceptions:
- Do NOT reopen issues whose closing comment (or any recent comment) contains: "do NOT reopen", "already fixed", "fixed directly", "fix applied to main", or "manually closed".
- Do NOT reopen issues where ALL associated PRs are closed/merged — even if there's no Worker "Test Report (PASS)" comment.
- Do NOT create new "Fix CI" issues if an identical issue title already exists (open OR recently closed within 24 hours).
- If you find a closed issue that matches Premature Close Detection criteria BUT has one of the above exceptions, skip it silently.

OVERRIDE — NEVER create quota/rate-limit issues:
- Do NOT create issues titled "LLM quota exhausted", "Vector LLM quota exhausted", or any variant.
- Quota exhaustion is TRANSIENT — it resolves automatically when the rolling window resets.
- The agent-loop already handles quota by pausing dispatch for 5 minutes and sending ONE Signal alert every 1.5 hours.
- If you see existing open quota issues, CLOSE them with comment "Quota issues are transient — closing as not actionable."

SPRINT SUMMARY INSTRUCTIONS:
- When you see recently closed issues that you haven't reported yet (not in ALREADY-REPORTED list), send a closure + sprint summary via Signal.
- Group closures by sprint label (sprint-1, sprint-4, sprint-6, etc.)

GENERAL STATUS MESSAGE INSTRUCTIONS:
- When sending a "general" status update, ALWAYS include a section for pending physical tests.
- Check for any open issues with label "blocker:needs-human" on BOTH repos.

TOKEN USAGE REPORT (include in general status messages if non-empty):
{token_report or "(no usage data)"}

Follow ALL instructions in your role definition above. The pgm.md file is the single source of truth for your behavior, with the OVERRIDE exceptions above taking precedence."""

    def _gather_issue_details(self, repo: str) -> str:
        """Gather recent comments for all open issues in a repo."""
        issue_nums = gh.issue_list(repo, fields="number", jq=".[].number")
        if not issue_nums or not isinstance(issue_nums, str):
            return ""

        details = []
        for num_str in issue_nums.split():
            num_str = num_str.strip()
            if not num_str.isdigit():
                continue
            comments = gh.gh(
                "issue", "view", num_str, "-R", repo,
                "--json", "comments",
                "--jq", '[.comments[-5:] | .[] | "  \\(.author.login) (\\(.createdAt)): \\(.body | split("\\n") | .[0])"] | join("\\n")',
            ) or "  (no comments)"
            details.append(f"Issue #{num_str} recent comments:\n{comments}\n---")

        return "\n".join(details)
