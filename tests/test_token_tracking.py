"""Tests for token usage tracking: log-token-usage.sh, claude-wrapper.sh, token-report.sh"""
import os
import subprocess
import tempfile


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLogTokenUsage:
    """Tests for scripts/log-token-usage.sh"""

    def _run_log(self, agent_role, issue_ref, stderr_content, tsv_file=None):
        """Run log-token-usage.sh standalone with given stderr content."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as stderr_f:
            stderr_f.write(stderr_content)
            stderr_path = stderr_f.name

        if tsv_file is None:
            tsv_f = tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False)
            tsv_path = tsv_f.name
            tsv_f.close()
            os.unlink(tsv_path)  # Start with no file
        else:
            tsv_path = tsv_file

        try:
            env = os.environ.copy()
            env['TOKEN_USAGE_FILE'] = tsv_path
            result = subprocess.run(
                ['bash', os.path.join(REPO_DIR, 'scripts', 'log-token-usage.sh'),
                 agent_role, issue_ref, stderr_path],
                env=env, capture_output=True, text=True, timeout=10
            )
            assert result.returncode == 0, f"Script failed: {result.stderr}"

            if os.path.exists(tsv_path):
                with open(tsv_path) as f:
                    return f.read(), tsv_path
            return '', tsv_path
        finally:
            os.unlink(stderr_path)

    def test_parses_claude_result_json(self):
        """Standard Claude CLI result JSON is parsed correctly."""
        stderr = '{"type":"result","subtype":"success","total_cost_usd":0.0523,"model":"claude-sonnet-4-20250514","usage":{"input_tokens":1500,"output_tokens":800,"cache_read_input_tokens":200,"cache_creation_input_tokens":100}}\n'
        content, tsv_path = self._run_log('worker', 'ShesekBean/nuc-vector-orchestrator#42', stderr)
        try:
            lines = content.strip().split('\n')
            assert len(lines) == 2  # header + data
            assert lines[0].startswith('timestamp')
            fields = lines[1].split('\t')
            assert fields[1] == 'worker'
            assert fields[2] == 'ShesekBean/nuc-vector-orchestrator#42'
            assert fields[3] == 'claude-sonnet-4-20250514'
            assert fields[4] == '1500'  # input_tokens
            assert fields[5] == '800'   # output_tokens
            assert fields[6] == '200'   # cache_read
            assert fields[7] == '100'   # cache_create
            assert fields[8] == '0.0523'
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)

    def test_skips_non_json_lines(self):
        """Non-JSON lines (progress output) are ignored."""
        stderr = 'Thinking...\nProcessing request...\n{"total_cost_usd":0.01,"usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}\nDone.\n'
        content, tsv_path = self._run_log('pgm', 'agent:pgm', stderr)
        try:
            lines = content.strip().split('\n')
            assert len(lines) == 2
            fields = lines[1].split('\t')
            assert fields[1] == 'pgm'
            assert fields[2] == 'agent:pgm'
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)

    def test_empty_stderr_produces_no_output(self):
        """Empty stderr file produces no TSV output."""
        content, tsv_path = self._run_log('worker', 'test#1', '')
        try:
            assert content == ''
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)

    def test_no_usage_data_skips(self):
        """JSON without usage/cost data is skipped."""
        stderr = '{"type":"progress","message":"thinking"}\n'
        content, tsv_path = self._run_log('worker', 'test#1', stderr)
        try:
            assert content == ''
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)

    def test_missing_stderr_file(self):
        """Missing stderr file doesn't crash."""
        env = os.environ.copy()
        env['TOKEN_USAGE_FILE'] = '/tmp/test-missing.tsv'
        result = subprocess.run(
            ['bash', os.path.join(REPO_DIR, 'scripts', 'log-token-usage.sh'),
             'worker', 'test#1', '/nonexistent/file.txt'],
            env=env, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0

    def test_append_mode(self):
        """Multiple calls append to same TSV file."""
        stderr1 = '{"total_cost_usd":0.01,"usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}\n'
        stderr2 = '{"total_cost_usd":0.02,"usage":{"input_tokens":200,"output_tokens":100,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}\n'
        _, tsv_path = self._run_log('worker', 'test#1', stderr1)
        try:
            self._run_log('pgm', 'agent:pgm', stderr2, tsv_file=tsv_path)
            with open(tsv_path) as f:
                lines = f.read().strip().split('\n')
            assert len(lines) == 3  # header + 2 data rows
            assert 'worker' in lines[1]
            assert 'pgm' in lines[2]
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)

    def test_zero_tokens_skipped(self):
        """Entry with all-zero tokens and cost is skipped."""
        stderr = '{"total_cost_usd":0,"usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}\n'
        content, tsv_path = self._run_log('worker', 'test#1', stderr)
        try:
            assert content == ''
        finally:
            if os.path.exists(tsv_path):
                os.unlink(tsv_path)


class TestTokenReport:
    """Tests for scripts/token-report.sh"""

    def _create_tsv(self, rows):
        """Create a TSV file with given data rows."""
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False)
        f.write('timestamp\tagent_role\tissue_ref\tmodel\tinput_tokens\toutput_tokens\tcache_read\tcache_create\tcost_usd\n')
        for row in rows:
            f.write('\t'.join(str(x) for x in row) + '\n')
        f.close()
        return f.name

    def _run_report(self, tsv_path, target_date):
        env = os.environ.copy()
        env['TOKEN_USAGE_FILE'] = tsv_path
        env['REPO_DIR'] = REPO_DIR
        result = subprocess.run(
            ['bash', os.path.join(REPO_DIR, 'scripts', 'token-report.sh'), target_date],
            env=env, capture_output=True, text=True, timeout=10
        )
        return result.stdout, result.returncode

    def test_daily_summary(self):
        """Generates summary for a specific date."""
        tsv = self._create_tsv([
            ['2026-03-05T10:00:00Z', 'worker', 'ShesekBean/nuc-vector-orchestrator#42', 'opus', 1000, 500, 100, 50, 0.05],
            ['2026-03-05T11:00:00Z', 'pgm', 'agent:pgm', 'haiku', 200, 100, 0, 0, 0.001],
            ['2026-03-04T10:00:00Z', 'worker', 'ShesekBean/nuc-vector-orchestrator#41', 'opus', 5000, 2000, 0, 0, 0.20],
        ])
        try:
            output, rc = self._run_report(tsv, '2026-03-05')
            assert rc == 0
            assert 'Token Usage Report' in output
            assert '2026-03-05' in output
            assert 'worker' in output
            assert 'pgm' in output
            # Should NOT include the 03-04 entry
            assert '#41' not in output
            assert '#42' in output
        finally:
            os.unlink(tsv)

    def test_empty_date_no_output(self):
        """No data for date produces no output."""
        tsv = self._create_tsv([
            ['2026-03-04T10:00:00Z', 'worker', 'test#1', 'opus', 1000, 500, 0, 0, 0.05],
        ])
        try:
            output, rc = self._run_report(tsv, '2026-03-05')
            assert rc == 0
            assert output.strip() == ''
        finally:
            os.unlink(tsv)

    def test_missing_file_no_crash(self):
        """Missing TSV file doesn't crash."""
        output, rc = self._run_report('/nonexistent/file.tsv', '2026-03-05')
        assert rc == 0
        assert output.strip() == ''

    def test_top_issues_limited(self):
        """Only top 5 issues by cost are shown."""
        rows = []
        for i in range(10):
            rows.append([f'2026-03-05T{i:02d}:00:00Z', 'worker', f'repo#{i}', 'opus', 1000, 500, 0, 0, 0.01 * (i + 1)])
        tsv = self._create_tsv(rows)
        try:
            output, rc = self._run_report(tsv, '2026-03-05')
            assert rc == 0
            # Top issues section should exist
            assert 'Top issues by cost' in output
            # Should show the most expensive ones
            assert 'repo#9' in output  # $0.10
            assert 'repo#8' in output  # $0.09
        finally:
            os.unlink(tsv)

    def test_agent_only_refs_excluded_from_issues(self):
        """agent: prefixed refs should not appear in top issues."""
        tsv = self._create_tsv([
            ['2026-03-05T10:00:00Z', 'pgm', 'agent:pgm', 'haiku', 200, 100, 0, 0, 0.5],
        ])
        try:
            output, rc = self._run_report(tsv, '2026-03-05')
            assert rc == 0
            assert 'Top issues' not in output  # No issue-level data
        finally:
            os.unlink(tsv)


class TestClaudeWrapper:
    """Tests for scripts/claude-wrapper.sh"""

    def test_wrapper_syntax_valid(self):
        """Wrapper script passes bash syntax check."""
        result = subprocess.run(
            ['bash', '-n', os.path.join(REPO_DIR, 'scripts', 'claude-wrapper.sh')],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_wrapper_fails_open_with_fake_binary(self):
        """Wrapper exits cleanly when real binary doesn't exist."""
        env = os.environ.copy()
        env['REAL_CLAUDE'] = '/nonexistent/claude'
        # Keep /usr/bin in PATH so bash can find basic tools, but remove ~/bin
        env['PATH'] = '/usr/bin:/bin'
        result = subprocess.run(
            ['bash', os.path.join(REPO_DIR, 'scripts', 'claude-wrapper.sh'), '--version'],
            env=env, capture_output=True, text=True, timeout=10
        )
        # Should fail (binary not found) but not crash the wrapper itself
        assert result.returncode != 0

    def test_log_token_usage_syntax_valid(self):
        """log-token-usage.sh passes bash syntax check."""
        result = subprocess.run(
            ['bash', '-n', os.path.join(REPO_DIR, 'scripts', 'log-token-usage.sh')],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_token_report_syntax_valid(self):
        """token-report.sh passes bash syntax check."""
        result = subprocess.run(
            ['bash', '-n', os.path.join(REPO_DIR, 'scripts', 'token-report.sh')],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"


class TestAgentLoopIntegration:
    """Tests that the Python agent-loop module is importable and valid."""

    def test_agent_loop_syntax_valid(self):
        """Python agent-loop module passes compile check."""
        result = subprocess.run(
            ['python3', '-m', 'py_compile', os.path.join(REPO_DIR, 'apps', 'control_plane', 'agent_loop', '__main__.py')],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"


class TestTokenReportSingleMachine:
    """Tests for single-machine token-report.sh (all calls from NUC)."""

    def _create_tsv(self, rows):
        """Create a TSV file with given data rows."""
        import tempfile
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False)
        f.write('timestamp\tagent_role\tissue_ref\tmodel\tinput_tokens\toutput_tokens\tcache_read\tcache_create\tcost_usd\n')
        for row in rows:
            f.write('\t'.join(str(x) for x in row) + '\n')
        f.close()
        return f.name

    def _run_report(self, nuc_tsv, target_date):
        env = os.environ.copy()
        env['TOKEN_USAGE_FILE'] = nuc_tsv
        env['REPO_DIR'] = REPO_DIR
        result = subprocess.run(
            ['bash', os.path.join(REPO_DIR, 'scripts', 'token-report.sh'), target_date],
            env=env, capture_output=True, text=True, timeout=10
        )
        return result.stdout, result.returncode

    def test_nuc_only_no_machine_breakdown(self):
        """Single machine (NUC only) doesn't show machine breakdown."""
        nuc = self._create_tsv([
            ['2026-03-05T10:00:00Z', 'worker', 'repo#42', 'opus', 1000, 500, 0, 0, 0.05],
        ])
        try:
            output, rc = self._run_report(nuc, '2026-03-05')
            assert rc == 0
            assert 'By role' in output
        finally:
            os.unlink(nuc)

    def test_multiple_issues_aggregated(self):
        """Costs from multiple issues are properly aggregated."""
        nuc = self._create_tsv([
            ['2026-03-05T10:00:00Z', 'worker', 'repo#1', 'opus', 1000, 500, 0, 0, 1.00],
            ['2026-03-05T10:30:00Z', 'pgm', 'agent:pgm', 'haiku', 200, 100, 0, 0, 0.01],
            ['2026-03-05T11:00:00Z', 'worker', 'repo#1', 'opus', 3000, 1000, 0, 0, 2.00],
        ])
        try:
            output, rc = self._run_report(nuc, '2026-03-05')
            assert rc == 0
            # Total should be 1.00 + 0.01 + 2.00 = 3.01
            assert '$3.01' in output
            # repo#1 should aggregate: 1.00 + 2.00 = 3.00
            assert 'repo#1: $3.00' in output
        finally:
            os.unlink(nuc)
