#!/usr/bin/env python3
"""Tests for brief_lint.py. Run: python3 -m unittest test_brief_lint -v"""
import tempfile
import unittest
from pathlib import Path

import brief_lint as bl


class TestBriefLint(unittest.TestCase):
    def test_artifact_brief_missing_line_fails(self):
        ok, reason = bl.lint_text("Build a dashboard of Q3 spend by category.")
        self.assertFalse(ok)
        self.assertIn("missing", reason)

    def test_artifact_brief_with_line_passes(self):
        ok, _ = bl.lint_text("Build a dashboard.\n\n" + bl.STANDING_LINE)
        self.assertTrue(ok)

    def test_dash_and_whitespace_drift_still_passes(self):
        drifted = bl.STANDING_LINE.replace(" — ", "--").replace("; ", ";\n  ")
        ok, _ = bl.lint_text("Make a tracker page.\n" + drifted)
        self.assertTrue(ok)

    def test_plain_coding_brief_out_of_scope(self):
        ok, reason = bl.lint_text("Fix the flaky retry test in governor.py.")
        self.assertTrue(ok)
        self.assertIn("out of scope", reason)

    def test_artifact_scope_no_longer_exempts(self):
        # The artifact_scope exemption was stripped (Nov 26, 2026):
        # a brief showing artifact signals is in scope regardless.
        text = "---\nartifact_scope: runbook\n---\nWrite a runbook artifact for HA restores."
        ok, reason = bl.lint_text(text)
        self.assertFalse(ok)
        self.assertIn("missing", reason)

    def test_output_type_overrides_keywords(self):
        text = "---\noutput_type: code\n---\nRefactor the UI layer."
        self.assertTrue(bl.lint_text(text)[0])
        text2 = "---\noutput_type: dashboard\n---\nSummarize spend."
        self.assertFalse(bl.lint_text(text2)[0])

    def test_force_flags(self):
        self.assertFalse(bl.lint_text("Fix a bug.", force=True)[0])
        self.assertTrue(bl.lint_text("Build a dashboard.", force=False)[0])

    def test_fix_appends_after_body_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "brief.md"
            p.write_text("User style: dark mode, big numbers.\nBuild a wishlist page.\n", encoding="utf-8")
            self.assertTrue(bl.fix_file(p))
            out = p.read_text(encoding="utf-8")
            self.assertLess(out.index("User style"), out.index("Standing instruction from the user"))
            self.assertTrue(bl.lint_file(p)[0])
            self.assertFalse(bl.fix_file(p))  # second run changes nothing

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.md"
            bad.write_text("Build a dashboard.", encoding="utf-8")
            good = Path(d) / "good.md"
            good.write_text("Fix a bug.", encoding="utf-8")
            self.assertEqual(bl.main([str(good), "--quiet"]), 0)
            self.assertEqual(bl.main([str(bad), "--quiet"]), 1)
            self.assertEqual(bl.main([str(bad), "--fix", "--quiet"]), 0)
            self.assertEqual(bl.main([str(Path(d) / "nope.md")]), 2)


if __name__ == "__main__":
    unittest.main()
