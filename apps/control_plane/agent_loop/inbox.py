"""Signal inbox processing — go, pass/fail, approve, board, coach, orchestrator."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

from . import github as gh
from .board import BoardManager
from .config import Config
from .llm import run_llm
from .signal_client import send_signal
from .state import (
    get_conversation_history,
    get_unreplied_messages,
    mark_inbox_replied,
    read_json_file,
    write_json_file,
    delete_tsv_entries,
)

log = logging.getLogger("agent-loop")


def is_physical_test_go(msg: str) -> bool:
    """Check if message is a physical test 'go' signal."""
    trimmed = msg.strip()
    return bool(re.match(r"^#?[Gg][Oo](\s+#?(\d+))?$", trimmed))


def parse_go_issue_number(msg: str) -> str:
    """Extract issue number from #go message."""
    trimmed = msg.strip()
    m = re.match(r"^#?[Gg][Oo]\s+#?(\d+)$", trimmed)
    return m.group(1) if m else ""


def parse_physical_test_result(msg: str) -> str:
    """Parse pass/fail result. Returns 'pass', 'fail', or ''."""
    lower = msg.lower().strip()
    if re.match(r"^(pass|passed|all pass|looks good|lgtm)", lower):
        return "pass"
    if re.match(r"^(fail|failed|not working|broken|nope|no good)", lower):
        return "fail"
    return ""


def parse_result_issue_number(msg: str) -> str:
    """Extract issue number from pass/fail message."""
    trimmed = msg.strip()
    m = re.match(r"^[A-Za-z]+\s+#?(\d+)", trimmed)
    return m.group(1) if m else ""


def find_pending_physical_test(cfg: Config, target_issue: str = "") -> dict | None:
    """Find a pending physical test (issue with blocker:needs-human + Physical Test Request)."""
    # Use inline logic
    for repo in [cfg.nuc_repo]:
        issues = gh.issue_list(repo, label="blocker:needs-human",
                               fields="number,title")
        for issue in issues:
            num = issue.get("number")
            if not num:
                continue
            if target_issue and str(num) != target_issue:
                continue

            comments_raw = gh.gh(
                "issue", "view", str(num), "-R", repo,
                "--json", "comments", "--jq", ".comments[].body",
            )
            if not comments_raw or "Physical Test Request" not in comments_raw:
                continue

            result = _parse_physical_test_fields(comments_raw)
            if result.get("setup_command") or result.get("observe"):
                return {
                    "issue_num": num,
                    "repo": repo,
                    "title": issue.get("title", ""),
                    **result,
                }
    return None


def _parse_physical_test_fields(comments: str) -> dict:
    """Parse Physical Test Request fields from comments."""
    setup = ""
    observe = ""
    pass_c = ""
    fail_c = ""

    lines = comments.split("\n")
    i = 0
    while i < len(lines):
        line_s = lines[i].strip()

        if line_s.startswith("**Setup command:**"):
            setup = line_s.replace("**Setup command:**", "").strip()
        elif line_s.startswith("**What to observe:**"):
            first_line = line_s.replace("**What to observe:**", "").strip()
            obs_lines = [first_line] if first_line else []
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("**Pass") or nxt.startswith("**Fail") or nxt.startswith("**Setup") or nxt.startswith("##"):
                    break
                if nxt:
                    obs_lines.append(nxt)
                j += 1
            observe = "\n".join(obs_lines)
            i = j - 1
        elif line_s.startswith("**Pass criteria:**"):
            pass_c = line_s.replace("**Pass criteria:**", "").strip()
        elif line_s.startswith("**Fail criteria:**"):
            fail_c = line_s.replace("**Fail criteria:**", "").strip()
        elif re.match(r"^#{1,4}\s+Setup (command|instructions)", line_s, re.IGNORECASE):
            j = i + 1
            # Skip blank lines
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("```"):
                # Code block format
                j += 1
                code_lines = []
                while j < len(lines) and not lines[j].strip().startswith("```"):
                    code_lines.append(lines[j].rstrip())
                    j += 1
                cmds = [cl.strip() for cl in code_lines if cl.strip() and not cl.strip().startswith("#")]
                setup = " && ".join(cmds)
                i = j
            else:
                # Plain text / numbered list format
                text_lines = []
                while j < len(lines):
                    nxt = lines[j].strip()
                    if nxt.startswith("###") or nxt.startswith("## ") or nxt.startswith("**Pass") or nxt.startswith("**Fail"):
                        break
                    if nxt:
                        text_lines.append(nxt)
                    j += 1
                setup = "\n".join(text_lines)
                i = j - 1
        elif re.match(r"^#{1,4}\s+(What to observe|What NUC Runs)", line_s, re.IGNORECASE):
            j = i + 1
            text_lines = []
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("###") or nxt.startswith("**Pass") or nxt.startswith("**Fail"):
                    break
                if nxt:
                    text_lines.append(nxt)
                j += 1
            observe = " ".join(text_lines)
            i = j - 1
        elif re.match(r"^#{1,4}\s+Pass criteria", line_s, re.IGNORECASE):
            j = i + 1
            text_lines = []
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("###") or nxt.startswith("**Fail") or nxt.startswith("## "):
                    break
                if nxt:
                    text_lines.append(nxt)
                j += 1
            pass_c = " ".join(text_lines)
            i = j - 1
        elif re.match(r"^#{1,4}\s+Fail criteria", line_s, re.IGNORECASE):
            j = i + 1
            text_lines = []
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("###") or nxt.startswith("## ") or nxt.startswith("---"):
                    break
                if nxt:
                    text_lines.append(nxt)
                j += 1
            fail_c = " ".join(text_lines)
            i = j - 1
        i += 1

    return {
        "setup_command": setup,
        "observe": observe,
        "pass_criteria": pass_c,
        "fail_criteria": fail_c,
    }


def process_signal_inbox(cfg: Config, board_mgr: BoardManager) -> None:
    """Process unreplied Signal messages from Ophir."""
    messages = get_unreplied_messages(cfg.inbox_file, cfg.ophir_number)
    if not messages:
        return

    log.info("Found %d unreplied Signal message(s) from Ophir", len(messages))

    timestamps = {m["ts"] for m in messages}
    combined = "\n".join(f"[{m['ts']}] {m['msg']}" for m in messages)
    latest_msg = messages[-1]["msg"]

    # ── Interactive test in progress — skip inbox ──
    if _is_interactive_test_running(cfg):
        log.info("Interactive physical test in progress — leaving messages for test runner")
        return

    # ── # commands: bypass Coach/Orchestrator entirely ──
    if latest_msg.strip().startswith("#"):
        if _handle_hash_command(cfg, board_mgr, latest_msg, timestamps):
            return
        # Unknown # command — tell user
        send_signal(cfg,
                    "🤖 Orchestrator: Unknown command. Available: "
                    "#go <issue>, #golden, #status <issue>, #approve <issue>, #board <issue>")
        mark_inbox_replied(cfg.inbox_file, timestamps)
        return

    # ── Physical test result (pass/fail, legacy flow) ──
    if _handle_physical_test_result(cfg, latest_msg, timestamps):
        return

    # ── Coach quality gate ──
    conversation = get_conversation_history(cfg.inbox_file)
    issue_context = gh.issue_list(
        cfg.nuc_repo,
        fields="number,title,labels",
        jq='.[] | "#\\(.number) \\(.title) [\\([.labels[].name] | join(","))]"',
    ) or ""
    vector_context = ""  # All issues are now in the single monorepo
    # Truncate context to avoid huge prompts
    if isinstance(issue_context, str):
        issue_context = "\n".join(issue_context.splitlines()[:5])

    coach_concern = _run_coach(cfg, combined, conversation, issue_context, vector_context)
    if coach_concern:
        coach_msg = f"🏋️ COACH: {coach_concern}"
        log.info("Coach flagged concern: %s", coach_msg)
        send_signal(cfg, coach_msg)
        mark_inbox_replied(cfg.inbox_file, timestamps)
        log.info("Coach blocked — waiting for Ophir's reply")
        return

    log.info("Coach approved — proceeding to Orchestrator")

    # ── Orchestrator response ──
    _run_orchestrator(cfg, combined, conversation, issue_context, vector_context)
    mark_inbox_replied(cfg.inbox_file, timestamps)


def _handle_hash_command(cfg: Config, board_mgr: BoardManager,
                         msg: str, timestamps: set[int]) -> bool:
    """Route # commands. Returns True if handled."""
    stripped = msg.strip()

    # #golden — launch golden test (full Sprint 1-9 regression)
    if re.match(r"^#golden(\s|$)", stripped, re.IGNORECASE):
        return _handle_golden(cfg, stripped, timestamps)

    # #go <issue>
    if re.match(r"^#go(\s|$)", stripped, re.IGNORECASE):
        return _handle_go(cfg, stripped, timestamps)

    # #status (bare) — sprint-wide summary
    if re.match(r"^#status\s*$", stripped, re.IGNORECASE):
        _handle_sprint_status(cfg, timestamps)
        return True

    # #status <issue>
    m = re.match(r"^#status\s+#?(\d+)$", stripped, re.IGNORECASE)
    if m:
        _handle_status_query(cfg, int(m.group(1)), timestamps)
        return True

    # #approve <issue>
    m = re.match(r"^#approve\s+#?(\d+)$", stripped, re.IGNORECASE)
    if m:
        _handle_approve(cfg, int(m.group(1)), timestamps)
        return True

    # #board <issue>
    m = re.match(r"^#board\s+#?(\d+)$", stripped, re.IGNORECASE)
    if m:
        board_mgr.approve_item(int(m.group(1)))
        mark_inbox_replied(cfg.inbox_file, timestamps)
        return True

    return False


def _handle_golden(cfg: Config, msg: str, timestamps: set[int]) -> bool:
    """Handle #golden — launch full golden test with LiveKit URL, camera check, all phases."""
    log.info("Golden test requested via Signal")
    mark_inbox_replied(cfg.inbox_file, timestamps)

    # Kill any stale golden test from a previous #golden
    old_state = read_json_file(cfg.physical_test_state)
    old_pid = old_state.get("pid")
    if old_pid and old_state.get("state") in ("running_golden", "running_interactive"):
        try:
            os.kill(old_pid, 9)
            log.info("Killed stale test runner PID %d", old_pid)
        except (ProcessLookupError, PermissionError):
            pass

    # Immediately safe the robot BEFORE launching the script (no race window)
    log.info("Safing robot: manual mode + stop + disable detection")
    subprocess.run(["curl", "-sf", "-X", "POST",
                    "http://192.168.1.71:8081/manual/on",
                    "-H", "Content-Type: application/json", "-d", "{}"],
                   capture_output=True, timeout=5)
    subprocess.run(["curl", "-sf", "-X", "POST",
                    "http://192.168.1.71:8081/stop"],
                   capture_output=True, timeout=5)
    subprocess.run(["ssh", "vector",
                    "docker exec muscle bash -c 'source /opt/ros/humble/setup.bash && "
                    "source /opt/ros2_ws/install/setup.bash && "
                    "ros2 param set /person_detector enable false && "
                    "ros2 param set /planner max_speed 0.0'"],
                   capture_output=True, timeout=10)

    # Parse optional flags from message (e.g. "#golden --skip-following")
    parts = msg.strip().split()
    extra_args = parts[1:] if len(parts) > 1 else []

    # Build command — unbuffered python so output streams in real time
    cmd = ["python3", "-u", str(cfg.repo_dir / "monitoring" / "golden_test.py")] + extra_args
    log.info("Launching golden test: %s", " ".join(cmd))

    # start_new_session=True detaches from agent-loop's process group
    # so systemctl restart won't kill a running golden test
    log_path = cfg.repo_dir / ".claude" / "state" / "golden-test.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cfg.repo_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Save PID so a new #golden can kill a stale one
    write_json_file(cfg.physical_test_state, {
        "state": "running_golden",
        "pid": proc.pid,
    })

    log.info("Golden test started (PID %d)", proc.pid)
    return True


def _handle_go(cfg: Config, msg: str, timestamps: set[int]) -> bool:
    """Handle #go physical test trigger. Returns True if handled."""
    target = parse_go_issue_number(msg)
    if target:
        log.info("Physical test '#go %s' — targeting issue #%s", target, target)

    pt_info = None
    pt_state = read_json_file(cfg.physical_test_state)
    if pt_state.get("state") == "awaiting_go":
        if not target or str(pt_state.get("issue_num")) == target:
            pt_info = pt_state

    if not pt_info:
        pt_info = find_pending_physical_test(cfg, target)

    if not pt_info:
        issue_ref = f" for #{target}" if target else ""
        log.warning("No pending physical test found%s — missing blocker:needs-human label or Physical Test Request comment?", issue_ref)
        send_signal(cfg, f"🤖 Orchestrator: Can't find a Physical Test Request{issue_ref}. Check that the issue has `blocker:needs-human` label and a comment with `## Physical Test Request` or `## Updated Physical Test Request`.")
        mark_inbox_replied(cfg.inbox_file, timestamps)
        return True

    log.info("Physical test '#go' detected — launching interactive test")

    # Kill any stale test runner from a previous #go
    old_state = read_json_file(cfg.physical_test_state)
    old_pid = old_state.get("pid")
    if old_pid and old_state.get("state") == "running_interactive":
        try:
            os.kill(old_pid, 9)  # SIGKILL — force kill old runner
            log.info("Killed stale test runner PID %d", old_pid)
        except (ProcessLookupError, PermissionError):
            pass

    # Whitelist of allowed setup scripts — never pass raw user input to bash
    _ALLOWED_SETUP_SCRIPTS = {
        "run-evolution-cycle": ["bash", "scripts/run-evolution-cycle.sh"],
        "run-physical-test": ["bash", "scripts/run-physical-test.sh"],
        "golden_test": ["python3", "apps/test_harness/golden_test.py"],
    }

    setup = pt_info.get("setup_command", "")
    matched_script = None
    for key, cmd in _ALLOWED_SETUP_SCRIPTS.items():
        if key in setup:
            matched_script = (key, cmd)
            break

    if matched_script:
        key, cmd = matched_script
        log.info("Setup command matched whitelist entry: %s", key)
        proc = subprocess.Popen(cmd, cwd=str(cfg.repo_dir))
    else:
        # Default: pass structured JSON to the physical test runner (no shell interpretation)
        proc = subprocess.Popen(
            ["bash", "scripts/run-physical-test.sh", json.dumps(pt_info)],
            cwd=str(cfg.repo_dir),
        )

    pt_info["state"] = "running_interactive"
    pt_info["pid"] = proc.pid
    write_json_file(cfg.physical_test_state, pt_info)

    mark_inbox_replied(cfg.inbox_file, timestamps)
    log.info("Interactive physical test launched — runner handles all Signal communication")
    return True


def _is_interactive_test_running(cfg: Config) -> bool:
    """Check if an interactive physical test is currently running."""
    state = read_json_file(cfg.physical_test_state)
    if state.get("state") not in ("running_interactive", "running_golden"):
        return False
    # Verify the process is actually alive — stale state from crashed runners
    pid = state.get("pid")
    if pid:
        try:
            os.kill(pid, 0)  # Signal 0 = check existence, no actual signal
        except (ProcessLookupError, PermissionError):
            log.warning("Physical test PID %d is dead — resetting state to idle", pid)
            state["state"] = "idle"
            write_json_file(cfg.physical_test_state, state)
            return False
    return True


def _handle_physical_test_result(cfg: Config, msg: str, timestamps: set[int]) -> bool:
    """Handle pass/fail result for legacy physical test flow. Returns True if handled."""
    state = read_json_file(cfg.physical_test_state)
    if state.get("state") != "awaiting_result":
        return False

    result = parse_physical_test_result(msg)
    if not result:
        return False

    target = parse_result_issue_number(msg)
    pt_issue = state.get("issue_num")
    pt_repo = state.get("repo")

    if target and str(target) != str(pt_issue):
        log.info("Result target #%s doesn't match pending #%s — ignoring", target, pt_issue)
        send_signal(cfg,
                    f"🤖 Orchestrator: No pending physical test for #{target}. "
                    f"Currently awaiting result for #{pt_issue}.")
        mark_inbox_replied(cfg.inbox_file, timestamps)
        return True

    log.info("Physical test result: %s for #%s", result, pt_issue)

    verdict = result.upper()
    gh.issue_comment(pt_repo, pt_issue,
                     f"## 🤖 Orchestrator: Physical Test Result from Ophir\n\n"
                     f"**Verdict:** {verdict}\n"
                     f"**Feedback:** {msg}\n\n"
                     "Worker will be re-dispatched to evaluate and close.")

    gh.issue_edit_labels(pt_repo, pt_issue,
                         remove=["blocker:needs-human", "stuck"])
    gate_file = cfg.state_dir / "pgm-signal-sent.tsv"
    delete_tsv_entries(gate_file, rf"-{pt_issue}\t")

    # Stop robot after test
    if "vector" in str(pt_repo):
        log.info("Stopping muscle container after physical test...")
        subprocess.run(
            ["ssh", "vector", "cd /home/yahboom/claude && docker compose down muscle"],
            capture_output=True, text=True, timeout=30,
        )

    send_signal(cfg,
                f"🤖 Orchestrator: Got it — {result}. "
                f"Posted on issue #{pt_issue}, robot stopped, worker will evaluate.")

    cfg.physical_test_state.unlink(missing_ok=True)
    mark_inbox_replied(cfg.inbox_file, timestamps)
    return True


def _handle_sprint_status(cfg: Config, timestamps: set[int]) -> None:
    """Send a project-wide status summary via Signal, grouped by phase."""
    log.info("Sprint status requested via Signal")
    mark_inbox_replied(cfg.inbox_file, timestamps)

    def _label_names(issue):
        return [label.get("name", "") if isinstance(label, dict) else str(label)
                for label in issue.get("labels", [])]

    def _get_phase(issue):
        for name in _label_names(issue):
            if name.startswith("phase:"):
                return name
        return ""

    open_issues = gh.issue_list(cfg.nuc_repo, state="open",
                                fields="number,title,labels", limit=60)
    closed_issues = gh.issue_list(cfg.nuc_repo, state="closed",
                                  fields="number,title,labels", limit=30)

    # Count totals
    total_open = len(open_issues)
    total_closed = len(closed_issues)
    active = [i for i in open_issues if "assigned:worker" in _label_names(i)]
    stuck = [i for i in open_issues if "stuck" in _label_names(i)]
    blocked = [i for i in open_issues
               if any(l.startswith("blocker:") for l in _label_names(i))]

    lines = [f"📊 Project Status ({total_closed} closed, {total_open} open)\n"]

    # Active workers
    if active:
        lines.append("🔄 Active:")
        for i in active:
            lines.append(f"  #{i['number']} {i['title'][:45]}")
    else:
        lines.append("⏸️ No active workers")

    # Blocked
    if blocked:
        lines.append(f"\n🚫 Blocked: {len(blocked)}")
        for i in blocked:
            lines.append(f"  #{i['number']} {i['title'][:45]}")

    # Phase summary
    phases: dict[str, dict[str, int]] = {}
    for i in open_issues:
        phase = _get_phase(i)
        if not phase:
            continue
        phases.setdefault(phase, {"open": 0, "stuck": 0})
        phases[phase]["open"] += 1
        if "stuck" in _label_names(i):
            phases[phase]["stuck"] += 1
    for i in closed_issues:
        phase = _get_phase(i)
        if not phase:
            continue
        phases.setdefault(phase, {"open": 0, "stuck": 0})

    # Count closed per phase from closed_issues
    phase_closed: dict[str, int] = {}
    for i in closed_issues:
        phase = _get_phase(i)
        if phase:
            phase_closed[phase] = phase_closed.get(phase, 0) + 1

    if phases:
        lines.append("\nBy phase:")
        for phase in sorted(phases):
            c = phase_closed.get(phase, 0)
            o = phases[phase]["open"]
            s = phases[phase]["stuck"]
            status = f"{c}✅ {o}⏳"
            if s:
                status += f" ({s} stuck)"
            lines.append(f"  {phase}: {status}")

    send_signal(cfg, "\n".join(lines))


def _handle_status_query(cfg: Config, issue_num: int, timestamps: set[int]) -> None:
    """Look up issue status on both repos and send summary via Signal."""
    log.info("Status query for issue #%d", issue_num)

    found = False
    parts = []

    for repo in [cfg.nuc_repo]:
        raw = gh.issue_view(
            repo, issue_num,
            fields="title,state,labels,comments",
            jq='{title,state,labels:[.labels[].name],comments:[.comments[].body]}',
        )
        if not raw:
            continue
        try:
            info = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        state = info.get("state", "?")
        # Skip closed issues — usually irrelevant to status queries
        if state == "CLOSED":
            continue

        found = True
        repo_short = "Vector" if "vector" in repo else "NUC"
        title = info.get("title", "?")
        labels = info.get("labels", [])

        # Meaningful label summary
        blockers = [label for label in labels if label.startswith("blocker:")]
        status_labels = [label for label in labels if label in ("stuck", "assigned:worker", "human-approved")]
        sprint_labels = [label for label in labels if label.startswith("sprint-")]
        label_parts = blockers + status_labels + sprint_labels
        label_str = ", ".join(label_parts) if label_parts else "none"

        # Check for linked PR
        pr_num = gh.find_pr_for_issue(repo, issue_num)
        pr_info = ""
        if pr_num:
            pr_state = gh.pr_view(repo, pr_num, fields="state", jq=".state")
            pr_info = f"\n  PR #{pr_num}: {pr_state}"

        # Last comment — grab first 2 non-empty, non-HTML lines
        comments = info.get("comments", []) or []
        last_useful = ""
        for comment in reversed(comments):
            if not comment:
                continue
            meaningful_lines = [
                line.strip() for line in comment.strip().split("\n")
                if line.strip() and not line.strip().startswith("<!--")
            ][:3]
            last_useful = " | ".join(meaningful_lines)[:200]
            break

        comment_snippet = f"\n  Latest: {last_useful}" if last_useful else ""

        # Check if worker is actively dispatched
        dispatched_file = cfg.state_dir / "board-dispatched.txt"
        worker_active = ""
        if dispatched_file.exists():
            dispatched = dispatched_file.read_text()
            if f"#{issue_num}" in dispatched or f"-{issue_num}" in dispatched:
                worker_active = "\n  ⚡ Worker currently active"

        parts.append(
            f"**{repo_short} #{issue_num}:** {title}\n"
            f"  State: {state}, Labels: {label_str}"
            f"{pr_info}{worker_active}{comment_snippet}"
        )

    if not found:
        send_signal(cfg, f"🤖 Orchestrator: Issue #{issue_num} — not found or closed on both repos.")
    else:
        send_signal(cfg, f"🤖 Orchestrator: Status for #{issue_num}\n\n" + "\n\n".join(parts))

    mark_inbox_replied(cfg.inbox_file, timestamps)


def _handle_approve(cfg: Config, issue_num: int, timestamps: set[int]) -> None:
    """Handle merge approval for an issue."""
    log.info("Merge approval for issue #%d", issue_num)
    approved_any = False
    for repo in [cfg.nuc_repo]:
        state = gh.issue_view(repo, issue_num, fields="state", jq=".state")
        if state == "OPEN":
            gh.issue_edit_labels(repo, issue_num,
                                 add=["human-approved"],
                                 remove=["blocker:needs-human", "stuck"])
            gate_file = cfg.state_dir / "pgm-signal-sent.tsv"
            delete_tsv_entries(gate_file, rf"-{issue_num}\t")
            approved_any = True
            log.info("Approved #%d on %s", issue_num, repo)

    if approved_any:
        send_signal(cfg,
                    f"🤖 Orchestrator: #{issue_num} approved — merge gate will auto-merge on next cycle.")
    else:
        send_signal(cfg,
                    f"🤖 Orchestrator: Could not find open issue #{issue_num} on either repo.")

    mark_inbox_replied(cfg.inbox_file, timestamps)


def _run_coach(cfg: Config, combined: str, conversation: str,
               issue_context: str, vector_context: str) -> str:
    """Run Coach quality gate. Returns concern text or empty string if approved."""
    coach_prompt = f"""You are the Coach — quality gate for Project Vector (robotics AI system).
Ophir (lead engineer) sent messages on Signal. Evaluate them QUICKLY.

CONVERSATION:
{conversation}

NEW MESSAGES FROM OPHIR:
{combined}

OPEN ISSUES:
NUC: {issue_context or "none"}
Vector: {vector_context or "none"}

EVALUATE for: clarity (can agents execute without confusion?), risks (will this break something?), architectural conflicts, missing dependencies, scope issues.

RESPOND WITH EXACTLY ONE LINE — nothing else:
- Greetings, status requests, test results, approvals, short replies → APPROVED
- Work instructions with NO concerns → APPROVED
- Work instructions WITH concerns → CONCERN: [brief concern]. Suggestion: [fix].

ONLY flag REAL problems that will cause agent failures, architectural damage, or dangerous operations. Do NOT nitpick wording. Do NOT flag things that are merely suboptimal. Be concise."""

    output, _ = run_llm(
        cfg, "heavy", coach_prompt, timeout=120,
        agent_role="coach", issue_key="agent:coach",
        cwd=cfg.repo_dir,
    )

    # Extract verdict — last matching line (codex echoes prompt)
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("CONCERN:"):
            return line[len("CONCERN:"):].strip()
        if line.startswith("APPROVED"):
            return ""

    return ""  # default: approved


def _run_orchestrator(cfg: Config, combined: str, conversation: str,
                      issue_context: str, vector_context: str) -> None:
    """Run Orchestrator to respond to Ophir's messages."""
    prompt = f"""You are Vector, the Orchestrator for Project Vector — a robotics AI system on a NUC. Ophir is your lead engineer texting you on Signal.

CONVERSATION:
{conversation}

NEW (reply to this):
{combined}

OPEN ISSUES:
NUC: {issue_context or "none"}
Vector: {vector_context or "none"}

DO THIS NOW — no reading files, no exploring. Just:
1. Send ONE concise Signal reply (prefix: 🤖 Orchestrator:)
2. If Ophir gave a work instruction, also create a GitHub Issue: gh issue create -R {cfg.nuc_repo} --title 'TITLE' --label assigned:worker --body 'BODY'
3. If Ophir reported a test result, comment on the relevant issue: gh issue comment NUM -R REPO -b 'RESULT'
4. Physical test flow is handled automatically by the agent-loop (go/pass/fail detection). You do NOT need to handle 'go' or 'pass'/'fail' messages for physical tests — the loop handles that before you run.

SEND COMMAND (use this exactly):
python3 -c "import json, sys; print(json.dumps({{'jsonrpc':'2.0','method':'send','params':{{'groupId':'{cfg.alert_group_id}','message': sys.argv[1]}},'id':1}}))" "YOUR_MESSAGE" > /tmp/sig-reply.json && sg docker -c "docker cp /tmp/sig-reply.json {cfg.bot_container}:/tmp/sig-reply.json" && sg docker -c "docker exec {cfg.bot_container} curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' -d @/tmp/sig-reply.json"

RULES: No reading .env/secrets. No sudo. No modifying .md files. No restarting containers. Keep reply SHORT."""

    output, exit_code = run_llm(
        cfg, "medium", prompt, timeout=600,
        agent_role="orchestrator", issue_key="agent:orchestrator",
        cwd=cfg.repo_dir,
    )

    if exit_code != 0:
        log.warning("Orchestrator session failed (exit code: %d)", exit_code)
    else:
        log.info("Signal inbox processed successfully")
