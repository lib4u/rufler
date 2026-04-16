"""Tests for rufler.tasks.chain — compression, retrospective building, and
chain flag resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from rufler.tasks.chain import (
    ChainedTask,
    build_retrospective,
    collect_chain_entry,
    compress_task_context,
    resolve_chain_flag,
)


class TestCompressTaskContext:
    def test_empty(self):
        assert compress_task_context("") == ""
        assert compress_task_context(None) == ""

    def test_strips_html(self):
        assert compress_task_context("<b>hello</b>") == "hello"

    def test_strips_horizontal_rules(self):
        out = compress_task_context("before\n---\nafter")
        assert "---" not in out
        assert "before" in out
        assert "after" in out

    def test_collapses_blank_lines(self):
        out = compress_task_context("a\n\n\n\n\nb")
        assert "\n\n\n" not in out
        assert "a" in out and "b" in out

    def test_flattens_code_blocks(self):
        text = "```python\ndef hello():\n    pass\n```"
        out = compress_task_context(text)
        assert "```" not in out
        assert "[code:python:" in out
        assert "def hello()" in out

    def test_downgrades_headers(self):
        out = compress_task_context("## My Header")
        assert "##" not in out
        assert "[My Header]" in out

    def test_removes_bold_italic(self):
        out = compress_task_context("**bold** and *italic*")
        assert "**" not in out
        assert "*" not in out
        assert "bold" in out
        assert "italic" in out

    def test_truncates_to_max_tokens(self):
        text = " ".join(f"word{i}" for i in range(500))
        out = compress_task_context(text, max_tokens=10)
        words = out.replace("[… truncated]", "").split()
        assert len(words) <= 11  # 10 words + possible truncation marker

    def test_no_truncation_when_under_limit(self):
        text = "short text here"
        out = compress_task_context(text, max_tokens=100)
        assert "[… truncated]" not in out


class TestBuildRetrospective:
    def test_empty_history(self):
        assert build_retrospective([]) == ""

    def test_single_completed_task(self):
        ct = ChainedTask(
            name="design", slot=1, total=3,
            body_compressed="Design the API.", report_compressed="Done.",
            rc=0,
        )
        out = build_retrospective([ct])
        assert "PREVIOUS TASK RETROSPECTIVE" in out
        assert "[1/3] design" in out
        assert "completed" in out
        assert "Design the API." in out
        assert "Report (design):" in out
        assert "Done." in out

    def test_failed_task(self):
        ct = ChainedTask(
            name="build", slot=2, total=3,
            body_compressed="Build step.", report_compressed="",
            rc=1,
        )
        out = build_retrospective([ct])
        assert "failed (rc=1)" in out

    def test_no_report(self):
        ct = ChainedTask(
            name="x", slot=1, total=1,
            body_compressed="Body.", report_compressed="",
            rc=0,
        )
        out = build_retrospective([ct])
        assert "Report" not in out

    def test_multiple_tasks_ordered(self):
        history = [
            ChainedTask("a", 1, 3, "body-a", "", 0),
            ChainedTask("b", 2, 3, "body-b", "report-b", 0),
        ]
        out = build_retrospective(history)
        assert out.index("[1/3] a") < out.index("[2/3] b")


class TestCollectChainEntry:
    def test_basic(self):
        ct = collect_chain_entry(
            name="task1", slot=1, total=2,
            body="## Header\n\n**Bold** text\n\n\n\nextra lines",
            report_path=None, rc=0, max_tokens=500,
        )
        assert ct.name == "task1"
        assert ct.rc == 0
        assert "##" not in ct.body_compressed
        assert "**" not in ct.body_compressed
        assert ct.report_compressed == ""

    def test_with_report_file(self, tmp_path):
        report = tmp_path / "report.md"
        report.write_text("## Report\n\nEverything is fine.\n")
        ct = collect_chain_entry(
            name="task1", slot=1, total=1,
            body="body", report_path=report, rc=0, max_tokens=500,
        )
        assert "Everything is fine" in ct.report_compressed

    def test_missing_report_file(self, tmp_path):
        ct = collect_chain_entry(
            name="task1", slot=1, total=1,
            body="body", report_path=tmp_path / "missing.md",
            rc=0, max_tokens=500,
        )
        assert ct.report_compressed == ""

    def test_budget_split(self):
        long_body = " ".join(f"w{i}" for i in range(5000))
        ct = collect_chain_entry(
            name="t", slot=1, total=1,
            body=long_body, report_path=None, rc=0, max_tokens=500,
        )
        word_count = len(ct.body_compressed.split())
        # body gets ~2/3 of budget (report_budget = 500//3 = 166, body = 334)
        assert word_count <= 400

    def test_small_budget(self):
        long_body = " ".join(f"w{i}" for i in range(5000))
        ct = collect_chain_entry(
            name="t", slot=1, total=1,
            body=long_body, report_path=None, rc=0, max_tokens=50,
        )
        assert "[… truncated]" in ct.body_compressed


class TestResolveChainFlag:
    def test_global_true_no_override(self):
        class FakeSpec:
            chain = True
        assert resolve_chain_flag(FakeSpec(), None) is True

    def test_global_false_no_override(self):
        class FakeSpec:
            chain = False
        assert resolve_chain_flag(FakeSpec(), None) is False

    def test_item_override_true(self):
        class FakeSpec:
            chain = False
        assert resolve_chain_flag(FakeSpec(), True) is True

    def test_item_override_false(self):
        class FakeSpec:
            chain = True
        assert resolve_chain_flag(FakeSpec(), False) is False
