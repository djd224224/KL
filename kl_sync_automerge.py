"""KL sync auto-merge: merge GitHub's run-logs-only commits into a box main that
is ahead of GitHub, so the :15/:45 sync stops failing every night.

R-261125-02 (Jack 2026-11-27: "Yes, build it").

THE PROBLEM. sync_kl_main.ps1 fast-forwards the box's main to GitHub main and
does nothing else. Sessions land commits on the box's main, and pushing them is
Jack's call, so the box is often ahead of GitHub. When GitHub main then moves on
as well, a fast-forward is impossible, and the sync failed every 30 minutes until
someone merged by hand. Each wedge:
  - 11/24 00:45 to 11/25 09:43 ET (merge e9f2e06). GitHub's side was two
    high-temp digests, run-logs/high-temp/*.html only.
  - 11/25 17:15 to 19:04 ET (merge e072f34). GitHub's side held real code
    (a599e18, 6d37871: 26 paths outside run-logs/). This merge refuses that
    case on purpose: merging code is a person's job, so it stays a loud failure.
  - 11/26 22:45 to 11/27 07:49 ET (merge 3c787f7). The 11/25 digest only.

THE RULE. Every check must pass, or nothing changes:
  1. the off switch is not set: KL_SYNC_AUTO_MERGE=0, or the file
     run-logs/sync-auto-merge.off;
  2. on branch main, no merge in progress, nothing staged;
  3. diverged: the box is ahead of origin/main AND behind it;
  4. every path any incoming commit (HEAD..origin/main) touches is under
     run-logs/ (renames count both sides, so code renamed into run-logs/ fails);
  5. git merge-tree merges HEAD and origin/main with no conflict. That happens
     in the object store, never in the live tree, so a conflict cannot leave
     the live tree mid-merge;
  6. the merged tree differs from HEAD only under run-logs/;
  7. none of those paths is modified, untracked or ignored-but-present in the
     working tree.
Then git commit-tree writes the merge commit (parents HEAD and origin/main), and
git merge --ff-only moves main to it, the same step the sync takes every 30
minutes. That writes only those run-logs/ files:
  - other sessions' uncommitted files are untouched;
  - incentive_mm.py does not change, so the bot does not restart.
The box stays ahead of GitHub (its own commits plus the merge); pushing stays
Jack's call.

Exit codes: 0 merged (with --dry-run: would merge), 1 refused, 2 error. Either
way one ASCII line goes to stdout, and the sync appends it to its log.
"""
import argparse
import os
import subprocess
import sys
from typing import Dict, List, Tuple

ALLOW_PREFIX = "run-logs/"
OFF_FILE = "run-logs/sync-auto-merge.off"
UPSTREAM = "origin/main"
MAX_FILES = 200          # a digest backlog is a handful; more is not routine


def git(repo: str, *args: str) -> Tuple[int, str, str]:
    p = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false"] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def _lines(s: str) -> List[str]:
    return [l.strip() for l in s.splitlines() if l.strip()]


def _short(paths: List[str], n: int = 4) -> str:
    more = f" +{len(paths) - n} more" if len(paths) > n else ""
    return ", ".join(paths[:n]) + more


def check(repo: str, upstream: str = UPSTREAM) -> Dict:
    """Every check, changing nothing. The first failure returns
    {"ok": False, "reason": ...} ("error": True when git itself failed);
    success returns what the merge would do."""
    def no(reason: str, error: bool = False) -> Dict:
        return {"ok": False, "reason": reason, "error": error}

    if os.environ.get("KL_SYNC_AUTO_MERGE", "1").strip() == "0":
        return no("off (KL_SYNC_AUTO_MERGE=0)")
    if os.path.exists(os.path.join(repo, *OFF_FILE.split("/"))):
        return no(f"off ({OFF_FILE} exists)")
    rc, br, _ = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if rc != 0 or br.strip() != "main":
        return no(f"not on main ({br.strip() or 'detached HEAD'})")
    if git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        return no("a merge is in progress in the working tree")
    if git(repo, "diff", "--cached", "--quiet")[0] != 0:
        return no("something is staged in the working tree (a session mid-commit?)")
    rc, head, err = git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    rc2, up, err2 = git(repo, "rev-parse", "--verify", "-q", upstream + "^{commit}")
    if rc != 0 or rc2 != 0:
        return no(f"cannot resolve HEAD or {upstream}", error=True)
    head, up = head.strip(), up.strip()
    rc, ab, err = git(repo, "rev-list", "--left-right", "--count", f"{head}...{up}")
    if rc != 0 or len(ab.split()) < 2:
        return no("rev-list failed: " + err.strip()[:160], error=True)
    ahead, behind = (int(x) for x in ab.split()[:2])
    if behind == 0:
        return no(f"not behind {upstream} (nothing to merge)")
    if ahead == 0:
        return no(f"not ahead of {upstream} (a plain fast-forward applies)")

    rc, names, err = git(repo, "log", "--no-renames", "--name-only", "--format=", f"{head}..{up}")
    if rc != 0:
        return no("git log failed: " + err.strip()[:160], error=True)
    incoming = sorted(set(_lines(names)))
    outside = [p for p in incoming if not p.startswith(ALLOW_PREFIX)]
    if outside:
        return no(f"{behind} incoming commit(s) touch {len(outside)} path(s) outside "
                  f"{ALLOW_PREFIX}: {_short(outside)}")

    rc, mt, err = git(repo, "merge-tree", "--write-tree", "--no-messages", "--name-only", head, up)
    if rc == 1:
        return no("the merge conflicts in " + (_short(_lines(mt)[1:]) or "an unnamed path"))
    if rc != 0 or not _lines(mt):
        return no("git merge-tree failed: " + err.strip()[:160], error=True)
    tree = _lines(mt)[0]

    rc, d, err = git(repo, "diff", "--name-only", "--no-renames", head, tree)
    if rc != 0:
        return no("git diff failed: " + err.strip()[:160], error=True)
    changed = _lines(d)
    outside = [p for p in changed if not p.startswith(ALLOW_PREFIX)]
    if outside:
        return no(f"the merge would change {len(outside)} path(s) outside {ALLOW_PREFIX}: {_short(outside)}")
    if len(changed) > MAX_FILES:
        return no(f"the merge would write {len(changed)} files (more than {MAX_FILES}); merge by hand")
    if changed:
        rc, st, err = git(repo, "status", "--porcelain=v1", "--ignored", "--untracked-files=all",
                          "--", *changed)
        if rc != 0:
            return no("git status failed: " + err.strip()[:160], error=True)
        busy = [l[3:] for l in st.splitlines() if l.strip()]
        if busy:
            return no(f"{len(busy)} incoming file(s) are modified, untracked or ignored here: {_short(busy)}")
    return {"ok": True, "head": head, "up": up, "tree": tree, "changed": changed,
            "ahead": ahead, "behind": behind}


def merge(repo: str, dry: bool = False, upstream: str = UPSTREAM) -> Tuple[int, str]:
    c = check(repo, upstream)
    if not c["ok"]:
        return (2 if c.get("error") else 1), c["reason"]
    files = _short(c["changed"], 3) or "no file changes"
    if dry:
        return 0, f"would merge {c['behind']} run-logs-only commit(s) from {upstream} ({files})"
    subject = (f"Merge GitHub main ({c['behind']} run-logs-only commit(s)) into the box's main "
               "[KL sync auto-merge]")
    body = (f"The box's main held {c['ahead']} commit(s) that GitHub main lacks, so the ff-only sync "
            "could not fast-forward. Every incoming path is under run-logs/ and the merge is clean, so "
            "kl_sync_automerge.py merged it (R-261125-02): the live tree gains only the files below, "
            "and no bot code changes.\n\nIncoming: " + (", ".join(c["changed"][:30]) or "no file changes")
            + ("" if len(c["changed"]) <= 30 else f" +{len(c['changed']) - 30} more"))
    rc, m, err = git(repo, "commit-tree", c["tree"], "-p", c["head"], "-p", c["up"],
                     "-m", subject, "-m", body)
    if rc != 0 or not m.strip():
        return 2, "git commit-tree failed: " + err.strip()[:200]
    m = m.strip()
    rc, out, err = git(repo, "merge", "--ff-only", "-q", m)
    if rc != 0:
        return 2, f"fast-forward to the merge {m[:7]} failed (nothing changed): {(err or out).strip()[:200]}"
    rc, now, _ = git(repo, "rev-parse", "HEAD")
    if now.strip() != m:
        return 2, f"HEAD is {now.strip()[:7]}, not the merge {m[:7]}"
    return 0, (f"merged {c['behind']} run-logs-only commit(s) from {upstream} as {m[:7]} ({files}); "
               f"the box is {c['ahead'] + 1} commit(s) ahead of GitHub until a push")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="the repository to merge in (no default, on purpose)")
    ap.add_argument("--dry-run", action="store_true", help="run every check, change nothing")
    a = ap.parse_args(argv)
    try:
        code, line = merge(a.repo, dry=a.dry_run)
    except Exception as e:  # the sync must get a line, never a traceback
        code, line = 2, f"{type(e).__name__}: {e}"
    print(line.encode("ascii", "replace").decode("ascii"))
    return code


if __name__ == "__main__":
    sys.exit(main())
