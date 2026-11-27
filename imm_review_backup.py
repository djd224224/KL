#!/usr/bin/env python3
r"""imm_review_backup.py -- the IMM daily review's moat, backed up to BigQuery
(review R-261124-20; Jack 2026-11-26: "Yes, build it").

Everything the review compounds lives in run-logs/incentive-mm on one unbacked
disk: the findings ledger, the change registry, the family history, priors and
playbook, every day's evidence pack, the dashboard's per-market day archive
and the program history. This copies it every morning, after the review's
email, into a private BigQuery dataset that nothing expires, and proves the
copy restores.

    python imm_review_backup.py run [--dry]        # the launcher, after the send
    python imm_review_backup.py verify             # the copy vs the local files
    python imm_review_backup.py restore --to DIR   # rebuild the files from the copy
    python imm_review_backup.py status             # the last run (backup_state.json)

Dataset elite-contact-446323-q7:imm_review (northamerica-northeast1), created
on purpose with no default table or partition expiration. The backup never
creates it: a missing dataset, or one that grew a default expiration, is an
error. bq_retention_guard.py alerts if a table in it is dropped or given an
expiration. Three kinds of table, none of which a bad local file can damage:

  <log>_lines   the append-only JSONL logs (LINE_LOGS), one row per line. Only
                lines past the copy's last are added, after checking that the
                copy's last line still matches the file. A file shorter than
                its copy (truncated, replaced) or rewritten in place is refused
                and alerts; the copy keeps every line.
  files         every other file under daily_review/: the moat folder, config
                versions, each day's evidence pack, findings, review and email.
                One row per version, added when a path's content differs from
                its newest copy. A corrupt rewrite only adds a version; old ones
                stay; restore takes each path's newest.
  day_archive   the dashboard's per-market day files, the latest copy of each
                (the dashboard rewrites recent days in bursts): replaced as a
                whole, but never by a set missing a day the copy has.

State: daily_review/backup_state.json (the next collect reports it in evidence
section 10; the email shows a health chip). IMM_REVIEW_BQ=0 turns it off.
Cost (11/26): ~60 MB, +~1 GB a year, inside the free 10 GB the project barely
uses (5.4 GB); batch loads are free.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import imm_review as rv  # noqa: E402

try:  # the task console is cp1252: never die on a glyph
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:
    pass

PROJECT = os.environ.get("IMM_REVIEW_BQ_PROJECT") or "elite-contact-446323-q7"
LIVE_DATASET = "imm_review"
LOCATION = "northamerica-northeast1"
MAX_FILE_BYTES = 9_000_000          # one row per file version; BigQuery caps a row at 100 MB
MAX_LOAD_BYTES = 40_000_000         # rows per load job are batched to about this
STATE_NAME = "backup_state.json"

# (table, root, path relative to the root): the append-only JSONL logs
LINE_LOGS = (
    ("ledger_lines", "review", "ledger.jsonl"),
    ("changes_lines", "review", "changes.jsonl"),
    ("counterfactual_lines", "review", "counterfactual.jsonl"),
    ("watch_results_lines", "review", "watch_results.jsonl"),
    ("config_manifest_lines", "review", "config_manifest.jsonl"),
    ("program_history_lines", "status", "program_history.jsonl"),
    # every settled market a fair model was scored on (imm_fair_scorecard.py, 11/26)
    ("model_scores_lines", "review", "moat/model_scores.jsonl"),
    # what the allocation model proposed each day (imm_allocation.py, 11/26)
    ("allocation_lines", "review", "moat/allocation.jsonl"),
)
# under daily_review/, never copied: a cache the collect rebuilds, this file's
# own state, other sessions' uncommitted-file copies, temp files
SKIP_RX = re.compile(r"^(archive_totals_cache\.json|backup_state\.json|uncommitted_backups/.*|.*\.tmp|.*\.lock)$")

FILES_SCHEMA = (("path", "STRING"), ("sha256", "STRING"), ("bytes", "INTEGER"), ("mtime", "TIMESTAMP"),
                ("backed_up", "TIMESTAMP"), ("encoding", "STRING"), ("content", "STRING"))
LINES_SCHEMA = (("line", "INTEGER"), ("raw", "STRING"), ("op", "STRING"), ("id", "STRING"), ("day", "STRING"),
                ("backed_up", "TIMESTAMP"))
DAY_SCHEMA = (("day", "STRING"), ("sha256", "STRING"), ("bytes", "INTEGER"), ("mtime", "TIMESTAMP"),
              ("backed_up", "TIMESTAMP"), ("content", "STRING"))
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def dataset_name() -> str:
    return os.environ.get("IMM_REVIEW_BQ_DATASET") or LIVE_DATASET


def _live_review_dir() -> str:
    return os.path.join(rv.LIVE_REPO, "run-logs", "incentive-mm", "daily_review")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _root(name: str) -> str:
    return rv.REVIEW_DIR if name == "review" else rv.STATUS_DIR


def _rel_out(root: str, rel: str) -> str:
    """Where a file sits under STATUS_DIR (the restore layout)."""
    return ("daily_review/" + rel) if root == "review" else rel


# ---------------------------------------------------------------------------
# BigQuery: the few calls the backup makes (a fake replaces this in tests)
# ---------------------------------------------------------------------------

class BQ:
    def __init__(self, project: str = PROJECT, dataset: Optional[str] = None):
        from google.cloud import bigquery
        from google.api_core.exceptions import NotFound
        self._bq, self._nf = bigquery, NotFound
        self.client = bigquery.Client(project=project)
        self.ds = f"{project}.{dataset or dataset_name()}"

    def _q(self, sql: str) -> List[dict]:
        return [dict(r) for r in self.client.query(sql, location=LOCATION).result()]

    def dataset_problem(self) -> str:
        try:
            d = self.client.get_dataset(self.ds)
        except self._nf:
            return f"dataset {self.ds} not found: it is created on purpose, never by the backup"
        if d.default_table_expiration_ms or d.default_partition_expiration_ms:
            return (f"dataset {self.ds} carries a default expiration "
                    f"({d.default_table_expiration_ms} / {d.default_partition_expiration_ms}): "
                    "its tables would silently self-delete (the 5/8 and 7/7 incidents)")
        return ""

    def exists(self, table: str) -> bool:
        try:
            self.client.get_table(f"{self.ds}.{table}")
            return True
        except self._nf:
            return False

    def line_stats(self, table: str) -> Tuple[int, int]:
        r = self._q(f"SELECT COUNT(*) AS n, IFNULL(MAX(line), 0) AS m FROM `{self.ds}.{table}`")[0]
        return int(r["n"]), int(r["m"])

    def line_at(self, table: str, line: int) -> Optional[str]:
        r = self._q(f"SELECT raw FROM `{self.ds}.{table}` WHERE line = {int(line)} LIMIT 1")
        return r[0]["raw"] if r else None

    def newest_shas(self) -> Dict[str, str]:
        return {r["path"]: r["sha"] for r in self._q(
            f"SELECT path, ARRAY_AGG(sha256 ORDER BY backed_up DESC LIMIT 1)[OFFSET(0)] AS sha "
            f"FROM `{self.ds}.files` GROUP BY path")}

    def day_shas(self) -> Dict[str, str]:
        return {r["day"]: r["sha256"] for r in self._q(f"SELECT day, sha256 FROM `{self.ds}.day_archive`")}

    def load(self, table: str, rows: List[dict], schema, truncate: bool) -> int:
        cfg = self._bq.LoadJobConfig(
            source_format=self._bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=[self._bq.SchemaField(n, t) for n, t in schema],
            write_disposition=(self._bq.WriteDisposition.WRITE_TRUNCATE if truncate
                               else self._bq.WriteDisposition.WRITE_APPEND))
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        job = self.client.load_table_from_file(io.BytesIO(data), f"{self.ds}.{table}", job_config=cfg,
                                               location=LOCATION)
        job.result()
        return int(job.output_rows or len(rows))

    def count(self, table: str) -> Optional[int]:
        try:
            return int(self.client.get_table(f"{self.ds}.{table}").num_rows or 0)
        except self._nf:
            return None

    def read_lines(self, table: str) -> List[Tuple[int, str]]:
        return [(int(r["line"]), r["raw"]) for r in
                self._q(f"SELECT line, raw FROM `{self.ds}.{table}` ORDER BY line")]

    def read_newest_files(self) -> List[dict]:
        return self._q(f"SELECT path, sha256, encoding, content FROM (SELECT *, ROW_NUMBER() OVER "
                       f"(PARTITION BY path ORDER BY backed_up DESC) AS rn FROM `{self.ds}.files`) WHERE rn = 1")

    def read_days(self) -> List[dict]:
        return self._q(f"SELECT day, sha256, content FROM `{self.ds}.day_archive`")


# ---------------------------------------------------------------------------
# reading the local files
# ---------------------------------------------------------------------------

def complete_lines(path: str) -> Optional[List[str]]:
    """The file's complete lines, in order, each exactly as written up to its
    "\\n" (a CRLF line keeps its "\\r": the box writes these logs CRLF, and a
    restore must be byte-identical); None when the file does not exist. A last
    line still being written (no newline yet) waits for the next run."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    return [p.decode("utf-8", "replace") for p in data.split(b"\n")[:-1]]


def _line_meta(raw: str) -> dict:
    """A few columns to query by: op, an id (id / key / keys / series), a day."""
    try:
        o = json.loads(raw)
    except ValueError:
        return {"op": None, "id": None, "day": None}
    if not isinstance(o, dict):
        return {"op": None, "id": None, "day": None}
    key = o.get("id") or o.get("key") or (",".join(map(str, o["keys"])) if isinstance(o.get("keys"), list)
                                          else None) or o.get("series")
    day = str(o.get("day") or o.get("date") or "")[:10]
    return {"op": str(o["op"])[:40] if o.get("op") else None, "id": str(key)[:200] if key else None,
            "day": day or None}


def local_files() -> Dict[str, str]:
    """{"daily_review/<rel>": abs path} of every file the files table covers."""
    logs = {rel for _t, root, rel in LINE_LOGS if root == "review"}
    out: Dict[str, str] = {}
    for dirpath, _dirs, names in os.walk(rv.REVIEW_DIR):
        for n in names:
            ap = os.path.join(dirpath, n)
            rel = os.path.relpath(ap, rv.REVIEW_DIR).replace("\\", "/")
            if rel in logs or SKIP_RX.match(rel):
                continue
            out["daily_review/" + rel] = ap
    return out


def local_days() -> Dict[str, str]:
    """{day: abs path} of the dashboard's day archive."""
    try:
        names = os.listdir(rv.ARCHIVE_DIR)
    except OSError:
        return {}
    return {m.group(1): os.path.join(rv.ARCHIVE_DIR, n) for n in names for m in [_DAY_FILE.match(n)] if m}


def _batches(rows: List[dict], key: str = "content") -> Iterable[List[dict]]:
    cur: List[dict] = []
    size = 0
    for r in rows:
        b = len(str(r.get(key) or "")) + 200
        if cur and size + b > MAX_LOAD_BYTES:
            yield cur
            cur, size = [], 0
        cur.append(r)
        size += b
    if cur:
        yield cur


# ---------------------------------------------------------------------------
# run / verify / restore
# ---------------------------------------------------------------------------

def _guard_target() -> str:
    """A review folder other than the live one (a test, a scratch copy) must
    name its own dataset: its files would otherwise land in the real backup."""
    if os.path.normcase(os.path.normpath(rv.REVIEW_DIR)) != os.path.normcase(os.path.normpath(_live_review_dir())) \
            and not os.environ.get("IMM_REVIEW_BQ_DATASET"):
        return (f"the review folder {rv.REVIEW_DIR} is not the live one: set IMM_REVIEW_BQ_DATASET to a "
                f"scratch dataset to back it up")
    return ""


def backup_lines(bq, table: str, root: str, rel: str, now: str, dry: bool, st: dict) -> int:
    lines = complete_lines(os.path.join(_root(root), rel))
    if lines is None:
        if bq.exists(table):
            st["refused"].append(f"{table}: {_rel_out(root, rel)} is gone locally; the copy keeps its lines")
        return 0
    n, m = bq.line_stats(table) if bq.exists(table) else (0, 0)
    if n != m:
        st["errors"].append(f"{table}: {n} rows but lines up to {m} in the copy (a gap)")
    if len(lines) < m:
        st["refused"].append(f"{table}: {_rel_out(root, rel)} has {len(lines)} lines, the copy {m}: "
                             "truncated or replaced? nothing loaded")
        return 0
    if m and bq.line_at(table, m) != lines[m - 1]:
        st["refused"].append(f"{table}: line {m} of {_rel_out(root, rel)} differs from the copy: the file was "
                             "rewritten, not appended; nothing loaded")
        return 0
    new = [dict(line=i + 1, raw=lines[i], backed_up=now, **_line_meta(lines[i])) for i in range(m, len(lines))]
    if new and not dry:
        for chunk in _batches(new, "raw"):
            bq.load(table, chunk, LINES_SCHEMA, truncate=False)
    return len(new)


def backup_files(bq, now: str, dry: bool, st: dict) -> Tuple[int, int]:
    newest = bq.newest_shas() if bq.exists("files") else {}
    rows: List[dict] = []
    for path, ap in sorted(local_files().items()):
        try:
            with open(ap, "rb") as fh:
                data = fh.read()
            mtime = os.path.getmtime(ap)
        except OSError as e:
            st["errors"].append(f"files: {path}: {e}")
            continue
        if len(data) > MAX_FILE_BYTES:
            st["errors"].append(f"files: {path} is {len(data):,} bytes, over the {MAX_FILE_BYTES:,} row cap")
            continue
        h = _sha(data)
        if newest.get(path) == h:
            continue
        try:
            content, enc = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, enc = base64.b64encode(data).decode("ascii"), "base64"
        rows.append({"path": path, "sha256": h, "bytes": len(data), "mtime": _iso(mtime), "backed_up": now,
                     "encoding": enc, "content": content})
    if rows and not dry:
        for chunk in _batches(rows):
            bq.load("files", chunk, FILES_SCHEMA, truncate=False)
    return len(rows), sum(r["bytes"] for r in rows)


def backup_days(bq, now: str, dry: bool, st: dict) -> int:
    days = local_days()
    have = bq.day_shas() if bq.exists("day_archive") else {}
    lost = sorted(set(have) - set(days))
    if lost:
        st["refused"].append(f"day_archive: {len(lost)} day(s) the copy has are gone locally "
                             f"({', '.join(lost[:5])}): nothing replaced")
        return 0
    rows = []
    for day, ap in sorted(days.items()):
        with open(ap, "rb") as fh:
            data = fh.read()
        rows.append({"day": day, "sha256": _sha(data), "bytes": len(data), "mtime": _iso(os.path.getmtime(ap)),
                     "backed_up": now, "content": data.decode("utf-8", "replace")})
    changed = sum(1 for r in rows if have.get(r["day"]) != r["sha256"])
    if changed and not dry:
        bq.load("day_archive", rows, DAY_SCHEMA, truncate=True)
    return changed


def _write_state(st: dict) -> None:
    rv._write_json(os.path.join(rv.REVIEW_DIR, STATE_NAME), st)


def run(dry: bool = False, bq=None) -> dict:
    t0 = time.time()
    st: dict = {"at": rv._now_iso(), "dataset": f"{PROJECT}:{dataset_name()}", "dry": dry, "ok": False,
                "errors": [], "refused": [], "added": {}, "totals": {}}
    if os.environ.get("IMM_REVIEW_BQ", "1") == "0":
        st.update(ok=True, off=True)
        if not dry:
            _write_state(st)
        return st
    why = _guard_target()
    if why:
        st["errors"].append(why)
        return st                     # nothing to record: this folder is not the live one
    try:
        bq = bq or BQ()
        problem = bq.dataset_problem()
        if problem:
            st["errors"].append(problem)
        else:
            now = _iso(t0)
            st["added"]["lines"] = {t: backup_lines(bq, t, root, rel, now, dry, st) for t, root, rel in LINE_LOGS}
            st["added"]["files"], st["added"]["file_bytes"] = backup_files(bq, now, dry, st)
            st["added"]["days_changed"] = backup_days(bq, now, dry, st)
            if not dry:
                st["totals"] = {t: bq.count(t) for t in [x[0] for x in LINE_LOGS] + ["files", "day_archive"]}
    except Exception as e:  # the launcher alerts on a non-zero exit
        st["errors"].append(f"{type(e).__name__}: {e}"[:500])
    st["ok"] = not st["errors"] and not st["refused"]
    st["secs"] = round(time.time() - t0, 1)
    if not dry:
        _write_state(st)
    return st


def verify(bq=None) -> dict:
    """Every local file's current content is the copy's newest version, every
    log line is in the copy, and every day file matches."""
    bq = bq or BQ()
    problems: List[str] = []
    newest = bq.newest_shas() if bq.exists("files") else {}
    files = local_files()
    for path, ap in sorted(files.items()):
        with open(ap, "rb") as fh:
            data = fh.read()
        if len(data) <= MAX_FILE_BYTES and newest.get(path) != _sha(data):
            problems.append(f"files: {path} is not the copy's newest version")
    for t, root, rel in LINE_LOGS:
        lines = complete_lines(os.path.join(_root(root), rel))
        if lines is None:
            continue
        n, m = bq.line_stats(t) if bq.exists(t) else (0, 0)
        if (n, m) != (len(lines), len(lines)) or (m and bq.line_at(t, m) != lines[m - 1]):
            problems.append(f"{t}: the copy has {n} rows up to line {m}, the file {len(lines)} lines")
    days = local_days()
    have = bq.day_shas() if bq.exists("day_archive") else {}
    for day, ap in sorted(days.items()):
        with open(ap, "rb") as fh:
            if have.get(day) != _sha(fh.read()):
                problems.append(f"day_archive: {day} differs from the copy")
    return {"ok": not problems, "problems": problems[:50], "n_problems": len(problems),
            "checked": {"files": len(files), "logs": len(LINE_LOGS), "days": len(days)}}


def restore(to_dir: str, bq=None) -> dict:
    """Rebuild the backed-up files under to_dir in STATUS_DIR's layout
    (daily_review/..., program_history.jsonl, dashboard/cache/day_archive/...).
    Never into the live run-logs."""
    to_dir = os.path.abspath(to_dir)
    if os.path.normcase(to_dir).startswith(os.path.normcase(os.path.abspath(rv.RUN_LOGS))):
        raise SystemExit(f"restore: {to_dir} is inside the live run-logs; restore elsewhere and copy by hand")
    bq = bq or BQ()
    out = {"files": 0, "logs": {}, "days": 0, "bad_sha": []}

    def put(rel: str, data: bytes) -> None:
        p = os.path.join(to_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)

    for r in bq.read_newest_files():
        data = base64.b64decode(r["content"]) if r.get("encoding") == "base64" else str(r["content"]).encode("utf-8")
        if _sha(data) != r["sha256"]:
            out["bad_sha"].append(r["path"])
        put(r["path"], data)
        out["files"] += 1
    for t, root, rel in LINE_LOGS:
        if not bq.exists(t):
            continue
        lines = bq.read_lines(t)
        put(_rel_out(root, rel), "".join(raw + "\n" for _n, raw in lines).encode("utf-8"))
        out["logs"][t] = len(lines)
    for r in bq.read_days() if bq.exists("day_archive") else []:
        data = str(r["content"]).encode("utf-8")
        if _sha(data) != r["sha256"]:
            out["bad_sha"].append("day_archive/" + r["day"])
        put(f"dashboard/cache/day_archive/{r['day']}.json", data)
        out["days"] += 1
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true", help="say what would load; touch nothing")
    r.add_argument("--day", help="ignored (the launcher passes the review day for its log)")
    sub.add_parser("verify")
    rs = sub.add_parser("restore")
    rs.add_argument("--to", required=True)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(rv._load_json(os.path.join(rv.REVIEW_DIR, STATE_NAME), {}), indent=1))
        return 0
    if a.cmd == "run":
        st = run(dry=a.dry)
        print(json.dumps({k: v for k, v in st.items() if k != "totals"}, default=str))
        if st.get("totals"):
            print("totals: " + json.dumps(st["totals"]))
        return 0 if st["ok"] else 1
    if a.cmd == "verify":
        v = verify()
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] else 1
    out = restore(a.to)
    print(json.dumps(out, indent=1))
    return 0 if not out["bad_sha"] else 1


if __name__ == "__main__":
    sys.exit(main())
