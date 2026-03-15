"""Worker dispatch — prompt building, worktree management, PR review, merge gate."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import github as gh
from .config import Config
from .llm import check_quota_exhausted, run_llm
from .state import delete_tsv_entries

log = logging.getLogger("agent-loop")


def _is_vector_issue(repo: str, issue_num: int) -> bool:
    """Check if an issue has the component:vector label."""
    result = gh.issue_view(
        repo, issue_num, fields="labels",
        jq='[.labels[].name] | any(test("component:vector"))',
    )
    return result is not None and result.strip() == "true"


def get_dispatchable_issues(cfg: Config) -> list[tuple[str, int, bool]]:
    """Get open issues with assigned:worker that aren't blocked/stuck.

    Returns list of (repo, issue_num, is_vector) tuples.
    """
    jq_filter = (
        '[.[] | select(.labels | map(.name) | all(test("^blocker:|^stuck$") | not))]'
        r' | .[] | "\(.number)\t\([.labels[].name] | any(test("component:vector")))"'
    )
    results: list[tuple[str, int, bool]] = []

    output = gh.issue_list(
        cfg.nuc_repo,
        label=cfg.dispatch_label,
        fields="number,labels",
        jq=jq_filter,
    )
    if output and isinstance(output, str):
        for line in output.splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[0].isdigit():
                is_vector = parts[1].strip() == "true"
                results.append((cfg.nuc_repo, int(parts[0]), is_vector))
    return results


def build_worker_prompt(cfg: Config, repo: str, issue_num: int,
                        issue_body: str, issue_comments: str) -> str:
    """Build the full worker prompt with role definition, lessons, and context."""
    # Read issue-worker.md (single source of truth)
    md_file = cfg.repo_dir / ".claude/agents/issue-worker.md"
    md_content = md_file.read_text() if md_file.exists() else (
        "You are a NUC Issue Worker for Project Vector. Read .claude/CLAUDE.md for context."
    )

    # Recent lessons
    lessons_file = cfg.repo_dir / "docs/lessons-learned.jsonl"
    recent_lessons = ""
    if lessons_file.exists():
        lines = lessons_file.read_text().splitlines()
        recent_lessons = "\n".join(lines[-10:])

    # PR review warnings (recurring patterns)
    review_warnings = _get_review_warnings(cfg)

    # Recent context (sprint siblings, closed issues, git log)
    recent_context = _get_recent_context(cfg, repo, issue_num)

    prompt = f"""=== YOUR ROLE DEFINITION (from issue-worker.md) ===
{md_content}
=== END ROLE DEFINITION ===

=== RECENT LESSONS (from docs/lessons-learned.jsonl — apply relevant ones) ===
{recent_lessons or "No lessons recorded yet."}
=== END LESSONS ===
"""
    if review_warnings:
        prompt += f"""
=== PR REVIEW WARNINGS (recurring rejections — avoid these) ===
{review_warnings}
=== END WARNINGS ===
"""
    if recent_context:
        prompt += f"""
=== RECENT CONTEXT (sibling issues + recent activity — be aware of overlapping work) ===
{recent_context}
=== END CONTEXT ===
"""

    # Add Vector context for Vector issues (detected by component:vector label)
    is_vector = _is_vector_issue(repo, issue_num)
    if is_vector:
        prompt += """
=== VECTOR WORKER CONTEXT ===
You are working on **Vector robot code** located at `apps/vector/` in this monorepo.
- Git operations (branch, commit, push, PR) happen in this worktree (the monorepo).
- Vector bridge/inference code is at `apps/vector/` — make your changes there.
- Vector tests are at `tests/vector/`.
- Vector communicates via gRPC over WiFi — all inference runs on NUC, Vector is a thin client.
- wire-pod on NUC replaces Anki cloud services.

**VECTOR CONNECTION (ACTIVE — verified 2026-03-11):**
- Name: Vector-D2C9, ESN: 0dd1cdcf, IP: 192.168.1.73
- SSH: `ssh -i ~/.ssh/id_rsa_Vector-D2C9 -o PubkeyAcceptedAlgorithms=+ssh-rsa -o HostKeyAlgorithms=+ssh-rsa root@192.168.1.73` (or `ssh vector` alias)
- SDK: `wirepod-vector-sdk` 0.8.1 (imports as `import anki_vector`)
- SDK config: `~/.anki_vector/sdk_config.ini` (serial=0dd1cdcf, cert, guid all configured)
- wire-pod: running on NUC (localhost:8080 HTTP, localhost:443 TLS)
- Vector's server_config points to NUC (192.168.1.62:443)

**QUICK-START — Connect to Vector from Python:**
```python
import anki_vector
robot = anki_vector.Robot(serial="0dd1cdcf", default_logging=False)
robot.connect()
# Use robot.behavior, robot.motors, robot.camera, etc.
robot.behavior.say_text("Hello!")
batt = robot.get_battery_state()
robot.disconnect()
```

**IMPORTANT: Up to 2 Vector workers may run in parallel (max_vector_workers=2).
Another worker may be using the robot concurrently — use the centralized ControlManager singleton
(`from apps.vector.src.control_manager import get_control_manager; ctrl = get_control_manager(); ctrl.acquire("worker")`)
before movement commands and `ctrl.release("worker")` after. NEVER call `robot.conn.request_control()` directly.
Do NOT leave robot.connect() open when done — always disconnect.**

**ARCHITECTURE:**
- No Docker on Vector — too resource-constrained (Snapdragon 212)
- No ROS2 — gRPC replaces ROS2 topics
- All ML inference (YOLO, face recognition, STT, TTS) runs on NUC
- Camera frames stream from Vector → NUC for processing
- Motor commands stream from NUC → Vector via gRPC

**KEY APIs (Vector SDK — `robot.*`):**
- Say text: `robot.behavior.say_text("text")`
- Drive: `robot.motors.set_wheel_motors(left, right)`, `robot.behavior.drive_straight(distance_mm(N), speed_mmps(N))`
- Turn: `robot.behavior.turn_in_place(degrees(N))`
- Head: `robot.behavior.set_head_angle(degrees(N))` — range -22° to 45°
- Lift: `robot.behavior.set_lift_height(0.0–1.0)`
- LEDs: `robot.behavior.set_backpack_lights(...)` — see SDK docs
- Camera: `robot.camera.capture_single_image()` or `robot.camera.init_camera_feed()`
- Audio: `robot.audio.stream_wav_file(path)`
- Display: `robot.screen.set_screen_with_image_data(image_bytes, duration_sec)`
- Battery: `robot.get_battery_state()` → voltage, level, charging
- Events: `robot.events.subscribe(event_type, callback)`

**KEY APIs (raw gRPC — for advanced/low-level use):**
- Camera: `CameraFeed` stream (640x360)
- Motors: `DriveWheels(left, right, accel)`, `DriveStraight(dist, speed)`, `TurnInPlace(angle, speed)`
- Head: `SetHeadAngle(angle_deg, speed_dps)`
- Lift: `SetLiftHeight(height_mm, speed_mmps)`
- LEDs: `SetBackpackLights(front, middle, back)` — RGBA per segment
- Audio: `PlayAudio(wav_bytes)`, `AudioFeed` (raw gRPC for mic)
- Display: `DisplayImage(image_bytes)` — 160x80 OLED (SDK sends 184x96; vic-engine converts stride)
- Sensors: `BatteryState`, `RobotState` (accel, gyro, cliff, touch)

**RULES:**
- Vector is differential drive (tank treads) — NO strafing. Use turn-then-drive.
- No LiDAR — use camera-based obstacle detection or cliff sensors only.
- Always `robot.disconnect()` when done — other workers may need the robot.
- Full setup guide: `docs/vector/setup-guide.md`
=== END VECTOR CONTEXT ===
"""

    prompt += f"""
You are working on GitHub Issue {repo}#{issue_num}.
For all gh issue/pr commands, use: -R {repo} and issue number {issue_num}

ISSUE-SPECIFIC COMMANDS (use these exact commands — issue number pre-filled):
- Comment: gh issue comment {issue_num} -R {repo} -b 'YOUR MESSAGE'
- Create branch: git fetch origin && git checkout -b experiment/issue-{issue_num} origin/main
- BEFORE pushing: git fetch origin && git rebase origin/main (resolve any conflicts)
- Open PR: gh pr create -R {repo} --title 'Issue #{issue_num}: <summary>' --body 'Relates to #{issue_num}'
- Re-read issue: gh issue view {issue_num} -R {repo} --json comments --jq '.comments[-3:][] | "\\(.author.login): \\(.body | split("\\n")[0])"'

Issue description:
{issue_body}

Comments on this issue (READ THESE FIRST):
{issue_comments}

General rules:
1. Read ALL comments above before starting any work
2. Work autonomously — do not ask for confirmation
3. **COMMENT HEADER (MANDATORY):** EVERY comment MUST start with "## Worker: <Phase> — <title>"
4. **COMMENT RATE LIMIT:** Before posting, check the last 3 comments. If they are ALL from Worker and within the last hour, do NOT post — you are likely in a loop.
5. **PHYSICAL TESTS:** If the issue involves physical hardware movement:
   a. First, write and run SOFTWARE verification tests (e.g., capture frames, check servo angles via logs, verify image changes after servo commands).
   b. ONLY if software verification PASSES, then post a Physical Test Request, add blocker:needs-human, and STOP.
   c. If software verification FAILS, fix the code and re-test. Do NOT request physical testing until software tests confirm the fix works.
   d. If Ophir reported a FAIL on a previous physical test, you MUST add new software verification that catches the reported failure BEFORE requesting another physical test.
   e. **CRITICAL — DO NOT RE-ADD blocker:needs-human** if the comments show a previous Physical Test Request that FAILED and you have NOT made meaningful code changes (new commits) that fix the failure. If you have nothing new to fix, do NOT post another Physical Test Request — that creates a stale loop. Instead, analyze what went wrong, write code to fix it, push new commits, THEN run SW verification, and ONLY THEN post a new Physical Test Request.
   f. **AUDIT TRAIL:** When you add blocker:needs-human, you MUST include in your Physical Test Request comment: (1) what new commits you pushed, (2) what SW tests passed, (3) why this test will succeed when the previous one failed. If you cannot list new commits, DO NOT add the label.
   g. **INTERACTIVE TEST FORMAT:** Physical tests are run interactively via Signal — the system sends each step to Ophir one at a time and waits for confirmation. Your Physical Test Request MUST include these structured fields:
      - **Setup command:** (bash command to start services)
      - **What to observe:** (what Ophir should watch for)
      - **Pass criteria:** (what success looks like)
      - **Fail criteria:** (what failure looks like)
6. **SAFETY — NO MOTOR MOVEMENT DURING SOFTWARE TESTS:** LiDAR obstacle avoidance is NOT installed yet. During software verification, you may start containers and test servos/camera, but you MUST NOT enable motor/wheel movement. Software tests should verify camera tracking, servo angles, and node health — NOT driving. If you need the full pipeline running, disable the planner node or set motor speeds to 0. Only Ophir can authorize motor movement during physical tests.
"""
    return prompt


def _get_review_warnings(cfg: Config) -> str:
    """Get recurring PR review rejection patterns."""
    review_file = cfg.state_dir / "review-patterns.jsonl"
    if not review_file.exists():
        return ""
    patterns: Counter[str] = Counter()
    for line in review_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("verdict") == "REJECTED":
                patterns[d.get("pattern", "")] += 1
        except json.JSONDecodeError:
            continue
    recurring = [(p, c) for p, c in patterns.items() if c >= 3 and p]
    return "\n".join(f"- RECURRING REJECTION ({c}x): {p}" for p, c in recurring[:5])


def _get_recent_context(cfg: Config, repo: str, issue_num: int) -> str:
    """Gather sprint siblings, recently closed issues, and git log."""
    context_parts = []

    # Sprint label
    sprint_label = gh.issue_view(
        repo, issue_num, fields="labels",
        jq='[.labels[].name | select(startswith("sprint-"))] | first // empty',
    )

    if sprint_label:
        siblings = gh.issue_list(
            repo, label=sprint_label, state="all",
            fields="number,title,state",
            jq=f'.[] | select(.number != {issue_num}) | "#\\(.number) [\\(.state)] \\(.title)"',
        )
        if siblings:
            context_parts.append(f"Sprint siblings ({sprint_label}):\n{siblings}")

    # Recently closed
    recent_closed = gh.issue_list(
        repo, state="closed",
        fields="number,title,closedAt", limit=10,
        jq=f'.[] | select(.number != {issue_num}) | "#\\(.number) \\(.title)"',
    )
    if recent_closed:
        context_parts.append(f"Recently closed issues (may have touched related code):\n{recent_closed}")

    # Recent git log — always use the monorepo dir
    git_repo_dir = cfg.repo_dir
    try:
        result = subprocess.run(
            ["git", "-C", str(git_repo_dir), "log", "--oneline", "-15", "origin/main"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            context_parts.append(
                f"Recent commits on main (check for overlapping changes):\n{result.stdout.strip()}"
            )
    except (subprocess.TimeoutExpired, OSError):
        pass

    return "\n\n".join(context_parts)


def get_issue_comments_summarized(cfg: Config, repo: str, issue_num: int) -> str:
    """Get issue comments via smart summarization."""
    summarizer = cfg.repo_dir / "scripts/summarize-comments.py"
    try:
        comments_json = gh.gh(
            "issue", "view", str(issue_num), "-R", repo, "--json", "comments"
        )
        if not comments_json:
            return ""
        result = subprocess.run(
            ["python3", str(summarizer)],
            input=comments_json, capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def work_on_issue(cfg: Config, repo: str, issue_num: int) -> int:
    """Run the full worker lifecycle for an issue. Returns 0/1/2 (success/fail/quota)."""
    issue_key = f"{repo}#{issue_num}"
    issue_title = gh.issue_view(repo, issue_num, fields="title", jq=".title") or "unknown"
    log.info("Working on %s: %s", issue_key, issue_title)

    # Snapshot labels before
    labels_before = gh.issue_view(
        repo, issue_num, fields="labels",
        jq='[.labels[].name] | join(",")',
    )

    # Get issue body and comments
    issue_body = gh.issue_view(repo, issue_num, fields="body", jq=".body")
    issue_comments = get_issue_comments_summarized(cfg, repo, issue_num)

    prompt = build_worker_prompt(cfg, repo, issue_num, issue_body, issue_comments)

    # All worktrees come from the single monorepo
    is_vector = _is_vector_issue(repo, issue_num)
    source_repo_dir = cfg.repo_dir

    # Create worktree for isolation — prefix with repo name to avoid collisions
    repo_prefix = "vector" if is_vector else "nuc"
    worktree_dir = Path(f"/tmp/{repo_prefix}-worker-issue-{issue_num}")
    worktree_branch = f"worktree/{repo_prefix}-issue-{issue_num}"

    _cleanup_worktree(source_repo_dir, worktree_dir, worktree_branch)

    try:
        subprocess.run(
            ["git", "-C", str(source_repo_dir), "worktree", "add",
             str(worktree_dir), "-b", worktree_branch, "main"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.error("Failed to create worktree for %s", issue_key)
        return 1

    if not worktree_dir.exists():
        log.error("Failed to create worktree for %s", issue_key)
        return 1

    log.info("Created worktree at %s for %s", worktree_dir, issue_key)

    # Run LLM
    output, exit_code = run_llm(
        cfg, "heavy", prompt,
        cwd=worktree_dir,
        timeout=cfg.issue_timeout,
        agent_role="worker",
        issue_key=issue_key,
    )

    # Cleanup worktree
    _cleanup_worktree(source_repo_dir, worktree_dir, worktree_branch)

    if exit_code != 0:
        if check_quota_exhausted(output, cfg):
            return 2
        log.warning("LLM exited with code %d for %s", exit_code, issue_key)
        gh.issue_comment(repo, issue_num,
                         f"Worker session failed (exit code: {exit_code}). Will retry next cycle.")
        return 1

    # Audit: check if Worker added blocker:needs-human
    _audit_blocker_label(cfg, repo, issue_num, labels_before)

    # PR Review Hook
    run_pr_review_hook(cfg, repo, issue_num)

    # Hardware Sanity Test (Vector PRs only)
    # TODO: Implement Vector gRPC health check for PRs
    # if is_vector:
    #     _run_vector_sanity_for_pr(cfg, repo, issue_num, source_repo_dir)

    # Merge Gate
    merge_if_approved(cfg, repo, issue_num)

    log.info("Completed %s", issue_key)
    return 0


def _cleanup_worktree(repo_dir: Path, worktree_dir: Path, worktree_branch: str) -> None:
    """Clean up a git worktree."""
    if worktree_dir.exists():
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "remove",
             str(worktree_dir), "--force"],
            capture_output=True, text=True, timeout=10,
        )
        if worktree_dir.exists():
            import shutil
            shutil.rmtree(worktree_dir, ignore_errors=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch", "-D", worktree_branch],
        capture_output=True, text=True, timeout=10,
    )


def _audit_blocker_label(cfg: Config, repo: str, issue_num: int, labels_before: str) -> None:
    """Audit blocker:needs-human label changes after worker runs."""
    labels_after = gh.issue_view(
        repo, issue_num, fields="labels", jq='[.labels[].name] | join(",")',
    )
    if "blocker:needs-human" not in labels_after or "blocker:needs-human" in labels_before:
        return

    log.info("AUDIT: Worker added blocker:needs-human on %s#%d", repo, issue_num)
    last_comment = gh.issue_view(
        repo, issue_num, fields="comments", jq=".comments[-1].body",
    )

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Strict check: must have a proper Physical Test Request (not just mention it)
    # AND must require watching the robot physically move
    has_ptr = (last_comment
               and "## Worker: Physical Test Request" in last_comment
               or last_comment
               and "## Updated Physical Test Request" in last_comment)
    has_observe = (last_comment
                   and "What to observe" in last_comment)

    if has_ptr and has_observe:
        gh.issue_comment(repo, issue_num,
                         f"## 🤖 Orchestrator: blocker:needs-human added\n\n"
                         f"**Source:** Issue Worker (post-Phase 4 SW verification)\n"
                         f"**Process:** work_on_issue → Worker added label after posting Physical Test Request\n"
                         f"**Time:** {now_utc}\n\n"
                         "To remove: Ophir reports PASS/FAIL via Signal or GitHub comment.")
    else:
        reason = "no Physical Test Request comment" if not has_ptr else "missing observation criteria"
        log.warning("blocker:needs-human REJECTED on %s#%d: %s", repo, issue_num, reason)
        gh.issue_edit_labels(repo, issue_num, remove=["blocker:needs-human"])
        gh.issue_comment(repo, issue_num,
                         f"## 🤖 Orchestrator: blocker:needs-human REJECTED\n\n"
                         f"**Reason:** {reason}\n"
                         f"**Action:** Label removed automatically\n"
                         f"**Time:** {now_utc}\n\n"
                         "blocker:needs-human requires a proper Physical Test Request with:\n"
                         "- `## Worker: Physical Test Request` header\n"
                         "- `What to observe:` section (Ophir must physically WATCH the robot MOVE)\n\n"
                         "If this is a software-only test (API, SSH, container, sensor data), "
                         "run it yourself — don't wait for Ophir.")


def _run_hw_sanity_for_pr(cfg: Config, repo: str, issue_num: int, source_repo_dir: Path) -> None:
    """Run hardware sanity tests for a Vector PR. Non-blocking — failures are posted but don't prevent merge."""
    pr_number = gh.find_pr_for_issue(repo, issue_num)
    if not pr_number:
        return

    # Check if this is a docs-only PR (skip sanity for .md, .txt, etc.)
    diff = gh.pr_diff(repo, pr_number)
    if diff:
        changed_files = set()
        for line in diff.split("\n"):
            if line.startswith("+++ b/") or line.startswith("--- a/"):
                fname = line.split("/", 1)[-1] if "/" in line else ""
                if fname:
                    changed_files.add(fname)
        code_files = [f for f in changed_files if not f.endswith((".md", ".txt", ".yml", ".yaml", ".json"))]
        if not code_files:
            log.info("HW sanity: skipping docs-only PR #%d", pr_number)
            return

    # Get the PR branch name
    pr_branch = gh.pr_view(repo, pr_number, fields="headRefName", jq=".headRefName")
    if not pr_branch:
        log.warning("HW sanity: could not determine PR branch for #%d", pr_number)
        return

    # Create a temporary checkout of the PR branch
    sanity_dir = Path(f"/tmp/hw-sanity-{issue_num}")
    if sanity_dir.exists():
        import shutil
        shutil.rmtree(sanity_dir, ignore_errors=True)

    try:
        subprocess.run(
            ["git", "-C", str(source_repo_dir), "fetch", "origin", pr_branch.strip()],
            capture_output=True, text=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(source_repo_dir), "worktree", "add",
             str(sanity_dir), f"origin/{pr_branch.strip()}"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.warning("HW sanity: failed to create temp checkout for PR #%d", pr_number)
        return

    if not sanity_dir.exists():
        log.warning("HW sanity: temp checkout dir not created")
        return

    log.info("Running HW sanity test for %s#%d (PR #%d)", repo, issue_num, pr_number)

    try:
        sanity_script = cfg.repo_dir / "scripts" / "run-hw-sanity.sh"
        result = subprocess.run(
            ["bash", str(sanity_script),
             "--worktree", str(sanity_dir),
             "--repo", repo,
             "--pr", str(pr_number)],
            capture_output=True, text=True, timeout=900,  # 15 min max
        )
        if result.returncode != 0:
            log.warning("HW sanity: critical failures for PR #%d (exit %d)",
                        pr_number, result.returncode)
        else:
            log.info("HW sanity: all critical tests passed for PR #%d", pr_number)
    except subprocess.TimeoutExpired:
        log.warning("HW sanity: timed out for PR #%d", pr_number)
        gh.pr_comment(repo, pr_number,
                      "## Hardware Sanity Check\n\n**Result:** TIMEOUT (15 min)")
    except OSError as e:
        log.warning("HW sanity: failed to run for PR #%d: %s", pr_number, e)
    finally:
        # Cleanup temp worktree
        subprocess.run(
            ["git", "-C", str(source_repo_dir), "worktree", "remove",
             str(sanity_dir), "--force"],
            capture_output=True, text=True, timeout=10,
        )
        if sanity_dir.exists():
            import shutil
            shutil.rmtree(sanity_dir, ignore_errors=True)


def _auto_rebase_pr(repo_dir: Path, pr_branch: str) -> bool:
    """Attempt to auto-rebase a PR branch onto main. Returns True on success.

    Uses a temporary worktree to avoid interfering with the main repo checkout,
    which is important since multiple merge gates can run in parallel threads.
    """
    import shutil

    rebase_dir = Path(f"/tmp/auto-rebase-{pr_branch.replace('/', '-')}")
    if rebase_dir.exists():
        shutil.rmtree(rebase_dir, ignore_errors=True)

    try:
        # Fetch the PR branch
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin", pr_branch],
            capture_output=True, text=True, timeout=30,
        )

        # Create a temporary worktree on the PR branch
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add",
             str(rebase_dir), f"origin/{pr_branch}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.warning("Auto-rebase: worktree creation failed: %s", result.stderr.strip())
            return False

        # Rebase onto origin/main inside the worktree
        result = subprocess.run(
            ["git", "-C", str(rebase_dir), "rebase", "origin/main"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("Auto-rebase: rebase failed (conflicts?): %s", result.stderr.strip())
            subprocess.run(
                ["git", "-C", str(rebase_dir), "rebase", "--abort"],
                capture_output=True, text=True, timeout=10,
            )
            return False

        # Force-push the rebased branch
        result = subprocess.run(
            ["git", "-C", str(rebase_dir), "push", "origin", "HEAD:" + pr_branch,
             "--force-with-lease"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("Auto-rebase: push failed: %s", result.stderr.strip())
            return False

        return True
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Auto-rebase failed: %s", e)
        return False
    finally:
        # Always clean up the temporary worktree
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "remove",
             str(rebase_dir), "--force"],
            capture_output=True, text=True, timeout=10,
        )
        if rebase_dir.exists():
            shutil.rmtree(rebase_dir, ignore_errors=True)


def _check_pr_up_to_date(cfg: Config, repo: str, pr_number: int, issue_num: int) -> None:
    """Check if PR branch is rebased on latest main. If not, comment and let the worker know."""
    source_repo_dir = cfg.repo_dir

    pr_branch = gh.pr_view(repo, pr_number, fields="headRefName", jq=".headRefName")
    if not pr_branch:
        return

    pr_branch = pr_branch.strip()

    try:
        # Fetch latest
        subprocess.run(
            ["git", "-C", str(source_repo_dir), "fetch", "origin"],
            capture_output=True, text=True, timeout=30,
        )
        # Check if main is an ancestor of the PR branch
        result = subprocess.run(
            ["git", "-C", str(source_repo_dir), "merge-base", "--is-ancestor",
             "origin/main", f"origin/{pr_branch}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # PR is behind main — post a comment
            log.warning("PR #%d is not up-to-date with main", pr_number)
            # Check if we already posted this warning recently
            last_comments = gh.pr_view(
                repo, pr_number, fields="comments",
                jq='[.comments[] | select(.body | test("needs rebase"))] | length',
            )
            if last_comments and last_comments.strip() not in ("", "0"):
                return  # Already warned
            gh.pr_comment(repo, pr_number,
                          "## PR Review: Rebase Required\n\n"
                          "This PR is behind `main`. Please rebase before merge:\n"
                          "```\ngit fetch origin && git rebase origin/main\n```\n"
                          "The merge gate will not merge until the PR includes the latest main.")
    except (subprocess.TimeoutExpired, OSError):
        pass  # Non-fatal — skip check if git fails


def _deploy_vector_after_merge(cfg: Config, repo: str, pr_number: int) -> None:
    """After merging a Vector PR, restart Vector bridge services on NUC."""
    log.info("Post-merge: restarting Vector services for PR #%d", pr_number)

    def _post_deploy_comment(success: bool, detail: str) -> None:
        icon = "✅" if success else "❌"
        gh.pr_comment(repo, pr_number,
                      f"{icon} **Post-merge Vector deploy**: {detail}")

    try:
        # Pull latest main locally (monorepo)
        subprocess.run(
            ["git", "-C", str(cfg.repo_dir), "checkout", "main"],
            capture_output=True, text=True, timeout=15,
        )
        subprocess.run(
            ["git", "-C", str(cfg.repo_dir), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Post-merge: failed to pull latest main locally")
        _post_deploy_comment(False, f"git pull failed: {e}")
        return

    # Vector services run on NUC — restart the bridge/inference processes
    # TODO: Implement Vector service restart (systemd units for bridge, inference pipeline)
    _post_deploy_comment(True, "code pulled — restart Vector services manually if needed")


def run_pr_review_hook(cfg: Config, repo: str, issue_num: int) -> None:
    """Run independent Haiku PR review on the diff."""
    pr_number = gh.find_pr_for_issue(repo, issue_num)
    if not pr_number:
        return

    # Skip if last review hook comment is APPROVED (allows re-review after REJECTED)
    last_review = gh.pr_view(
        repo, pr_number, fields="comments",
        jq='[.comments[] | select(.body | test("PR Review Hook"))] | last | .body',
    )
    if last_review and "APPROVED:" in last_review:
        log.info("PR review hook already APPROVED %s#%d — skipping", repo, pr_number)
        return

    log.info("Running PR review hook on %s#%d (issue #%d)", repo, pr_number, issue_num)

    diff = gh.pr_diff(repo, pr_number)
    if not diff:
        return

    diff_lines = diff.count("\n") + 1
    truncated_diff = "\n".join(diff.split("\n")[:1000])
    if diff_lines > 1000:
        truncation_note = (
            f"NOTE: This diff was truncated from {diff_lines} to 1000 lines. "
            "Do NOT reject code solely because it appears to end abruptly — "
            "the file is complete in the actual PR."
        )
    else:
        truncation_note = (
            f"NOTE: This is the COMPLETE diff ({diff_lines} lines, not truncated). "
            "All functions and files shown are complete. "
            "Do NOT claim code is truncated or incomplete."
        )

    # Gate 0: Check PR is up-to-date with main
    _check_pr_up_to_date(cfg, repo, pr_number, issue_num)

    review_prompt = f"""You are an independent security and quality reviewer for Project Vector.
Review this PR diff. You have NO context about why decisions were made — review purely on merit.

{truncation_note}

CHECK FOR:
1. Security: sudo, secrets, eval/exec, unauthorized URLs, .md file modifications
2. Quality: dead code, silent error swallowing, hardcoded values, commented-out code
3. Correctness: obvious bugs, race conditions, missing error handling
4. **Branch freshness**: If you see merge conflicts or signs the code doesn't account for recent main changes, REJECT

PR DIFF:
{truncated_diff}

RESPOND with EXACTLY one of these formats (no other text):
APPROVED: <one line summary>
REJECTED: <specific issues found>"""

    review_output, _ = run_llm(
        cfg, "light", review_prompt,
        timeout=120,
        agent_role="review-hook",
        issue_key=f"{repo}#{issue_num}",
    )

    if not review_output:
        log.info("PR review hook LLM returned empty — auto-approving (CI is the real gate)")
        gh.pr_comment(repo, pr_number,
                      "## PR Review Hook (automated)\n\n"
                      "APPROVED: LLM review unavailable — auto-approved. "
                      "CI checks are the primary quality gate.")
        today = datetime.now().strftime("%Y-%m-%d")
        review_entry = json.dumps({
            "date": today, "pr": pr_number, "issue": issue_num,
            "verdict": "APPROVED", "pattern": "auto-approved (LLM empty)",
        })
        review_file = cfg.state_dir / "review-patterns.jsonl"
        with open(review_file, "a") as f:
            f.write(review_entry + "\n")
        return

    # Extract verdict (take LAST matching line — codex echoes the prompt)
    verdict = ""
    pattern = ""
    for line in review_output.split("\n"):
        line = line.strip()
        if line.startswith("APPROVED:"):
            verdict = "APPROVED"
            pattern = line[len("APPROVED: "):]
        elif line.startswith("REJECTED:"):
            verdict = "REJECTED"
            pattern = line[len("REJECTED: "):]

    verdict_line = f"{verdict}: {pattern}" if verdict else "Unable to determine verdict"
    log.info("PR review hook result: %s", verdict_line)

    gh.pr_comment(repo, pr_number,
                  f"## PR Review Hook (automated)\n\n{verdict_line}")

    if verdict:
        today = datetime.now().strftime("%Y-%m-%d")
        review_entry = json.dumps({
            "date": today,
            "pr": pr_number,
            "issue": issue_num,
            "verdict": verdict,
            "pattern": pattern,
        })
        review_file = cfg.state_dir / "review-patterns.jsonl"
        with open(review_file, "a") as f:
            f.write(review_entry + "\n")

    # On REJECTED: send back to worker for a fix attempt first.
    # Only escalate to Ophir after 4+ rejections on the same issue.
    if verdict == "REJECTED":
        current_labels = gh.issue_view(
            repo, issue_num, fields="labels", jq='[.labels[].name] | join(",")',
        ) or ""
        if "human-approved" in current_labels:
            pass  # Already approved by human, skip
        else:
            # Count prior rejections for this issue
            rejection_count = 0
            review_file = cfg.state_dir / "review-patterns.jsonl"
            if review_file.exists():
                for line in review_file.read_text().splitlines():
                    try:
                        entry = json.loads(line)
                        if entry.get("issue") == issue_num and entry.get("verdict") == "REJECTED":
                            rejection_count += 1
                    except json.JSONDecodeError:
                        continue

            if rejection_count >= 4:
                # Multiple rejections — escalate to human
                gh.issue_edit_labels(repo, issue_num, add=["blocker:needs-human"])
                gh.issue_comment(
                    repo, issue_num,
                    f"## 🤖 Agent Loop: PR #{pr_number} needs manual approval\n\n"
                    f"**Review hook rejected {rejection_count} times.** Latest: {pattern}\n\n"
                    "Reply `approve {issue_num}` on Signal to override and merge, "
                    "or comment on the PR with guidance for the worker.",
                )
                log.info("Review hook rejected PR #%d %d times — escalated to Ophir", pr_number, rejection_count)
            else:
                # First rejection — send back to worker to fix
                gh.issue_comment(
                    repo, issue_num,
                    f"## 🤖 Agent Loop: PR #{pr_number} review rejected — fix needed\n\n"
                    f"**Review hook found:** {pattern}\n\n"
                    "Worker will be re-dispatched to address this feedback.",
                )
                log.info("Review hook rejected PR #%d (attempt %d) — returning to worker", pr_number, rejection_count + 1)

    log.info("PR review hook completed for %s#%d", repo, pr_number)


def merge_if_approved(cfg: Config, repo: str, issue_num: int) -> bool:
    """Merge Gate — merge only after hook approval + CI + change classification."""
    pr_number = gh.find_pr_for_issue(repo, issue_num)
    if not pr_number:
        return False

    log.info("Merge gate: checking %s#%d (issue #%d)", repo, pr_number, issue_num)

    # Gate 1: PR Review Hook verdict (human-approved overrides rejection)
    current_labels = gh.issue_view(
        repo, issue_num, fields="labels", jq='[.labels[].name] | join(",")',
    ) or ""
    human_approved = "human-approved" in current_labels

    last_review = gh.pr_view(
        repo, pr_number, fields="comments",
        jq='[.comments[] | select(.body | test("PR Review Hook"))] | last | .body',
    )
    if not last_review:
        log.info("Merge gate: no review hook comment found — skipping")
        return False
    if "REJECTED:" in last_review:
        if human_approved:
            log.info("Merge gate: review hook REJECTED but human-approved — overriding")
        else:
            log.info("Merge gate: REJECTED by review hook — needs human approval")
            return False
    elif "APPROVED:" not in last_review:
        log.info("Merge gate: review hook verdict unclear — skipping")
        return False

    # Gate 2: PR must be up-to-date with main (auto-rebase if behind)
    is_vector = _is_vector_issue(repo, issue_num)
    source_repo_dir = cfg.repo_dir
    pr_branch = gh.pr_view(repo, pr_number, fields="headRefName", jq=".headRefName")
    if pr_branch:
        pr_branch = pr_branch.strip()
        try:
            subprocess.run(
                ["git", "-C", str(source_repo_dir), "fetch", "origin"],
                capture_output=True, text=True, timeout=30,
            )
            result = subprocess.run(
                ["git", "-C", str(source_repo_dir), "merge-base", "--is-ancestor",
                 "origin/main", f"origin/{pr_branch}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                log.info("Merge gate: PR #%d is behind main — attempting auto-rebase", pr_number)
                rebased = _auto_rebase_pr(source_repo_dir, pr_branch)
                if not rebased:
                    log.info("Merge gate: auto-rebase failed for PR #%d — needs manual rebase", pr_number)
                    return False
                log.info("Merge gate: auto-rebase succeeded for PR #%d", pr_number)
        except (subprocess.TimeoutExpired, OSError):
            pass  # Non-fatal

    # Gate 3: CI status
    ci_status = gh.pr_checks(repo, pr_number)
    if ci_status:
        if "fail" in ci_status.lower():
            # Check if ALL failures are billing/infrastructure issues
            if gh.pr_checks_billing_failure(repo, pr_number):
                log.info("Merge gate: CI failures are billing-related — skipping CI gate")
            else:
                log.info("Merge gate: CI failing — waiting")
                return False
        if "pending" in ci_status.lower():
            log.info("Merge gate: CI pending — waiting")
            return False

    # Gate 4: Human approval check
    current_labels = gh.issue_view(
        repo, issue_num, fields="labels", jq='[.labels[].name] | join(",")',
    )
    # classify_changes always returns "auto" — trust PR Review Hook + CI
    if "human-approved" not in current_labels:
        pass  # auto classification always passes

    # All gates passed — merge
    log.info("Merge gate: all clear — merging %s#%d", repo, pr_number)
    merged = gh.pr_merge(repo, pr_number)
    if not merged:
        # gh pr merge can return non-zero even when the merge succeeds (race condition).
        # Verify actual PR state before giving up.
        pr_state = gh.pr_view(repo, pr_number, fields="state", jq=".state")
        if pr_state and pr_state.strip() == "MERGED":
            log.info("Merge gate: gh pr merge reported failure but PR #%d is actually MERGED", pr_number)
            merged = True
        else:
            log.info("Merge gate: merge command failed for PR #%d (state: %s)", pr_number, pr_state)
            return False

    # Clean up merged PR branch(es) from remote
    _cleanup_merged_branches(cfg, repo, issue_num)

    gh.issue_close(repo, issue_num)
    # Reset PGM signal gate entries
    gate_file = cfg.state_dir / "pgm-signal-sent.tsv"
    delete_tsv_entries(gate_file, rf"-{issue_num}\t")
    # Move board item to Done + close source conversation issue
    _move_board_item_done(cfg, issue_num, repo)
    log.info("Merge gate: merged PR #%d and closed issue #%d", pr_number, issue_num)

    # Post-merge: restart Vector services if applicable
    if is_vector:
        _deploy_vector_after_merge(cfg, repo, pr_number)

    # Post-merge: check if documentation needs updating
    _post_merge_doc_check(cfg, repo, pr_number, issue_num)

    return True


def _cleanup_merged_branches(cfg: Config, repo: str, issue_num: int) -> None:
    """Delete remote experiment/worktree branches for a merged issue."""
    try:
        result = subprocess.run(
            ["git", "branch", "-r"],
            capture_output=True, text=True, timeout=10,
            cwd=str(cfg.repo_dir),
        )
        for line in result.stdout.splitlines():
            branch = line.strip().replace("origin/", "")
            if f"issue-{issue_num}" not in branch:
                continue
            if not branch.startswith(("experiment/", "worktree/")):
                continue
            subprocess.run(
                ["git", "push", "origin", "--delete", branch],
                capture_output=True, text=True, timeout=15,
                cwd=str(cfg.repo_dir),
            )
            log.info("Cleaned up remote branch: %s", branch)
    except (subprocess.TimeoutExpired, OSError):
        pass  # Non-critical — branches can be cleaned later


def _post_merge_doc_check(cfg: Config, repo: str, pr_number: int, issue_num: int) -> None:
    """After merge, check if the PR changes behavior documented in MD files.

    Uses a light LLM call to compare the diff against key doc files.
    If inconsistencies are found, creates a doc-update issue.
    """
    diff = gh.pr_diff(repo, pr_number)
    if not diff:
        return

    # Skip tiny diffs (unlikely to affect docs)
    if diff.count("\n") < 10:
        return

    # Skip doc-only PRs — they can't introduce behavior drift, and checking
    # them creates recursive doc-update issues (see retro issue #60)
    changed_files = []
    for line in diff.split("\n"):
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            fname = line.split("/", 1)[-1] if "/" in line else ""
            if fname and fname != "/dev/null":
                changed_files.append(fname)
    code_files = [f for f in changed_files
                  if not f.endswith((".md", ".txt", ".yml", ".yaml", ".json", ".jsonl"))]
    if not code_files:
        log.info("Doc check: skipping doc-only PR #%d", pr_number)
        return

    # Skip if a doc-update issue was already created for this PR
    existing = gh.issue_list(
        repo, state="open",
        fields="number,title",
        jq=f'[.[] | select(.title | test("Doc update.*PR #{pr_number}"))] | length',
    )
    if existing and existing.strip() not in ("", "0"):
        return

    # Gather key doc snippets for comparison
    doc_snippets = []
    for doc_path in ["REPO_MAP.md", ".claude/CLAUDE.md"]:
        full_path = cfg.repo_dir / doc_path
        if full_path.exists():
            content = full_path.read_text()
            # Only include first 100 lines to keep prompt small
            snippet = "\n".join(content.splitlines()[:100])
            doc_snippets.append(f"--- {doc_path} (first 100 lines) ---\n{snippet}")

    # Include OpenClaw SKILL.md command reference for bridge/command drift detection
    skill_path = Path.home() / ".openclaw/workspace/skills/robot-control/SKILL.md"
    if skill_path.exists():
        skill_content = skill_path.read_text()
        # Extract just the Command Reference section (compact)
        snippet = "\n".join(skill_content.splitlines()[:50])
        doc_snippets.append(f"--- OpenClaw SKILL.md (first 50 lines) ---\n{snippet}")

    if not doc_snippets:
        return

    truncated_diff = "\n".join(diff.split("\n")[:500])
    doc_context = "\n\n".join(doc_snippets)

    check_prompt = f"""You are checking if a merged PR introduces behavior changes that make existing documentation inconsistent.

PR #{pr_number} DIFF (truncated):
{truncated_diff}

EXISTING DOCUMENTATION:
{doc_context}

Does this PR change any behavior, config, file paths, endpoints, commands, or architecture that is documented above?

Pay special attention to:
- New HTTP bridge endpoints in robot code (bridge.py, mqtt_bridge_node.py) → must be in SKILL.md
- New voice commands (command_router.py, audio_llm.py) → must be in SKILL.md
- New robot-commands.js command patterns → must be in SKILL.md
- Changed ports, URLs, or API contracts → must be updated everywhere

RESPOND with EXACTLY one line — pick the most specific match:
NO_UPDATE: <reason>
UPDATE_CLAUDE_MD: <what in .claude/CLAUDE.md specifically needs changing and why>
UPDATE_OTHER: <what non-CLAUDE.md docs need changing and why>

IMPORTANT: UPDATE_CLAUDE_MD is ONLY for changes to the file `.claude/CLAUDE.md` itself.
All other .md files (SKILL.md, REPO_MAP.md, docs/*.md, etc.) use UPDATE_OTHER."""

    output, _ = run_llm(
        cfg, "light", check_prompt,
        timeout=120,
        agent_role="doc-check",
        issue_key=f"{repo}#{issue_num}",
    )

    if not output:
        return

    # Extract verdict
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("UPDATE_CLAUDE_MD:"):
            detail = line[len("UPDATE_CLAUDE_MD:"):].strip()
            log.info("Doc check: CLAUDE.md update needed after PR #%d — %s", pr_number, detail)
            gh.gh(
                "issue", "create", "-R", repo,
                "--title", f"Doc update needed after PR #{pr_number}",
                "--label", "blocker:needs-human",
                "--body",
                f"PR #{pr_number} (issue #{issue_num}) merged changes that affect "
                f"**CLAUDE.md** (immutable — requires Ophir/Orchestrator).\n\n"
                f"**What needs updating:** {detail}\n\n"
                f"_Auto-generated by post-merge doc check._",
            )
            return
        if line.startswith("UPDATE_OTHER:"):
            detail = line[len("UPDATE_OTHER:"):].strip()
            log.info("Doc check: non-CLAUDE.md update needed after PR #%d — %s", pr_number, detail)
            gh.gh(
                "issue", "create", "-R", repo,
                "--title", f"Doc update needed after PR #{pr_number}",
                "--label", "assigned:worker",
                "--body",
                f"PR #{pr_number} (issue #{issue_num}) merged changes that may make "
                f"documentation inconsistent.\n\n"
                f"**What needs updating:** {detail}\n\n"
                f"**Action:** Update the relevant `.md` files (NOT CLAUDE.md).\n\n"
                f"_Auto-generated by post-merge doc check._",
            )
            return
        if line.startswith("NO_UPDATE:"):
            log.info("Doc check: no update needed after PR #%d", pr_number)
            return


def _move_board_item_done(cfg: Config, issue_num: int, repo: str) -> None:
    """Move board source item to Done when a board-dispatched worker issue closes."""
    body = gh.issue_view(repo, issue_num, fields="body", jq=".body")
    if not body:
        return

    # Parse source from body
    source_match = re.search(r"\*\*Source:\*\* ([^#]+)#(\d+)", body)
    if not source_match:
        return

    source_repo = source_match.group(1).strip()
    source_num = int(source_match.group(2))
    log.info("Board: worker issue #%d closed — moving %s#%d to Done",
             issue_num, source_repo, source_num)

    # Find board item ID
    board_data = gh.graphql(
        """query {
          user(login: "ophir-sw") {
            projectV2(number: 1) {
              items(first: 50) {
                nodes {
                  id
                  content {
                    ... on Issue { number repository { nameWithOwner } }
                  }
                }
              }
            }
          }
        }"""
    )
    if not board_data:
        return

    item_id = None
    try:
        for node in board_data["data"]["user"]["projectV2"]["items"]["nodes"]:
            content = node.get("content", {})
            if (content.get("number") == source_num and
                    content.get("repository", {}).get("nameWithOwner") == source_repo):
                item_id = node["id"]
                break
    except (KeyError, TypeError):
        return

    if not item_id:
        log.info("Board: source item not found on board")
        return

    gh.graphql(
        """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId,
            fieldId: $fieldId,
            value: { singleSelectOptionId: $optionId }
          }) { projectV2Item { id } }
        }""",
        projectId=cfg.board_project_id,
        itemId=item_id,
        fieldId=cfg.board_status_field_id,
        optionId=cfg.board_done_option,
    )
    log.info("Board: moved %s#%d to Done", source_repo, source_num)

    # Close the source conversation issue
    gh.issue_close(source_repo, source_num)
    log.info("Board: closed source issue %s#%d", source_repo, source_num)
