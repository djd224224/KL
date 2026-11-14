"""bait_watch: the bot's bait-defense lines on market-wave markets (Jack 2026-11-11: "Be
extremely careful for baiters, notify me when something suspicious occurs", "Just cover
market wave"). Log lines are the bot's own formats. No files, no network."""
import unittest
from datetime import datetime, timezone

import bait_watch as bw
import wave_alert as wa

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
WAVE = {"KXSENATENOM-NCD-28-DDAV": "w1111-statenom", "KXTREASRACE-UT-28-R": "w1111-statenom",
        "KXDPRESPRIMARY-28NH-GNEW": "w1111-elec2028"}
wave = WAVE.get
LINES = [
    # off the waves: never reported
    "2026-11-11 17:23:48Z [IMM] sweep breaker (on): KXFEATURE-26GTA6-BAD BID @ 55c filled out (10) -> pulling "
    "128 order(s) in KXFEATURE-26GTA6 for 3600s; trip 3 within 30 min of the last hold: held 60 min",
    "2026-11-11 16:56:53Z [IMM] ALERT [toxic_halt] KXLOLAWARD-WORLDS26FMVP-KNIGHT ASK picked off 2x in 24h "
    "(latest: filled 13c, mark 18.5c 6min later) -- halting that side 30min",
    # wave markets
    "2026-11-11 17:40:00Z [IMM] sweep breaker (on): KXTREASRACE-UT-28-R BID @ 89c filled out (200) -> pulling "
    "6 order(s) in KXTREASRACE-UT-28 for 3600s; trip 3 within 30 min of the last hold: held 60 min",
    "2026-11-11 17:41:00Z [IMM] sweep breaker (on): KXTREASRACE-UT-28-R ASK @ 97.6c filled out (40) -> pulling "
    "6 order(s) in KXTREASRACE-UT-28 for 600s",                                    # one trip: quiet
    "2026-11-11 17:42:00Z [IMM] ALERT [toxic_halt] KXDPRESPRIMARY-28NH-GNEW BID picked off 2x in 24h "
    "(latest: filled 31c, mark 24.5c 4min later) -- halting that side 30min",
    "2026-11-11 17:43:00Z [IMM] bait stop: KXDPRESPRIMARY-28NH-GNEW ASK @ 40c -- a taker of exactly our 200 took "
    "the whole rung on a lone top our rung had jumped to; that side out until 00:00 ET, and the lone-ref guard "
    "ON for KXDPRESPRIMARY-28NH until then",
] + [f"2026-11-11 17:{m:02d}:10Z [IMM] join wait KXSENATENOM-NCD-28-DDAV bid: the better price went away after "
     f"40s; rests 3c" for m in (20, 31, 45)] + [
    "2026-11-11 15:00:10Z [IMM] join wait KXTREASRACE-UT-28-R bid: the better price went away after 9s; rests 7c",
    "2026-11-11 17:50:10Z [IMM] join wait KXTREASRACE-UT-28-R bid: the better price went away after 9s; rests 7c",
]


class BaitWatch(unittest.TestCase):
    def test_signals_on_wave_markets_only(self):
        items = bw.scan_log(LINES, "2026-11-11 17:15:00", NOW, wave)
        got = {(i["kind"], i["ticker"], i["level"]) for i in items}
        self.assertEqual(got, {
            ("swept 3x", "KXTREASRACE-UT-28-R", "text"),
            ("toxic halt", "KXDPRESPRIMARY-28NH-GNEW", "email"),
            ("bait stop", "KXDPRESPRIMARY-28NH-GNEW", "text"),
            ("flash", "KXSENATENOM-NCD-28-DDAV", "email")})
        # a flash needs 3 inside 60 min: UT's 15:00 one is too old to count with 17:50's
        self.assertFalse([i for i in items if i["kind"] == "flash" and i["ticker"] == "KXTREASRACE-UT-28-R"])
        # nothing newer than the last check: nothing
        self.assertEqual(bw.scan_log(LINES, "2026-11-11 17:59:00", NOW, wave), [])

    def test_once_only_and_the_flash_re_alert(self):
        items = bw.scan_log(LINES, "2026-11-11 17:15:00", NOW, wave)
        self.assertEqual(len(bw.pick_new(items, {}, NOW)), 4)
        sent = {i["key"]: {"at": "2026-11-11T17:55:00Z", "times": i.get("times")} for i in items}
        self.assertEqual(bw.pick_new(items, sent, NOW), [])
        flash = [dict(i, times=6) for i in items if i["kind"] == "flash"]       # 3 more since
        self.assertEqual(len(bw.pick_new(flash, sent, NOW)), 1)
        later = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)              # 6 h on
        self.assertEqual([i["kind"] for i in bw.pick_new(items, sent, later)], ["flash"])

    def test_the_alert_texts_on_hard_evidence_only(self):
        items = bw.scan_log(LINES, "2026-11-11 17:15:00", NOW, wave)
        text, push, subject, body, do_text = wa.bait_msg({"new": items})
        lines = text.splitlines()
        self.assertEqual(lines[0], "BAIT WATCH: 4 suspicious signs on wave mkts - 1 bait stop, 1 swept 3x, "
                                   "1 toxic halt, 1 flash")
        self.assertTrue(lines[2].startswith("!1. BAIT STOP KXDPRESPRIMARY-28NH-GNEW ask @40c 17:43Z"))
        self.assertTrue(do_text)
        soft = [i for i in items if i["level"] == "email"]
        self.assertFalse(wa.bait_msg({"new": soft})[4])
        self.assertIn("FLASH KXSENATENOM-NCD-28-DDAV bid: a better price shown and pulled 3x since 17:20Z",
                      wa.bait_msg({"new": soft})[0])


if __name__ == "__main__":
    unittest.main()
