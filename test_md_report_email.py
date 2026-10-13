"""Tests for md_report_email.py: an unattended report job's Markdown answer
rendered for Gmail (no Markdown package on this box)."""
import os
import tempfile
import unittest

import md_report_email as M
from send_html_email import wrap_body_for_email


class RenderTest(unittest.TestCase):
    def test_table_keeps_alignment_and_inline_marks(self):
        out = M.render("| market | P&L |\n|---|---:|\n| **KXCPICORE-26NOV** | -$1.20 |")
        self.assertIn("<thead><tr><th", out)
        self.assertIn(">P&amp;L</th>", out)
        self.assertIn('text-align:right">-$1.20</td>', out)
        self.assertIn("<b>KXCPICORE-26NOV</b>", out)
        self.assertNotIn("|", out)

    def test_empty_header_row_is_dropped(self):
        # imm_cpi_pilot_report.py's "Money" table
        out = M.render("| | |\n|---|---|\n| Rewards | $1.00 |")
        self.assertNotIn("<th", out)
        self.assertIn(">Rewards</td>", out)

    def test_short_rows_are_padded(self):
        out = M.render("| a | b | c |\n|---|---|---|\n| 1 |")
        self.assertEqual(out.count("<td"), 3)

    def test_escapes_html_and_keeps_line_breaks(self):
        out = M.render("Net <$5> & rising\nsecond line")
        self.assertIn("Net &lt;$5&gt; &amp; rising<br>second line", out)

    def test_lists_headings_rules_and_code(self):
        out = M.render("# Title\n- one\n- two *x*\n  wrapped\n\n1. first\n2. second\n---\n"
                       "Saved: `run-logs\\incentive-mm\\a.md`")
        self.assertIn(">Title</h1>", out)
        self.assertIn(">two <i>x</i> wrapped</li></ul>", out)
        self.assertIn(">second</li></ol>", out)
        self.assertIn("<hr", out)
        self.assertIn(">run-logs\\incentive-mm\\a.md</code>", out)

    def test_bold_line_is_not_a_bullet_and_arithmetic_is_not_italics(self):
        out = M.render("**Bottom line: keep.** 2 * 3 * 4")
        self.assertIn("<p", out)
        self.assertIn("<b>Bottom line: keep.</b> 2 * 3 * 4", out)
        self.assertNotIn("<li", out)

    def test_fenced_code_is_escaped_verbatim(self):
        out = M.render("```\n| not | a table |\n<b>\n```\nafter")
        self.assertIn("| not | a table |\n&lt;b&gt;</pre>", out)
        self.assertIn(">after</p>", out)


class PageTest(unittest.TestCase):
    def test_main_writes_one_page_with_a_rule_between_files(self):
        with tempfile.TemporaryDirectory() as d:
            a, b, out = (os.path.join(d, n) for n in ("a.md", "b.md", "o.html"))
            with open(a, "w", encoding="utf-8-sig") as fh:      # PowerShell's BOM
                fh.write("# A\n")
            with open(b, "w", encoding="utf-8") as fh:
                fh.write("# B \u00a2\n")
            M.main([out, "T & U", a, b])
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("<title>T &amp; U</title>", html)
        self.assertNotIn("\ufeff", html)
        self.assertLess(html.index(">A</h1>"), html.index("<hr"))
        self.assertLess(html.index("<hr"), html.index(">B \u00a2</h1>"))

    def test_send_html_email_finds_the_body_rule(self):
        wrapped = wrap_body_for_email(M.page("t", ["<p>x</p>"]))
        self.assertIn("<body><div style=\"font-family: -apple-system, 'Segoe UI'", wrapped)


if __name__ == "__main__":
    unittest.main()
