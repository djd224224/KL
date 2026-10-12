#!/usr/bin/env python3
"""Email an HTML page (a run-logs digest) through the ALERT_EMAIL_* account
that send_alert_email.py uses. Recipient falls back to the sender when
ALERT_EMAIL_TO is unset.

Gmail drops the <body> tag and its styles, so a dark page would show its
light text on white. The page's body is therefore wrapped in a div that
carries the body rule from its <style> block inline. The original file is
attached as well, for the full page in a browser.

Usage: python send_html_email.py SUBJECT HTML_FILE [TEXT]
"""
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_BODY_RULE = re.compile(r"(?<![\w.#-])body\s*\{([^}]*)\}", re.I)
_BODY_OPEN = re.compile(r"<body\b[^>]*>", re.I)
_BODY_CLOSE = re.compile(r"</body\s*>", re.I)


def wrap_body_for_email(page: str) -> str:
    """Put the page's body content in a div styled like its body rule."""
    rule = _BODY_RULE.search(page)
    style = " ".join(rule.group(1).split()).replace('"', "'") if rule else ""
    open_tag = _BODY_OPEN.search(page)
    close_tag = _BODY_CLOSE.search(page)
    if not open_tag or not close_tag or close_tag.start() < open_tag.end():
        return f'<div style="{style}">{page}</div>'
    inner = page[open_tag.end():close_tag.start()]
    return (page[:open_tag.end()] + f'<div style="{style}">' + inner
            + "</div>" + page[close_tag.start():])


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: send_html_email.py SUBJECT HTML_FILE [TEXT]")
    subject, path = sys.argv[1], sys.argv[2]
    text = sys.argv[3] if len(sys.argv) > 3 else ""
    frm = os.environ.get("ALERT_EMAIL_FROM", "")
    pw = os.environ.get("ALERT_EMAIL_PASSWORD", "")
    to = os.environ.get("ALERT_EMAIL_TO", "") or frm
    if not frm or not pw:
        sys.exit("ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
    with open(path, encoding="utf-8") as f:
        page = f.read()

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text or subject, "plain", "utf-8"))
    body.attach(MIMEText(wrap_body_for_email(page), "html", "utf-8"))
    msg = MIMEMultipart("mixed")
    msg.attach(body)
    attachment = MIMEText(page, "html", "utf-8")
    attachment.add_header("Content-Disposition", "attachment",
                          filename=os.path.basename(path))
    msg.attach(attachment)
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    recipients = [r.strip() for r in to.split(",") if r.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(frm, pw)
        server.sendmail(frm, recipients, msg.as_string())
    print(f"email sent to {to}")


if __name__ == "__main__":
    main()
