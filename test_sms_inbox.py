"""sms_inbox.py + phone_actions.py (Jack 2026-11-08: text replies to Claude,
"Yes, build it"). Pure functions and temp files only: no IMAP, no Claude, no
sends, and the real list / registry are never touched."""
import email
import email.utils
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import phone_actions as pa
import sms_inbox as si

GOOD_AR = ("mx.google.com; dkim=pass header.i=@tmomail.net header.s=mes header.b=KwFP15xx; "
           "spf=pass (google.com: domain of +19729713381@tmomail.net designates 34.195.253.214 as "
           "permitted sender) smtp.mailfrom=+19729713381@tmomail.net")


def tmo(text="stop wave w1108-x", frm="+19729713381@tmomail.net", ar=GOOD_AR, extra_ar=None):
    """A message shaped like T-Mobile's: html part + the words in text_0.txt."""
    m = MIMEMultipart("related")
    m["From"] = frm
    m["To"] = "jackdu224@gmail.com"
    if ar is not None:
        m["Authentication-Results"] = ar            # the top one, as Gmail writes it
    if extra_ar:
        m["Authentication-Results"] = extra_ar      # a lower, sender-written one
    m.attach(MIMEText("<html><body>junk</body></html>", "html"))
    t = MIMEText(text, "plain")
    t.add_header("Content-Disposition", "attachment", filename="text_0.txt")
    m.attach(t)
    return email.message_from_bytes(m.as_bytes())


class FetchTests(unittest.TestCase):
    """Gmail put both of his first texts in Spam (11/08); All Mail over IMAP leaves Spam out."""

    def setUp(self):
        self.saved = (si.imaplib.IMAP4_SSL, si.sms_alert.setting)
        si.sms_alert.setting = lambda name: "x@example.com" if name.endswith("FROM") else "pw"
        now = datetime.now(timezone.utc)

        def raw(text, mins_ago, mid):
            m = tmo(text)
            m["Date"] = email.utils.format_datetime(now - timedelta(minutes=mins_ago))
            m["Message-ID"] = mid
            return m.as_bytes()
        boxes = {'"[Gmail]/All Mail"': {b"1": raw("new one", 1, "<a>")},
                 '"[Gmail]/Spam"': {b"7": raw("from spam", 5, "<b>"), b"8": raw("new one", 1, "<a>")}}

        class FakeImap:
            def __init__(self, *a, **k):
                self.box = None
            def login(self, u, p):
                pass
            def select(self, box, readonly=False):
                assert readonly
                self.box = boxes[box]
                return "OK", [b"1"]
            def uid(self, cmd, *args):
                if cmd == "SEARCH":
                    assert "+19729713381@tmomail.net" in args[1]    # from:9729713381@ never matches
                    return "OK", [b" ".join(self.box)]
                return "OK", [(b"x", self.box[args[0]])]
            def logout(self):
                pass
        si.imaplib.IMAP4_SSL = FakeImap

    def tearDown(self):
        si.imaplib.IMAP4_SSL, si.sms_alert.setting = self.saved

    def test_spam_is_read_and_each_text_comes_once_oldest_first(self):
        got = si.fetch_new({"seen": []})
        self.assertEqual([si.body_text(m) for _, m in got], ["from spam", "new one"])

    def test_seen_texts_are_skipped(self):
        self.assertEqual([mid for mid, _ in si.fetch_new({"seen": ["<b>"]})], ["<a>"])

    def test_age_from_the_date_header(self):
        m = tmo("x")
        m["Date"] = "Sun, 08 Nov 2026 15:46:10 -0000"
        self.assertGreater(si.msg_age_min(m), 0)
        self.assertIsNone(si.msg_age_min(tmo("no date")))


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = (si.STATE, si.LOG, si.fetch_new, si.run_claude, si.send_reply, si.email_jack)
        si.STATE = os.path.join(self.d, "state.json")
        si.LOG = os.path.join(self.d, "inbox.log")
        with open(si.STATE, "w") as f:
            json.dump({"seen": []}, f)
        self.ran, self.mails = [], []
        si.run_claude = lambda text, hist: (self.ran.append(text), ("ACTION: none\nREPLY: hi", "ok"))[1]
        si.send_reply = lambda text, answer, dry: None
        si.email_jack = lambda subj, body: self.mails.append(subj)

    def tearDown(self):
        si.STATE, si.LOG, si.fetch_new, si.run_claude, si.send_reply, si.email_jack = self.saved

    def test_a_late_text_is_not_run(self):
        old, new = tmo("halt"), tmo("status")
        old["Date"] = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(hours=5))
        new["Date"] = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(minutes=2))
        si.fetch_new = lambda st: [("<old>", old), ("<new>", new)]
        si.one_pass(False)
        self.assertEqual(self.ran, ["status"])
        self.assertEqual(self.mails, ["Text not run: it reached the inbox late"])
        with open(si.STATE) as f:
            self.assertEqual(json.load(f)["seen"], ["<old>", "<new>"])


class SenderCheckTests(unittest.TestCase):
    def test_tmobile_signed_text_from_jack_passes(self):
        self.assertEqual(si.sender_ok(tmo()), (True, "dkim+spf pass"))

    def test_other_number_is_refused(self):
        ok, why = si.sender_ok(tmo(frm="+14698650428@tmomail.net"))
        self.assertFalse(ok)
        self.assertIn("not Jack's number", why)

    def test_forged_from_without_tmobile_dkim_is_refused(self):
        forged = ("mx.google.com; dkim=fail header.i=@tmomail.net; spf=softfail "
                  "smtp.mailfrom=evil@example.com")
        ok, why = si.sender_ok(tmo(ar=forged))
        self.assertFalse(ok)
        self.assertIn("DKIM", why)

    def test_spf_for_another_sender_is_refused(self):
        ar = GOOD_AR.replace("smtp.mailfrom=+19729713381@tmomail.net", "smtp.mailfrom=+15550001111@tmomail.net")
        self.assertFalse(si.sender_ok(tmo(ar=ar))[0])

    def test_only_gmails_top_header_counts(self):
        # no Gmail header on top; a sender-written "pass" further down must not count
        ok, why = si.sender_ok(tmo(ar="evil.example; dkim=pass header.i=@tmomail.net", extra_ar=GOOD_AR))
        self.assertFalse(ok)
        self.assertIn("no Gmail", why)

    def test_the_words_come_from_text_0(self):
        self.assertEqual(si.body_text(tmo("halt the bot")), "halt the bot")


class ActionParseTests(unittest.TestCase):
    def test_allowed_actions_pass_exactly(self):
        self.assertEqual(si.parse_answer("ACTION: stop-wave w1108-mixed\nREPLY: Stopping it now."),
                         ("stop-wave w1108-mixed", "Stopping it now.", ""))
        self.assertEqual(si.parse_answer("ACTION: HALT\nREPLY: Halting.")[0], "halt")
        self.assertEqual(si.parse_answer("ACTION: stop-series KXHEADLINE\nREPLY: ok")[0],
                         "stop-series KXHEADLINE")

    def test_anything_else_is_none(self):
        for bad in ("rm -rf run-logs", "stop-wave w1; del HALT", "deposit 500", "start nfl-sniper",
                    "stop-series headline && x", "size IMM_A_SIZE_MULT"):
            a, reply, _plan = si.parse_answer(f"ACTION: {bad}\nREPLY: ok")
            self.assertEqual(a, "none", bad)
            self.assertIn("I didn't act", reply)

    def test_no_format_is_a_plain_answer(self):
        self.assertEqual(si.parse_answer("Just words."), ("none", "Just words.", ""))

    def test_a_plan_comes_back_whole(self):
        a, reply, plan = si.parse_answer("ACTION: PLAN\nPLAN: 1. set X in run_incentive_mm.ps1\n2. push\n"
                                         "REPLY: Here's the plan.")
        self.assertEqual((a, reply), ("PLAN", "Here's the plan."))
        self.assertEqual(plan, "1. set X in run_incentive_mm.ps1 2. push")
        self.assertEqual(si.parse_answer("ACTION: PLAN\nREPLY: hm")[0], "none")    # a plan needs steps

    def test_tidy_keeps_underscores(self):
        self.assertEqual(si.tidy("**Bold** UNWIND_ONLY_WAVES `x`\n# Head"), "Bold UNWIND_ONLY_WAVES x\nHead")


class PhoneActionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = (pa.LIST, pa.REGISTRY, pa.LOCK, pa.LOG, pa.PAUSE, pa.HALT)
        pa.LIST = os.path.join(self.d, "list.json")
        pa.REGISTRY = os.path.join(self.d, "waves.json")
        pa.LOCK = os.path.join(self.d, "scan.lock")
        pa.LOG = os.path.join(self.d, "phone.log")
        pa.PAUSE = os.path.join(self.d, "OVERNIGHT_PAUSED")
        pa.HALT = os.path.join(self.d, "HALT")
        later = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(pa.LIST, "w") as fh:
            json.dump({"markets": {"KXA-26-X": {"stop": later}, "KXA-26-Y": {"stop": past},
                                   "KXB-26-Z": {"stop": later}}}, fh)
        with open(pa.REGISTRY, "w") as fh:
            json.dump({"waves": {"w1": {"series": ["KXA"], "near": [], "enabled": True}}}, fh)

    def tearDown(self):
        pa.LIST, pa.REGISTRY, pa.LOCK, pa.LOG, pa.PAUSE, pa.HALT = self.saved

    def test_status_quotes_the_command_center_pnl_first(self):
        import contextlib
        import io
        saved = (pa.STATUS, pa.SUMMARY, pa.STATUS_DIR)
        pa.STATUS = os.path.join(self.d, "status.json")
        pa.SUMMARY = os.path.join(self.d, "summary.json")
        pa.STATUS_DIR = self.d
        try:
            with open(pa.STATUS, "w") as fh:
                json.dump({"pnl_today": -62.0, "realized_today": -6.0, "inventory_contracts": 10.0}, fh)
            with open(pa.SUMMARY, "w") as fh:
                json.dump({"generated": 0, "windows": {
                    "today": {"desc": "since 00:00 ET", "pnl": -3692.0, "rew": 183.4},
                    "yesterday": {"desc": "Sat", "pnl": -1330.85, "rew": 5898.33}}}, fh)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                pa.cmd_status(None)
            text = out.getvalue()
        finally:
            pa.STATUS, pa.SUMMARY, pa.STATUS_DIR = saved
        self.assertIn("today (since 00:00 ET): trading P&L -3,692.00", text)
        self.assertNotIn("-62", text)                   # the bot's own pnl_today is never printed
        self.assertLess(text.index("P&L = the command center"), text.index("inventory"))

    def test_stop_wave_moves_only_its_live_entries(self):
        class A: name, why = "w1", "stop wave w1"
        pa.cmd_stop_wave(A)
        d = json.load(open(pa.LIST))
        self.assertEqual(set(d["markets"]), {"KXA-26-Y", "KXB-26-Z"})        # expired + other series stay
        self.assertEqual(set(d["stopped"]), {"KXA-26-X"})
        self.assertIn("Jack by text", d["stopped"]["KXA-26-X"]["reason"])
        self.assertFalse(json.load(open(pa.REGISTRY))["waves"]["w1"]["enabled"])
        self.assertFalse(os.path.exists(pa.LOCK))                           # lock released

    def test_stop_series_leaves_waves_on(self):
        class A: series, why = "kxa", "stop KXA"
        pa.cmd_stop_series(A)
        self.assertEqual(json.load(open(pa.REGISTRY))["waves"]["w1"]["series"], [])
        self.assertIn("KXA-26-X", json.load(open(pa.LIST))["stopped"])

    def test_halt_needs_the_confirm_word(self):
        class Bad: confirm, why = "yes", "x"
        with self.assertRaises(SystemExit):
            pa.cmd_halt(Bad)
        self.assertFalse(os.path.exists(pa.HALT))
        class Good: confirm, why = "HALT", "halt the bot"
        pa.cmd_halt(Good)
        self.assertTrue(os.path.exists(pa.HALT))

    def test_pause_writes_the_file(self):
        class A: why = "pause overnight"
        pa.cmd_pause(A)
        self.assertIn("paused by text", open(pa.PAUSE).read())


class ConfirmCodeTests(unittest.TestCase):
    """Risky actions wait for "YES <code>" (Jack 2026-11-08: "allow sizing adjustments too. and allow
    refreshing the dashboards", on resume / un-halt / unwind / restart behind a code)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = (si.STATE, si.LOG, si.do_action, si.describe)
        si.STATE = os.path.join(self.d, "state.json")
        si.LOG = os.path.join(self.d, "inbox.log")
        self.ran = []
        si.do_action = lambda action, why: self.ran.append(action) or f"Done: {action}"
        si.describe = lambda action: (True, f"desc of {action}")

    def tearDown(self):
        si.STATE, si.LOG, si.do_action, si.describe = self.saved

    def test_stops_and_refreshes_run_at_once(self):
        for a in ("stop-wave w1", "halt", "unwind-only on", "refresh all"):
            out = si.act_or_ask({}, a, "ok", "x")
            self.assertIn(f"Done: {a}", out)
        self.assertEqual(self.ran, ["stop-wave w1", "halt", "unwind-only on", "refresh all"])

    def test_risky_action_waits_for_the_right_code(self):
        st = {}
        out = si.act_or_ask(st, "size IMM_TRUMPVH_SIZE_MULT 5", "I'll need your OK.", "trumpvh 5x")
        code = st["pending"]["code"]
        self.assertIn(f"Reply YES {code}", out)
        self.assertEqual(self.ran, [])                          # nothing ran yet
        self.assertIsNone(si.confirm_or_cancel(st, "how's it going?"))   # not a code: goes to Claude
        res = si.confirm_or_cancel(st, f"yes {code}")
        self.assertIn("Confirmed", res)
        self.assertEqual(self.ran, ["size IMM_TRUMPVH_SIZE_MULT 5"])
        self.assertNotIn("pending", st)
        self.assertIn("no change waiting", si.confirm_or_cancel(st, f"YES {code}"))   # single use

    def test_wrong_code_cancels(self):
        st = {}
        si.act_or_ask(st, "unhalt", "ok", "unhalt")
        wrong = "1000" if st["pending"]["code"] != "1000" else "1001"
        self.assertIn("Wrong code", si.confirm_or_cancel(st, f"YES {wrong}"))
        self.assertEqual(self.ran, [])
        self.assertNotIn("pending", st)

    def test_expired_code_does_nothing(self):
        st = {}
        si.act_or_ask(st, "restart", "ok", "restart")
        st["pending"]["expires"] = 0
        self.assertIn("expired", si.confirm_or_cancel(st, f"YES {st['pending']['code']}"))
        self.assertEqual(self.ran, [])

    def test_no_cancels_and_bare_code_only_while_pending(self):
        st = {}
        self.assertIsNone(si.confirm_or_cancel(st, "2028"))     # no pending: a number is a question
        si.act_or_ask(st, "resume-overnight", "ok", "resume")
        self.assertIn("Cancelled", si.confirm_or_cancel(st, "no"))
        si.act_or_ask(st, "waves-unwind off", "ok", "waves off")
        self.assertIn("Confirmed", si.confirm_or_cancel(st, st["pending"]["code"]))
        self.assertEqual(self.ran, ["waves-unwind off"])

    def test_invalid_action_is_not_done(self):
        si.describe = lambda action: (False, "IMM_NOPE_SIZE_MULT is not a size knob")
        st = {}
        out = si.act_or_ask(st, "size IMM_NOPE_SIZE_MULT 5", "ok", "x")
        self.assertIn("Not done", out)
        self.assertNotIn("pending", st)

    def test_verbs_are_normalised_and_words_checked(self):
        self.assertEqual(si.parse_answer("ACTION: Unwind-Only OFF\nREPLY: x")[0], "unwind-only off")
        self.assertEqual(si.parse_answer("ACTION: size IMM_A_SIZE_MULT 5;del\nREPLY: x")[0], "none")
        self.assertEqual(si.parse_answer('ACTION: wave-set w1 budget "40k"\nREPLY: x')[0], "none")
        self.assertTrue(si.needs_code("unwind-only off"))
        self.assertFalse(si.needs_code("unwind-only on"))


class PlanTests(unittest.TestCase):
    """Any change a session could make, as a PLAN behind a code (Jack 2026-11-08: "can you just allow
    texts in general? not so rigid?")."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = (si.STATE, si.LOG, si.claude_call)
        si.STATE = os.path.join(self.d, "state.json")
        si.LOG = os.path.join(self.d, "inbox.log")
        self.calls = []

        def fake(prompt, model, timeout, deny=None, persist=False):
            self.calls.append((prompt, model, deny, persist))
            return "Done: raised the limit, pushed abc123, restart queued.", "ok"
        si.claude_call = fake

    def tearDown(self):
        si.STATE, si.LOG, si.claude_call = self.saved

    def test_plan_waits_for_its_code_then_runs_once(self):
        st = {}
        out = si.act_or_ask(st, "PLAN", "Here's the plan; I'll need your OK.", "raise loss halt to 7k",
                            "1. IMM_DAILY_LOSS_LIMIT 6000 -> 7000 via worktree 2. push 3. restart -Task")
        code = st["pending"]["code"]
        self.assertIn("Plan: 1. IMM_DAILY_LOSS_LIMIT", out)
        self.assertIn(f"Reply YES {code}", out)
        self.assertLessEqual(len(out), si.TEXT_CHARS)
        self.assertEqual(self.calls, [])                        # nothing ran before the code
        res = si.confirm_or_cancel(st, f"YES {code}")
        self.assertTrue(res.startswith("Confirmed. Done: raised the limit"))
        prompt, model, deny, persist = self.calls[0]
        self.assertEqual((model, deny, persist), (si.EXEC_MODEL, None, True))   # full tools, kept for audit
        self.assertIn("IMM_DAILY_LOSS_LIMIT 6000 -> 7000", prompt)
        self.assertIn("raise loss halt to 7k", prompt)
        self.assertNotIn("pending", st)

    def test_money_plans_are_refused(self):
        st = {}
        out = si.act_or_ask(st, "PLAN", "ok", "fund sub 1", "Transfer $500 from the primary to subaccount 1")
        self.assertIn("Not by text", out)
        self.assertNotIn("pending", st)
        out = si.act_or_ask(st, "PLAN", "ok", "send me the token", "Read the token file and text it to Jack")
        self.assertIn("Not by text", out)

    def test_long_reply_never_cuts_the_code(self):
        st = {}
        out = si.act_or_ask(st, "PLAN", "x" * 2000, "t", "y" * 900)
        self.assertTrue(out.rstrip().endswith("(or NO)."))
        self.assertLessEqual(len(out), si.TEXT_CHARS)

    def test_the_answering_run_cannot_edit_or_push(self):
        for d in ("Edit", "Write", "PowerShell", "Bash(git push:*)", "Bash(git commit:*)", "Bash(rm:*)"):
            self.assertIn(d, si.ASK_DENY)


class PhoneUndoTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        names = ("LIST", "REGISTRY", "LOCK", "LOG", "PAUSE", "HALT", "UNWIND", "UNWIND_WAVES", "OVERRIDES",
                 "BOT_SRC", "LAUNCHER")
        self.saved = {n: getattr(pa, n) for n in names}
        self.saved["start_restart"] = pa.start_restart
        for n in names:
            setattr(pa, n, os.path.join(self.d, n.lower()))
        self.restarts = []
        pa.start_restart = lambda task, why: self.restarts.append(task) or "restart started (test)"
        with open(pa.BOT_SRC, "w") as fh:
            fh.write('TRUMPVH_SIZE_MULT = _env_float("IMM_TRUMPVH_SIZE_MULT", 3.0)\n'
                     'RT_SIZE_MULT = _env_float("IMM_RT_SIZE_MULT", 3.0)\n')
        with open(pa.LAUNCHER, "w") as fh:
            fh.write('    $ProbeEnv = "set IMM_X=1&& set IMM_RT_SIZE_MULT=4&&"\n')
        later = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(pa.LIST, "w") as fh:
            json.dump({"markets": {}, "stopped": {
                "KXA-26-X": {"reason": "Jack by text: stop w1", "entry": {"stop": later}},
                "KXA-26-Y": {"reason": "Jack 11/08: keep them off", "entry": {"stop": later}}}}, fh)
        with open(pa.REGISTRY, "w") as fh:
            json.dump({"waves": {"w1": {"series": ["KXA"], "near": [], "enabled": False, "budget": 50000}}}, fh)

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(pa, n, v)

    def test_resume_wave_restores_only_what_a_text_stopped(self):
        class A: name, why = "w1", "turn w1 back on"
        pa.cmd_resume_wave(A)
        d = json.load(open(pa.LIST))
        self.assertEqual(set(d["markets"]), {"KXA-26-X"})
        self.assertIn("KXA-26-Y", d["stopped"])                  # a session's stop stays
        self.assertTrue(json.load(open(pa.REGISTRY))["waves"]["w1"]["enabled"])

    def test_unhalt_and_modes_rename_and_create(self):
        open(pa.HALT, "w").close()
        class A: why = "unhalt"
        pa.cmd_unhalt(A)
        self.assertFalse(os.path.exists(pa.HALT))
        self.assertTrue(any(f.startswith("halt.off-") for f in os.listdir(self.d)))
        class M: cmd, state, why = "unwind-only", "on", "unwind only"
        pa.cmd_mode(M)
        self.assertTrue(os.path.exists(pa.UNWIND))
        M.state = "off"
        pa.cmd_mode(M)
        self.assertFalse(os.path.exists(pa.UNWIND))

    def test_size_reads_launcher_then_writes_override_and_restarts(self):
        self.assertEqual(pa.size_now("IMM_RT_SIZE_MULT"), (4.0, "launcher"))
        self.assertEqual(pa.size_now("IMM_TRUMPVH_SIZE_MULT"), (3.0, "code default"))
        class A: name, value, why = "imm_trumpvh_size_mult", "5x", "trumpvh 5x"
        pa.cmd_size(A)
        self.assertEqual(open(pa.OVERRIDES).read().splitlines()[-1], "IMM_TRUMPVH_SIZE_MULT=5")
        self.assertEqual(pa.size_now("IMM_TRUMPVH_SIZE_MULT"), (5.0, "text override"))
        self.assertEqual(self.restarts, [True])                  # the -Task bounce that applies it
        class Bad: name, value, why = "IMM_TRUMPVH_SIZE_MULT", "11", "x"
        with self.assertRaises(SystemExit):
            pa.cmd_size(Bad)
        class Nope: name, value, why = "IMM_FOO_SIZE_MULT", "2", "x"
        with self.assertRaises(SystemExit):
            pa.cmd_size(Nope)

    def test_wave_set_bounds(self):
        class A: name, key, value, why = "w1", "budget", "40k", "budget 40k"
        pa.cmd_wave_set(A)
        self.assertEqual(json.load(open(pa.REGISTRY))["waves"]["w1"]["budget"], 40000.0)
        class B: name, key, value, why = "w1", "budget", "900k", "x"
        with self.assertRaises(SystemExit):
            pa.cmd_wave_set(B)


if __name__ == "__main__":
    unittest.main()
