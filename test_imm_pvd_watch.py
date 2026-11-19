"""imm_pvd_watch's page-row helpers (the wave dashboards)."""
import os
import tempfile
import unittest

os.environ.setdefault("IMM_STATUS_DIR", os.path.join(tempfile.gettempdir(), "imm_pvd_watch_test_status"))
os.makedirs(os.environ["IMM_STATUS_DIR"], exist_ok=True)

import imm_pvd_watch as w  # noqa: E402


class TestUnwindState(unittest.TestCase):
    """Jack 2026-11-19: "make that clear in the dashboard, what has resting positions
    already and are unwinding"."""

    def row(self, pos, bid=None, ask=None):
        rest = 300 if (bid is not None or ask is not None) else 0
        return {"pos": pos, "our_bid": bid, "our_ask": ask, "rest_ct": rest}

    def test_flat_is_blank(self):
        self.assertEqual(w.unwind_state(self.row(0, 5, 95)), "")
        self.assertEqual(w.unwind_state(self.row(0.4, 5, None)), "")

    def test_only_the_closing_order_is_unwinding(self):
        # short YES: the closing order is our bid; long YES: our ask
        self.assertEqual(w.unwind_state(self.row(-200, bid=25)), "unwinding")
        self.assertEqual(w.unwind_state(self.row(200, ask=10)), "unwinding")

    def test_the_adding_side_resting_is_holding_and_quoting(self):
        self.assertEqual(w.unwind_state(self.row(-200, bid=25, ask=26)), "holding + quoting")
        self.assertEqual(w.unwind_state(self.row(200, bid=9)), "holding + quoting")

    def test_nothing_resting(self):
        self.assertEqual(w.unwind_state(self.row(-4)), "unwinding, nothing resting")
        self.assertIn("unwinding, nothing resting", w.UNWIND_STATES)
        self.assertNotIn("holding + quoting", w.UNWIND_STATES)


if __name__ == "__main__":
    unittest.main()
