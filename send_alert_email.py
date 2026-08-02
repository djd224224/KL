#!/usr/bin/env python3
"""Send a one-off alert email using the ALERT_EMAIL_* env vars (the same
ones bq_retention_guard.py uses). Recipient falls back to the sender when
ALERT_EMAIL_TO is unset.

Usage: python send_alert_email.py SUBJECT [BODY]
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: send_alert_email.py SUBJECT [BODY]")
    frm = os.environ.get("ALERT_EMAIL_FROM", "")
    pw = os.environ.get("ALERT_EMAIL_PASSWORD", "")
    to = os.environ.get("ALERT_EMAIL_TO", "") or frm
    if not frm or not pw:
        sys.exit("ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
    msg = MIMEText(sys.argv[2] if len(sys.argv) > 2 else "")
    msg["Subject"] = sys.argv[1]
    msg["From"] = frm
    msg["To"] = to
    recipients = [r.strip() for r in to.split(",") if r.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(frm, pw)
        server.sendmail(frm, recipients, msg.as_string())
    print(f"alert sent to {to}")


if __name__ == "__main__":
    main()
