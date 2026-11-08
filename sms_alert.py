#!/usr/bin/env python3
"""Text Jack through his carrier's email-to-SMS gateway, RATE-CAPPED.

Why the cap (Jack 2026-11-07: "how to fix the text gateway"): from ~2026-06-29
T-Mobile's gateway filter (tmo-*.mx.a.cloudfilter.net) answered every message
from jackdu224@gmail.com with "452 4.1.0 <jackdu224@gmail.com> server
temporarily unavailable AUP#MXRT" -- an acceptable-use block on the SENDER,
after the bots sent 200+ texts in the 9 days before. bcc4bee (7/3) moved every
bot's alerts to the Gmail inbox. This sends texts again, but:
  * at most one text per SMS_MIN_GAP_SECS (600) and SMS_MAX_PER_DAY (10) a
    rolling day -- a capped text is dropped and logged, never queued;
  * body only, no subject, <= 160 characters;
  * from a DEDICATED sender when ALERT_SMS_FROM / ALERT_SMS_PASSWORD are set (a
    Gmail used only for texts, so a block can't touch the alert inbox),
    else from ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD.
Settings come from the environment, else HKCU\\Environment (never printed).
Gmail accepts the message at once; a carrier refusal only comes back later as a
mailer-daemon bounce to the sender, so "sent" means handed to Gmail.

    python sms_alert.py "Wave w1108-x listed: 120 mkts"     # capped
    python sms_alert.py "test" --force                      # skip the cap (tests)
    python sms_alert.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "run-logs", "sms_alert_state.json")
LOG = os.path.join(HERE, "run-logs", "sms_alert.log")
DEFAULT_TO = "9729713381@tmomail.net"
MIN_GAP = float(os.environ.get("SMS_MIN_GAP_SECS", "600"))
MAX_PER_DAY = int(os.environ.get("SMS_MAX_PER_DAY", "10"))
MAX_CHARS = 160
LONG_CHARS = 900          # --long (wave_alert.py, Jack 2026-11-08: "... 10 names")
# --reply: Claude's answers to Jack's texts (sms_inbox.py, Jack 2026-11-08: "can i set it up so
# if i text back and it goes to Claude?"). Their own caps, so a conversation isn't held to the
# alerts' 10-minute gap, and a ceiling on ALL texts a day: the June block came at ~22 a day.
REPLY_MIN_GAP = float(os.environ.get("SMS_REPLY_MIN_GAP_SECS", "20"))
REPLY_MAX_PER_DAY = int(os.environ.get("SMS_REPLY_MAX_PER_DAY", "15"))
TOTAL_MAX_PER_DAY = int(os.environ.get("SMS_TOTAL_MAX_PER_DAY", "25"))


def setting(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0] or "")
    except (OSError, ImportError):
        return ""


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"sent": []}


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?")
    ap.add_argument("--force", action="store_true", help="skip the rate cap")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--long", action="store_true",
                    help=f"keep line breaks, up to {LONG_CHARS} chars (T-Mobile delivers it as one MMS): "
                         "the wave alerts' names list (wave_alert.py)")
    ap.add_argument("--reply", action="store_true",
                    help="an answer to Jack's text (sms_inbox.py): the reply caps, line breaks kept")
    a = ap.parse_args()
    now = time.time()
    st = load_state()
    st["sent"] = [t for t in st.get("sent") or [] if t > now - 86400]
    st["replies"] = [t for t in st.get("replies") or [] if t > now - 86400]
    dedicated = bool(setting("ALERT_SMS_FROM") and setting("ALERT_SMS_PASSWORD"))
    if a.status:
        last = max(st["sent"]) if st["sent"] else None
        print(f"sender: {'dedicated (ALERT_SMS_FROM)' if dedicated else 'alert account (ALERT_EMAIL_FROM)'}; "
              f"to: {setting('ALERT_SMS_TO') or DEFAULT_TO}; sent last 24h: {len(st['sent'])}/{MAX_PER_DAY} alerts, "
              f"{len(st['replies'])}/{REPLY_MAX_PER_DAY} replies (all texts cap {TOTAL_MAX_PER_DAY}); "
              f"last: {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(last)) if last else 'never'}")
        return 0
    if not a.text:
        ap.error("text required")
    if a.long or a.reply:
        body = "\n".join(" ".join(ln.split()) for ln in a.text.splitlines() if ln.strip())[:LONG_CHARS]
    else:
        body = " ".join(a.text.split())[:MAX_CHARS]
    total = len(st["sent"]) + len(st["replies"])
    if not a.force and a.reply:
        if st["replies"] and now - max(st["replies"]) < REPLY_MIN_GAP:
            log(f"CAPPED reply (gap {now - max(st['replies']):.0f}s < {REPLY_MIN_GAP:.0f}s): {body[:60]}")
            print("capped: too soon after the last reply")
            return 2
        if len(st["replies"]) >= REPLY_MAX_PER_DAY or total >= TOTAL_MAX_PER_DAY:
            log(f"CAPPED reply ({len(st['replies'])} replies, {total} texts in 24h): {body[:60]}")
            print("capped: daily reply limit")
            return 2
    elif not a.force:
        if total >= TOTAL_MAX_PER_DAY:
            log(f"CAPPED ({total} texts in 24h): {body[:60]}")
            print("capped: daily limit (all texts)")
            return 2
        if st["sent"] and now - max(st["sent"]) < MIN_GAP:
            log(f"CAPPED (gap {now - max(st['sent']):.0f}s < {MIN_GAP:.0f}s): {body[:60]}")
            print("capped: too soon after the last text")
            return 2
        if len(st["sent"]) >= MAX_PER_DAY:
            log(f"CAPPED ({len(st['sent'])} in 24h): {body[:60]}")
            print("capped: daily limit")
            return 2
    frm = setting("ALERT_SMS_FROM") if dedicated else setting("ALERT_EMAIL_FROM")
    pw = setting("ALERT_SMS_PASSWORD") if dedicated else setting("ALERT_EMAIL_PASSWORD")
    to = setting("ALERT_SMS_TO") or DEFAULT_TO
    if not frm or not pw:
        print("no sender configured (ALERT_SMS_FROM/PASSWORD or ALERT_EMAIL_FROM/PASSWORD)")
        return 1
    if a.dry_run:
        print(f"dry run: would text {to.split('@')[-1]} ({len(body)} chars) from the "
              f"{'dedicated' if dedicated else 'alert'} sender")
        return 0
    msg = MIMEText(body)
    msg["From"] = frm
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
        server.login(frm, pw)
        server.sendmail(frm, [to], msg.as_string())
    st["replies" if a.reply else "sent"].append(now)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    log(f"SENT{' reply' if a.reply else ''} via {'dedicated' if dedicated else 'alert'} sender to "
        f"@{to.split('@')[-1]} ({len(body)} chars)")
    print(f"handed to Gmail for @{to.split('@')[-1]} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
