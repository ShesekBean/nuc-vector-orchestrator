"""Board polling — Inbox/Needs Input notifications, proposals, replies, dispatch."""

from __future__ import annotations

import json
import logging
import re
import time

from . import github as gh
from .config import Config
from .llm import run_llm
from .signal_client import send_signal, send_signal_gated
from .state import read_file_lines, append_line

log = logging.getLogger("agent-loop")


class BoardManager:
    """Manages GitHub Projects board interaction."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.last_run = 0
        self.interval = 300  # 5 minutes
        self.dispatched_file = cfg.state_dir / "board-dispatched.txt"
        self.proposed_file = cfg.state_dir / "board-proposed.tsv"
        self.last_comment_file = cfg.state_dir / "board-last-comment.tsv"
        self.state_file = cfg.state_dir / "board-state.tsv"

    def run_all(self) -> None:
        """Run all board checks."""
        now = int(time.time())
        if now - self.last_run < self.interval:
            return
        self.last_run = now
        self.check_notifications()
        self.propose_items()
        self.check_replies()

    def fetch_board_json(self) -> dict | None:
        """Fetch all project items."""
        return gh.graphql("""query {
          user(login: "ShesekBean") {
            projectV2(number: 1) {
              items(first: 50) {
                nodes {
                  id
                  fieldValueByName(name: "Status") {
                    ... on ProjectV2ItemFieldSingleSelectValue { name }
                  }
                  content {
                    ... on Issue {
                      number
                      title
                      body
                      repository { nameWithOwner }
                      comments(last: 1) {
                        nodes {
                          author { login }
                          body
                          createdAt
                        }
                      }
                    }
                    ... on DraftIssue {
                      title
                      body
                    }
                  }
                }
              }
            }
          }
        }""")

    def check_notifications(self) -> None:
        """Check for board status changes and notify Ophir."""
        board_data = self.fetch_board_json()
        if not board_data:
            return

        items = self._extract_inbox_needs_items(board_data)
        current_snapshot = self._build_snapshot(items)

        prev_snapshot = ""
        if self.state_file.exists():
            prev_snapshot = self.state_file.read_text().strip()

        if current_snapshot == prev_snapshot:
            return

        self.state_file.write_text(current_snapshot)
        if not current_snapshot:
            return

        msg = self._build_notification_message(items, board_data)
        send_signal_gated(self.cfg, "board-status", 0, msg)
        log.info("Board update sent to Ophir")

    def propose_items(self) -> None:
        """Propose new Inbox items to Ophir via Signal."""
        self.proposed_file.touch()
        self.dispatched_file.touch()

        board_data = self.fetch_board_json()
        if not board_data:
            return

        proposed = set(read_file_lines(self.proposed_file))
        dispatched = set(read_file_lines(self.dispatched_file))

        try:
            nodes = board_data["data"]["user"]["projectV2"]["items"]["nodes"]
        except (KeyError, TypeError):
            return

        for node in nodes:
            status = node.get("fieldValueByName", {}).get("name", "")
            if status != "Inbox":
                continue
            content = node.get("content", {})
            number = content.get("number")
            if not number:
                continue
            title = content.get("title", "")
            repo = content.get("repository", {}).get("nameWithOwner", "")
            item_id = node.get("id", "")

            key = f"{repo}#{number}"
            if any(key in s for s in proposed) or any(key in s for s in dispatched):
                continue

            log.info("Board: new item #%d (%s) — proposing to Ophir", number, title)
            body = gh.issue_view(repo, number, fields="body", jq=".body")

            # Generate proposal via LLM
            nuc_issues = gh.issue_list(
                self.cfg.nuc_repo, fields="number,title",
                jq='.[] | "#\\(.number) \\(.title)"',
            ) or ""
            proposal_prompt = f"""You are the Orchestrator for Project Vector. Ophir posted a new idea on the board.

**Board item:** {repo}#{number} — {title}

**Description:**
{body}

**Current open issues:**
{nuc_issues[:500] if isinstance(nuc_issues, str) else ""}

Write a SHORT Signal message (max 5 lines) to Ophir proposing how to implement this.
Format:
🤖 Orchestrator: New board item — #{number} "{title}"

[2-3 line proposal: what you'd do, which repo, any dependencies]

Reply "go board {number}" to approve, or tell me your thoughts.

Keep it concise. No markdown formatting (Signal doesn't render it).
Output ONLY the message text, nothing else."""

            proposal_msg, _ = run_llm(
                self.cfg, "light", proposal_prompt, timeout=60,
                agent_role="orchestrator", issue_key=f"board:{number}",
            )

            if not proposal_msg:
                proposal_msg = (
                    f'🤖 Orchestrator: New board item — #{number} "{title}"\n\n'
                    f'{body[:200] if body else "(no description)"}\n\n'
                    f'Reply "go board {number}" to approve, or tell me your thoughts.'
                )

            send_signal(self.cfg, proposal_msg)
            gh.issue_comment(repo, number,
                             f"## 🤖 Orchestrator Proposal\n\n{proposal_msg}\n\n---\n"
                             f"*Reply here or on Signal. Approve with `go board {number}` on Signal.*")

            # Move to Needs Input
            self._update_board_status(item_id, self.cfg.board_needs_input_option)
            log.info("Board: moved #%d to Needs Input", number)

            append_line(self.proposed_file, f"{key}\t{int(time.time())}\t{item_id}")

    def check_replies(self) -> None:
        """Check proposed items for new GitHub comments from Ophir."""
        if not self.proposed_file.exists():
            return
        self.last_comment_file.touch()
        self.dispatched_file.touch()

        dispatched = set(read_file_lines(self.dispatched_file))
        last_comments: dict[str, str] = {}
        for line in read_file_lines(self.last_comment_file):
            parts = line.split("\t")
            if len(parts) >= 2:
                last_comments[parts[0]] = parts[1]

        for line in read_file_lines(self.proposed_file):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            key, _, item_id = parts[0], parts[1], parts[2]

            if any(key in d for d in dispatched):
                continue

            repo, number_str = key.rsplit("#", 1)
            number = int(number_str)

            # Get latest non-bot comment
            latest_comment = gh.issue_view(
                repo, number, fields="comments",
                jq='[.comments[] | select(.body | startswith("## 🤖") | not)] | last | .body // ""',
            )
            if not latest_comment:
                continue

            latest_ts = gh.issue_view(
                repo, number, fields="comments",
                jq='[.comments[] | select(.body | startswith("## 🤖") | not)] | last | .createdAt // ""',
            )
            if not latest_ts or latest_ts == last_comments.get(key):
                continue

            log.info("Board: new comment on %s — evaluating", key)
            self._evaluate_reply(repo, number, key, item_id, latest_comment)

            # Update last comment tracking
            last_comments[key] = latest_ts
            self.last_comment_file.write_text(
                "\n".join(f"{k}\t{v}" for k, v in last_comments.items()) + "\n"
            )

    def approve_item(self, board_num: int) -> None:
        """Approve a board item — create worker issue and move to In Progress."""
        self.proposed_file.touch()
        self.dispatched_file.touch()

        # Find matching proposed item
        match_line = None
        for line in read_file_lines(self.proposed_file):
            if f"#{board_num}\t" in line or f"#{board_num}" in line:
                match_line = line
                break

        if not match_line:
            send_signal(self.cfg,
                        f"🤖 Orchestrator: Board item #{board_num} not found in proposals. Check the board?")
            return

        parts = match_line.split("\t")
        key = parts[0]
        item_id = parts[2] if len(parts) >= 3 else ""
        repo, number_str = key.rsplit("#", 1)
        number = int(number_str)

        title = gh.issue_view(repo, number, fields="title", jq=".title") or f"Board item #{number}"
        body = gh.issue_view(repo, number, fields="body", jq=".body") or ""

        log.info("Board: Ophir approved #%d (%s) — creating worker issue", number, title)

        worker_body = (
            f"## From Ophir's Board\n\n"
            f"**Source:** {repo}#{number}\n"
            f"**Original:** https://github.com/{repo}/issues/{number}\n\n"
            f"{body}\n\n---\n"
            f"*Approved by Ophir via Signal. Auto-dispatched from project board.*"
        )

        worker_url = gh.gh(
            "issue", "create", "-R", self.cfg.nuc_repo,
            "--title", f"Board: {title}",
            "--label", "assigned:worker",
            "--body", worker_body,
        )

        if worker_url:
            log.info("Board: created worker issue %s", worker_url)
            append_line(self.dispatched_file, key)

            if item_id:
                self._update_board_status(item_id, self.cfg.board_in_progress_option)
                log.info("Board: moved #%d to In Progress", number)

            gh.issue_comment(repo, number, f"Approved by Ophir. Worker issue: {worker_url}")
            send_signal(self.cfg,
                        f'🤖 Orchestrator: Done! Created worker issue {worker_url} for "{title}". '
                        "Worker will pick it up shortly.")
        else:
            log.error("Board: failed to create worker issue for #%d", number)
            send_signal(self.cfg,
                        f"🤖 Orchestrator: Failed to create worker issue for #{number}. Will retry.")

    def _evaluate_reply(self, repo: str, number: int, key: str,
                        item_id: str, latest_comment: str) -> None:
        """Use LLM to evaluate Ophir's reply to a board proposal."""
        title = gh.issue_view(repo, number, fields="title", jq=".title") or ""
        body = gh.issue_view(repo, number, fields="body", jq=".body") or ""
        all_comments = gh.issue_view(
            repo, number, fields="comments",
            jq='.comments[] | "\\(.createdAt): \\(.body[0:300])"',
        ) or ""
        # Take last 10 lines
        all_comments = "\n".join(all_comments.split("\n")[-10:])

        eval_prompt = f"""You are the Orchestrator for Project Vector. Ophir commented on a board item proposal.

**Board item:** {repo}#{number} — {title}

**Issue description:**
{body}

**Conversation so far:**
{all_comments}

**Ophir's latest comment:**
{latest_comment}

Classify Ophir's reply as ONE of:
- APPROVE — Ophir is satisfied, proceed to create the worker issue
- DISCUSS — Ophir has questions, concerns, or suggestions that need a response
- REJECT — Ophir doesn't want this done

Output a JSON object with exactly these fields:
{{"classification": "APPROVE|DISCUSS|REJECT", "response": "your response message to post as a comment"}}

If DISCUSS: write a thoughtful reply addressing Ophir's concerns, then ask if they want to proceed.
If APPROVE: response should be a brief "Creating worker issue now."
If REJECT: response should acknowledge and say you'll close it.

Output ONLY the JSON, nothing else."""

        eval_output, _ = run_llm(
            self.cfg, "light", eval_prompt, timeout=60,
            agent_role="orchestrator", issue_key=f"board:{number}",
        )

        classification = "DISCUSS"
        response_msg = ""
        try:
            m = re.search(r"\{.*\}", eval_output, re.DOTALL)
            if m:
                data = json.loads(m.group())
                classification = data.get("classification", "DISCUSS")
                response_msg = data.get("response", "")
        except (json.JSONDecodeError, AttributeError):
            pass

        log.info("Board: %s — classification: %s", key, classification)

        if classification == "APPROVE":
            gh.issue_comment(repo, number, f"## 🤖 Orchestrator\n\n{response_msg}")
            self.approve_item(number)
        elif classification == "REJECT":
            gh.issue_comment(repo, number, f"## 🤖 Orchestrator\n\n{response_msg}")
            # Remove from proposed
            if self.proposed_file.exists():
                lines = [ln for ln in read_file_lines(self.proposed_file)
                         if not ln.startswith(f"{key}\t")]
                self.proposed_file.write_text("\n".join(lines) + "\n" if lines else "")
            log.info("Board: %s rejected by Ophir", key)
        else:
            if response_msg and response_msg != "Let me think about this further.":
                gh.issue_comment(repo, number, f"## 🤖 Orchestrator\n\n{response_msg}")
                send_signal(self.cfg,
                            f'🤖 Orchestrator: Re #{number} "{title}" — replied to your comment. '
                            "Check GitHub or reply here.")
                log.info("Board: %s — posted discussion reply", key)

    def _update_board_status(self, item_id: str, option_id: str) -> None:
        """Update a board item's status field."""
        gh.graphql(
            """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId, itemId: $itemId,
                fieldId: $fieldId,
                value: { singleSelectOptionId: $optionId }
              }) { projectV2Item { id } }
            }""",
            projectId=self.cfg.board_project_id,
            itemId=item_id,
            fieldId=self.cfg.board_status_field_id,
            optionId=option_id,
        )

    def _extract_inbox_needs_items(self, board_data: dict) -> list[dict]:
        """Extract Inbox and Needs Input items from board data."""
        items = []
        try:
            nodes = board_data["data"]["user"]["projectV2"]["items"]["nodes"]
        except (KeyError, TypeError):
            return items

        for node in nodes:
            status = node.get("fieldValueByName", {}).get("name", "")
            if status not in ("Inbox", "Needs Input"):
                continue
            content = node.get("content", {})
            number = content.get("number", "draft")
            title = content.get("title", "")
            repo = content.get("repository", {}).get("nameWithOwner", "draft") if "repository" in content else "draft"
            comments = content.get("comments", {}).get("nodes", [])
            last = comments[-1] if comments else {}
            items.append({
                "status": status,
                "number": number,
                "title": title,
                "repo": repo,
                "item_id": node.get("id", ""),
                "last_author": last.get("author", {}).get("login", "none"),
                "last_comment_preview": (last.get("body", "").split("\n")[0][:60] + "..."
                                         if len(last.get("body", "").split("\n")[0]) > 60
                                         else last.get("body", "").split("\n")[0]),
            })
        return items

    def _build_snapshot(self, items: list[dict]) -> str:
        """Build a snapshot string for change detection."""
        lines = []
        for item in sorted(items, key=lambda x: str(x.get("number", ""))):
            lines.append(
                f"{item['status']}|{item['number']}|{item['title']}|"
                f"{item['repo']}|{item['last_author']}"
            )
        return "\n".join(lines)

    def _build_notification_message(self, items: list[dict], board_data: dict) -> str:
        """Build Signal notification message for board changes."""
        msg = "📊 PGM: Board Update"
        has_inbox = False
        has_needs = False

        for item in items:
            if item["status"] == "Inbox":
                if not has_inbox:
                    msg += "\n📥 Inbox:"
                    has_inbox = True
                msg += f"\n  #{item['number']} {item['title']}"
                if item["last_author"] != "none":
                    msg += f"\n    └ {item['last_author']}: {item['last_comment_preview']}"

                # Auto-move to Inbox if Ophir made last comment
                if item["last_author"] == "ShesekBean":
                    proposed = set(read_file_lines(self.proposed_file)) if self.proposed_file.exists() else set()
                    item_key = f"{item['repo']}#{item['number']}"
                    if not any(item_key in p for p in proposed):
                        self._update_board_status(
                            item["item_id"], self.cfg.board_inbox_option
                        )
                        log.info("Board: moved #%s to Inbox (Ophir replied)", item["number"])

            elif item["status"] == "Needs Input":
                if not has_needs:
                    msg += "\n❓ Needs Input:"
                    has_needs = True
                msg += f"\n  #{item['number']} {item['title']}"
                if item["last_author"] != "none":
                    msg += f"\n    └ {item['last_author']}: {item['last_comment_preview']}"

        return msg
