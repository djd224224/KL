"""Tests for bq_retention_guard.py's dataset coverage (the IMM review's moat
backup, imm_review, joined Kalshi on 2026-11-26; review R-261124-20). A fake
client stands in for BigQuery: nothing calls Google."""

import unittest
from types import SimpleNamespace

from google.api_core.exceptions import NotFound

import bq_retention_guard as g


class FakeClient:
    def __init__(self, datasets, options=None):
        self.datasets = datasets              # name -> (table default ms, partition default ms)
        self.options = options or {}          # name -> [(table, option, value)]

    def get_dataset(self, ref):
        name = ref.split(".")[-1]
        if name not in self.datasets:
            raise NotFound(f"Dataset {ref} not found")
        t, p = self.datasets[name]
        return SimpleNamespace(default_table_expiration_ms=t, default_partition_expiration_ms=p)

    def query(self, sql, location=None):
        name = sql.split("INFORMATION_SCHEMA")[0].split(".")[-2]
        rows = [SimpleNamespace(table_name=t, option_name=o, option_value=v) for t, o, v in self.options.get(name, [])]
        return SimpleNamespace(result=lambda: rows)


class GuardDatasetTests(unittest.TestCase):
    def test_imm_review_is_watched_with_kalshi(self):
        self.assertEqual(g.watched_datasets(), [g.DATASET] + g.EXTRA_DATASETS)
        self.assertIn("imm_review", g.watched_datasets())
        self.assertEqual(g.watched_datasets()[0], g.DATASET)

    def test_clean_datasets_pass(self):
        v = []
        g.audit(FakeClient({g.DATASET: (None, None), "imm_review": (None, None)}), v, generations=False)
        self.assertEqual(v, [])

    def test_a_vanished_dataset_alerts(self):
        v = []
        g.audit(FakeClient({g.DATASET: (None, None)}), v, generations=False)
        self.assertEqual(len(v), 1)
        self.assertIn("imm_review no longer exists", v[0])

    def test_expirations_name_their_dataset(self):
        v = []
        c = FakeClient({g.DATASET: (None, None), "imm_review": (5184000000, None)},
                       {"imm_review": [("files", "expiration_timestamp", "2027-01-01")]})
        g.audit(c, v, generations=False)
        self.assertTrue(any("imm_review default_table_expiration_ms" in x for x in v), v)
        self.assertTrue(any("imm_review.files has undeclared expiration_timestamp" in x for x in v), v)


if __name__ == "__main__":
    unittest.main()
