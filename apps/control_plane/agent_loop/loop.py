"""Main agent loop — ties everything together."""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from .account_rotation import AccountRotator
from .board import BoardManager
from .config import Config
from .dispatch import get_dispatchable_issues, work_on_issue
from .inbox import process_signal_inbox
from .pgm import PGMManager
from .state import delete_tsv_entries
from .watchdog import notify_ready, ping as watchdog_ping

log = logging.getLogger("agent-loop")


MAX_CONSECUTIVE_FAILURES = 3

# Inbox polling during waking hours (6am–midnight), every 5 minutes
INBOX_POLL_START_HOUR = 6
INBOX_POLL_END_HOUR = 24
INBOX_POLL_INTERVAL = 300  # 5 minutes


class AgentLoop:
    """The main NUC agent loop."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.board = BoardManager(cfg)
        self.pgm = PGMManager(cfg)
        self.rotator = AccountRotator(cfg.state_dir / "accounts")
        self._fail_counts: dict[str, int] = {}  # "repo#num" → consecutive failures
        self._inbox_stop = threading.Event()
        self._inbox_thread: threading.Thread | None = None
        self._pgm_stop = threading.Event()
        self._pgm_thread: threading.Thread | None = None

    def run(self) -> None:
        """Run the main loop."""
        log.info("=== NUC Agent Loop starting (Python) ===")
        log.info("Repo: %s", self.cfg.repo_dir)
        log.info("Poll interval: %ds (inbox: %ds)", self.cfg.poll_interval, self.cfg.inbox_poll)
        log.info("Dispatch label: %s", self.cfg.dispatch_label)
        log.info("Dispatch: %s", "enabled" if self.cfg.dispatch_enabled else "disabled")
        if self.cfg.max_cycles > 0:
            log.info("Max cycles: %d (bounded mode)", self.cfg.max_cycles)
        else:
            log.info("Max cycles: unlimited (daemon mode)")

        # Start background pollers (run continuously alongside workers)
        self._start_inbox_poller()
        self._start_pgm_poller()

        notify_ready()
        cycle_count = 0

        while True:
            watchdog_ping()
            try:
                self._run_cycle()
            except Exception:
                log.exception("Error in cycle %d", cycle_count + 1)

            cycle_count += 1
            if 0 < self.cfg.max_cycles <= cycle_count:
                log.info("Reached MAX_CYCLES=%d; exiting after %d cycle(s)",
                         self.cfg.max_cycles, cycle_count)
                self._stop_inbox_poller()
                self._stop_pgm_poller()
                break

            # Sleep between cycles, inbox poller handles messages in background
            time.sleep(self.cfg.poll_interval)

    def _start_inbox_poller(self) -> None:
        """Start background thread that polls Signal inbox during waking hours."""
        self._inbox_stop.clear()
        self._inbox_thread = threading.Thread(
            target=self._inbox_poll_loop, daemon=True, name="inbox-poller")
        self._inbox_thread.start()
        log.info("Inbox poller started (every %ds, %d:00–%d:00)",
                 INBOX_POLL_INTERVAL, INBOX_POLL_START_HOUR, INBOX_POLL_END_HOUR)

    def _stop_inbox_poller(self) -> None:
        """Stop the background inbox poller."""
        self._inbox_stop.set()
        if self._inbox_thread:
            self._inbox_thread.join(timeout=10)

    def _inbox_poll_loop(self) -> None:
        """Background loop: poll inbox every 5 min during 6am–midnight."""
        while not self._inbox_stop.is_set():
            hour = time.localtime().tm_hour
            if INBOX_POLL_START_HOUR <= hour < INBOX_POLL_END_HOUR:
                try:
                    process_signal_inbox(self.cfg, self.board)
                except Exception:
                    log.exception("Inbox poller error")
            self._inbox_stop.wait(INBOX_POLL_INTERVAL)

    def _start_pgm_poller(self) -> None:
        """Start background thread that runs PGM every 5 minutes."""
        self._pgm_stop.clear()
        self._pgm_thread = threading.Thread(
            target=self._pgm_poll_loop, daemon=True, name="pgm-poller")
        self._pgm_thread.start()
        log.info("PGM poller started (every 300s)")

    def _stop_pgm_poller(self) -> None:
        """Stop the background PGM poller."""
        self._pgm_stop.set()
        if self._pgm_thread:
            self._pgm_thread.join(timeout=10)

    def _pgm_poll_loop(self) -> None:
        """Background loop: run PGM health checks every 5 minutes."""
        while not self._pgm_stop.is_set():
            try:
                self.pgm.unblock_resolved_physical_tests()
                self.pgm.notify_pending_physical_tests()
                self.pgm.run_health_check()
                self.pgm.check_md_drift()
                self.pgm.run_sprint_advance()
            except Exception:
                log.exception("PGM poller error")
            self._pgm_stop.wait(300)

    def _run_cycle(self) -> None:
        """Run a single dispatch cycle."""
        # Pull latest
        self._git_pull()

        # Dispatch workers (PGM runs independently in background thread)
        found_work = False
        if self.cfg.dispatch_enabled:
            found_work = self._dispatch_workers()
        else:
            log.info("Skipping worker dispatch (dispatch_enabled=False)")

        # Board checks (if not already run in parallel with workers)
        if not found_work:
            self.board.run_all()

    def _git_pull(self) -> None:
        """Pull latest code from origin."""
        repo_dir = self.cfg.repo_dir
        if not repo_dir.exists():
            return
        log.info("Pulling latest...")
        try:
            subprocess.run(
                ["git", "checkout", "main"],
                capture_output=True, text=True, timeout=10,
                cwd=str(repo_dir),
            )
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30,
                cwd=str(repo_dir),
            )
            if result.returncode != 0:
                log.warning("git pull failed: %s", result.stderr.strip())
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("git pull failed: %s", e)

    def _dispatch_workers(self) -> bool:
        """Dispatch issue workers with backfill — new work starts as soon as a slot opens."""
        import concurrent.futures

        # Check if all accounts are in cooldown
        was_cooling = self.rotator.all_exhausted_at > 0
        if self.rotator.is_cooling_down():
            log.info("All accounts exhausted — in cooldown, skipping dispatch")
            return False
        if was_cooling:
            log.info("Cooldown ended — unsticking all issues")
            self._unstick_all_issues()

        # Pre-dispatch: warn if main CI is broken (workers will hit pre-existing failures)
        from . import github as gh_mod
        if not gh_mod.main_ci_healthy(self.cfg.nuc_repo):
            log.warning("Main branch CI is failing — workers may hit pre-existing lint/test failures")

        log.info("Checking for assigned:worker issues... [%s]", self.rotator.status)
        issues = get_dispatchable_issues(self.cfg)

        if not issues:
            return False

        # Build initial dispatch list respecting limits
        dispatch_list = self._apply_worker_limits(issues)
        if not dispatch_list:
            return False

        # Track what's currently in-flight to avoid double-dispatch
        in_flight: set[int] = set()
        quota_hit = False

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.cfg.max_workers + 1) as pool:
            worker_futures: dict[concurrent.futures.Future, tuple[str, int]] = {}

            # Submit initial batch
            for repo, num in dispatch_list:
                future = pool.submit(work_on_issue, self.cfg, repo, num)
                worker_futures[future] = (repo, num)
                in_flight.add(num)

            # Submit board checks in parallel
            board_future = pool.submit(self.board.run_all)

            # As workers complete, backfill from the queue
            while worker_futures:
                done, _ = concurrent.futures.wait(
                    worker_futures, timeout=30,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                # Ping watchdog while waiting
                if not done:
                    watchdog_ping()
                    continue

                for future in done:
                    repo, issue_num = worker_futures.pop(future)
                    in_flight.discard(issue_num)
                    issue_key = f"{repo}#{issue_num}"
                    try:
                        result = future.result()
                        if result == 0:
                            self._fail_counts.pop(issue_key, None)
                            self.rotator.on_success()
                        elif result == 2:
                            quota_hit = True
                        else:
                            self._track_failure(repo, issue_num)
                    except Exception:
                        log.exception("Worker failed for %s", issue_key)
                        self._track_failure(repo, issue_num)

                    # Reset idle gate since we found work
                    gate_file = self.cfg.state_dir / "pgm-signal-sent.tsv"
                    delete_tsv_entries(gate_file, r"^idle-0\t")

                # Backfill: check for new work if we have free slots
                if not quota_hit and len(worker_futures) < self.cfg.max_workers:
                    self._git_pull()
                    fresh_issues = get_dispatchable_issues(self.cfg)
                    if fresh_issues:
                        available = self._apply_worker_limits(
                            fresh_issues, exclude=in_flight,
                        )
                        slots = self.cfg.max_workers - len(worker_futures)
                        for repo, num in available[:slots]:
                            log.info("Backfill: dispatching %s#%d into free slot", repo, num)
                            future = pool.submit(work_on_issue, self.cfg, repo, num)
                            worker_futures[future] = (repo, num)
                            in_flight.add(num)

            # Wait for board
            try:
                board_future.result()
            except Exception:
                log.exception("Board checks failed")

        watchdog_ping()

        if quota_hit:
            rotated = self.rotator.on_quota_hit()
            if rotated:
                log.info("Quota hit — rotated to next account, retrying immediately")
                self._unstick_all_issues()
            else:
                log.info("Quota hit — all accounts exhausted, entering cooldown")

        return True

    def _apply_worker_limits(
        self,
        issues: list[tuple[str, int, bool]],
        exclude: set[int] | None = None,
    ) -> list[tuple[str, int]]:
        """Apply Vector/NUC worker limits, optionally excluding in-flight issues."""
        exclude = exclude or set()
        filtered = [(r, n, j) for r, n, j in issues if n not in exclude]
        vector_issues = [(r, n) for r, n, j in filtered if j]
        nuc_issues = [(r, n) for r, n, j in filtered if not j]
        capped_vector = vector_issues[:self.cfg.max_vector_workers]
        dispatch_list = (nuc_issues + capped_vector)[:self.cfg.max_workers]
        if dispatch_list:
            v_count = len([1 for r, n in dispatch_list if (r, n) in set(capped_vector)])
            log.info("Dispatching %d worker(s) (%d Vector, %d NUC)",
                     len(dispatch_list), v_count, len(dispatch_list) - v_count)
        return dispatch_list

    def _track_failure(self, repo: str, issue_num: int) -> None:
        """Track consecutive failures. Mark stuck after MAX_CONSECUTIVE_FAILURES."""
        from . import github as gh

        issue_key = f"{repo}#{issue_num}"
        self._fail_counts[issue_key] = self._fail_counts.get(issue_key, 0) + 1
        count = self._fail_counts[issue_key]
        log.warning("%s: consecutive failure %d/%d", issue_key, count, MAX_CONSECUTIVE_FAILURES)

        if count >= MAX_CONSECUTIVE_FAILURES:
            log.error("%s: hit %d consecutive failures — marking stuck", issue_key, count)
            gh.issue_edit_labels(repo, issue_num,
                                 add=["stuck"], remove=["assigned:worker"])
            gh.issue_comment(
                repo, issue_num,
                f"## 🤖 Agent Loop: Marked stuck after {count} consecutive failures\n\n"
                "Worker kept failing on this issue. Removing from dispatch to unblock other work.\n"
                "Re-add `assigned:worker` label to retry.",
            )
            self._fail_counts.pop(issue_key, None)

    def _unstick_all_issues(self) -> None:
        """Re-queue all stuck issues after quota recovery."""
        from . import github as gh

        repo = self.cfg.nuc_repo
        stuck_raw = gh.issue_list(repo, label="stuck", state="open")
        if not stuck_raw:
            return

        count = 0
        for line in stuck_raw.strip().splitlines():
            parts = line.split("\t")
            if not parts or not parts[0].isdigit():
                continue
            num = int(parts[0])
            gh.issue_edit_labels(repo, num, add=["assigned:worker"], remove=["stuck"])
            gh.issue_comment(
                repo, num,
                "## 🤖 Agent Loop: Quota recovered — re-queued\n\n"
                "LLM quota is available again. Re-adding to worker dispatch.",
            )
            count += 1
            self._fail_counts.pop(f"{repo}#{num}", None)

        if count:
            log.info("Unstuck %d issue(s) after quota recovery", count)
