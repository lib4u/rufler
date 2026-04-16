"""Tests for rufler.tasks.deep_think — prompt building and integration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rufler.tasks.deep_think import build_deep_think_prompt, deep_think


class TestBuildDeepThinkPrompt:
    def test_default_prompt_includes_task(self):
        out = build_deep_think_prompt("Build a REST API")
        assert "Build a REST API" in out
        assert "TASK TO ANALYZE" in out
        assert "Project Overview" in out

    def test_custom_template_with_placeholder(self):
        tpl = "Analyze this: {main}\nDone."
        out = build_deep_think_prompt("my task", template=tpl)
        assert out == "Analyze this: my task\nDone."

    def test_custom_template_without_placeholder(self):
        tpl = "Just analyze the project."
        out = build_deep_think_prompt("my task", template=tpl)
        assert "my task" in out
        assert "TASK TO ANALYZE" in out

    def test_strips_main_task(self):
        out = build_deep_think_prompt("  \n  spaced task  \n  ")
        assert "spaced task" in out


class TestDeepThink:
    def test_missing_claude_binary(self, tmp_path):
        with patch("rufler.tasks.deep_think.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="claude.*not found"):
                deep_think("task", tmp_path / "out.md")

    def test_successful_run(self, tmp_path):
        output = tmp_path / "analysis.md"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "# Analysis\n\nProject looks good."

        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run", return_value=mock_result) as mock_run:
            result = deep_think("Build API", output, model="opus")

        assert "Project looks good" in result
        assert output.exists()
        assert "Project looks good" in output.read_text()
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "opus" in cmd
        # By default --allowedTools is NOT passed (all tools available)
        assert "--allowedTools" not in cmd

    def test_failed_run(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error: something went wrong"

        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="deep_think failed"):
                deep_think("task", tmp_path / "out.md")

    def test_empty_output(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   "

        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="empty output"):
                deep_think("task", tmp_path / "out.md")

    def test_timeout(self, tmp_path):
        import subprocess
        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
            with pytest.raises(RuntimeError, match="timed out"):
                deep_think("task", tmp_path / "out.md", timeout=60)

    def test_allowed_tools_passed(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Analysis output"

        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run", return_value=mock_result) as mock_run:
            deep_think("task", tmp_path / "out.md", allowed_tools="Read,Glob")

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Read,Glob"

    def test_allowed_tools_with_mcp(self, tmp_path):
        """MCP tool names are passed through to --allowedTools."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Analysis output"
        tools = "Read,Glob,Grep,Bash,mcp__claude-flow__memory_search"

        with patch("rufler.tasks.deep_think.shutil.which", return_value="/usr/bin/claude"), \
             patch("rufler.tasks.deep_think.subprocess.run", return_value=mock_result) as mock_run:
            deep_think("task", tmp_path / "out.md", allowed_tools=tools)

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == tools
