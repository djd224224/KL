"""Tests for send_html_email.wrap_body_for_email: Gmail drops <body> styles,
so a dark digest page must carry its body rule on a wrapping div."""
import unittest

from send_html_email import wrap_body_for_email

PAGE = """<!DOCTYPE html>
<html><head><style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #0f1117;
         color: #e2e8f0; padding: 32px; }
  tbody td { padding: 9px 12px; }
</style></head>
<body class="x">
<h1>High-Temp Digest</h1>
</body></html>"""


class WrapBodyTest(unittest.TestCase):
    def test_body_rule_moves_onto_a_div_inside_body(self):
        out = wrap_body_for_email(PAGE)
        self.assertIn('<body class="x"><div style="font-family: -apple-system, '
                      "'Segoe UI', sans-serif; background: #0f1117; "
                      'color: #e2e8f0; padding: 32px;">', out)
        self.assertIn("<h1>High-Temp Digest</h1>\n</div></body></html>", out)

    def test_tbody_rule_is_not_mistaken_for_body(self):
        page = PAGE.replace("  body {", "  main {")
        self.assertIn('<div style="">', wrap_body_for_email(page))

    def test_page_without_body_tag_is_wrapped_whole(self):
        out = wrap_body_for_email("<p>hi</p>")
        self.assertEqual(out, '<div style=""><p>hi</p></div>')


if __name__ == "__main__":
    unittest.main()
