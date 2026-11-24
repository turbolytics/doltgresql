"""Version-control operations across kill -9, against one Doltgres subject.

One client runs the operations users run (branch create and delete,
checkout, writes, dolt_commit, dolt_merge with conflicts resolved,
dolt_cherry_pick, dolt_revert) while the scenario kills the server. Every
operation records the branch head before it and its outcome. When the
connection breaks, the client reconnects and decides the interrupted
operation from what the server now shows:

- old or new, never broken: the head is the one from before, or a commit
  the operation could have made (its parents are the old head, plus the
  merged branch's head for a merge), or, for a fast-forward, the merged
  branch's head. No merge is left in progress.
- a branch either exists with its permission row (dolt_branch_control gains
  one per new branch), or does not exist; a branch whose creation never
  returned leaves no row.

At the end, every commit hash the server returned is still in the history,
and reads AS OF it exactly as it read when it was returned; every branch's
head reads.

Usage: python vc_crash.py run SUBJECT SECONDS
       python vc_crash.py running          (0 while the client runs, 1 once done)
       python vc_crash.py check SUBJECT    (the verdict JSON)
"""
import json
import os
import random
import sys
import time

import psycopg
from psycopg import pq

from hermitage import HOSTS, log

STATE = "/ledger/vc_crash.json"


def connect(conninfo):
    deadline = time.time() + 120
    while True:
        try:
            return psycopg.connect(**conninfo, autocommit=True, connect_timeout=5)
        except psycopg.Error:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def broken(e):
    """A broken connection leaves the outcome unknown; a server error does not."""
    return not e.sqlstate or isinstance(e, psycopg.OperationalError) and e.sqlstate not in ("40001", "40P01")


class Client:
    def __init__(self, subject, seed=5):
        self.conninfo = {**HOSTS[subject], "dbname": "vcc"}
        boot = connect(HOSTS[subject])
        boot.execute("DROP DATABASE IF EXISTS vcc")
        boot.execute("CREATE DATABASE vcc")
        boot.close()
        self.c = connect(self.conninfo)
        self.c.execute("CREATE TABLE t (pk int PRIMARY KEY, a int, b int)")
        h = self.c.execute("SELECT dolt_commit('-Am', 'create t')").fetchone()[0]
        self.rng = random.Random(seed)
        self.branch = "main"
        self.n = 0
        self.s = {"commits": {}, "violations": [], "created": [], "never_created": [], "deleted": [],
                  "ops": {}, "unknown": 0}
        self.remember(self.one(h))

    def one(self, v):
        return v[0] if isinstance(v, list) else v

    def q(self, sql, args=()):
        cur = self.c.execute(sql, args)
        return cur.fetchall() if cur.description else None

    def head(self, branch=None):
        b = branch or self.branch
        rows = self.q("SELECT hash FROM dolt_branches WHERE name = %s", (b,))
        return rows[0][0] if rows else None

    def table(self, rev):
        assert rev.replace("/", "").isalnum() or rev == "main"
        return sorted(self.q(f"SELECT pk, a, b FROM t AS OF '{rev}' ORDER BY pk"))

    def remember(self, h):
        """Record a returned commit and what it reads, while the server is up."""
        try:
            self.s["commits"][h] = [list(r) for r in self.table(h)]
        except psycopg.Error:
            self.s["commits"][h] = None  # the server died first: check only that it exists

    def parents(self, h):
        return {r[0] for r in self.q("SELECT parent_hash FROM dolt_commit_ancestors WHERE commit_hash = %s", (h,))}

    def bad(self, what):
        self.s["violations"].append(what)
        log(f"  VIOLATION: {what}")

    def reconnect(self):
        try:
            self.c.close()
        except Exception:
            pass
        self.c = connect(self.conninfo)
        if self.head(self.branch) is None:
            self.branch = "main"
        self.q("SELECT dolt_checkout(%s)", (self.branch,))

    def clear(self):
        """Roll back a transaction an earlier operation left open or failed."""
        if self.c.info.transaction_status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR):
            self.c.execute("ROLLBACK")

    def op(self, name, fn, decide):
        """Run one operation. On a broken connection, reconnect and decide it."""
        self.clear()
        before = {"head": self.head(), "branch": self.branch}
        self.s["ops"][name] = self.s["ops"].get(name, 0) + 1
        try:
            fn()
        except psycopg.Error as e:
            if not broken(e):
                try:
                    self.q("ROLLBACK")
                except psycopg.Error:
                    pass
                return
            self.s["unknown"] += 1
            self.reconnect()
            decide(before)
            merging = self.q("SELECT is_merging FROM dolt_merge_status")
            if merging and merging[0][0] in (True, 1, "true", "t"):
                self.bad(f"{name}: a merge is still in progress after the crash")
                self.q("SELECT dolt_merge('--abort')")

    def expect_old_or_child(self, name, before, extra_parent=None, ff_to=None):
        now = self.head(before["branch"])
        if now == before["head"] or (ff_to and now == ff_to):
            return
        want = {before["head"]} | ({extra_parent} if extra_parent else set())
        got = self.parents(now)
        if got != want:
            self.bad(f"{name}: head {now[:8]} has parents {sorted(p[:8] for p in got)}, expected the old head {before['head'][:8]} or a child of it")
        else:
            self.remember(now)

    # operations ------------------------------------------------------------

    def write(self):
        pk = self.rng.randrange(10)
        col = self.rng.choice(["a", "b"])
        v = self.rng.randrange(100)
        self.op("write", lambda: self.q(f"INSERT INTO t VALUES (%s, %s, %s) ON CONFLICT (pk) DO UPDATE SET {col} = %s",
                                         (pk, v, v, v)), lambda b: None)

    def commit(self):
        def fn():
            h = self.one(self.q("SELECT dolt_commit('-Am', 'work')")[0][0])
            self.remember(h)
        self.op("commit", fn, lambda b: self.expect_old_or_child("commit", b))

    def create(self):
        self.n += 1
        name = f"b{self.n}"

        def fn():
            self.q("SELECT dolt_branch(%s)", (name,))
            self.s["created"].append(name)

        def decide(b):
            if self.head(name) is not None:
                self.s["created"].append(name)
            else:
                self.s["never_created"].append(name)
        self.op("branch_create", fn, decide)

    def delete(self):
        names = [n for n in self.s["created"] if n != self.branch and n not in self.s["deleted"]]
        if not names:
            return
        name = self.rng.choice(names)

        def fn():
            self.q("SELECT dolt_branch('-D', %s)", (name,))
            self.s["deleted"].append(name)

        def decide(b):
            if self.head(name) is None:
                self.s["deleted"].append(name)
        self.op("branch_delete", fn, decide)

    def checkout(self):
        self.clear()
        names = ["main"] + [n for n in self.s["created"] if n not in self.s["deleted"]]
        name = self.rng.choice(names)
        try:
            self.q("SELECT dolt_commit('-Am', 'before checkout')")
        except psycopg.Error as e:
            if broken(e):
                self.reconnect()
            else:
                pass
        try:
            self.q("SELECT dolt_checkout(%s)", (name,))
            self.branch = name
        except psycopg.Error as e:
            if broken(e):
                self.reconnect()

    def merge(self):
        others = [n for n in ["main"] + self.s["created"] if n != self.branch and n not in self.s["deleted"]]
        if not others:
            return
        other = self.rng.choice(others)
        theirs = self.head(other)
        side = self.rng.choice(["--ours", "--theirs"])

        # Pending writes are committed as their own operation, so a crash
        # between that commit and the merge is judged as a commit.
        self.commit()
        self.clear()
        theirs = self.head(other)
        if theirs is None:
            return

        def fn():
            self.q("BEGIN")
            h, ff, conflicts = self.q("SELECT hash, fast_forward, conflicts FROM dolt_merge(%s)", (other,))[0]
            if int(conflicts):
                self.q("SELECT dolt_conflicts_resolve(%s, 't')", (side,))
                h = self.one(self.q("SELECT dolt_commit('-Am', 'merge resolved')")[0][0])
            else:
                self.q("COMMIT")
            if h:
                self.remember(self.head())
        self.op("merge", fn, lambda b: self.expect_old_or_child("merge", b, extra_parent=theirs, ff_to=theirs))

    def cherry_pick(self):
        here = self.head()
        picks = [h for h in self.s["commits"] if h != here]
        if not picks:
            return
        c = self.rng.choice(sorted(picks))

        def fn():
            h, conflicts = self.q("SELECT hash, data_conflicts FROM dolt_cherry_pick(%s)", (c,))[0][:2]
            if int(conflicts):
                self.q("SELECT dolt_reset('--hard')")
            elif h:
                self.remember(h)
        self.op("cherry_pick", fn, lambda b: self.expect_old_or_child("cherry_pick", b))

    def revert(self):
        def fn():
            h, conflicts = self.q("SELECT hash, data_conflicts FROM dolt_revert('HEAD')")[0][:2]
            if int(conflicts):
                self.q("SELECT dolt_reset('--hard')")
            elif h:
                self.remember(h)
        self.op("revert", fn, lambda b: self.expect_old_or_child("revert", b))

    def go(self, seconds):
        stop = time.time() + seconds
        ops = [(self.write, 30), (self.commit, 15), (self.create, 6), (self.delete, 3), (self.checkout, 10),
               (self.merge, 12), (self.cherry_pick, 6), (self.revert, 4)]
        fns, w = zip(*ops)
        while time.time() < stop:
            try:
                self.rng.choices(fns, w)[0]()
            except psycopg.Error as e:
                if not broken(e):
                    raise
                self.s["unknown"] += 1
                self.reconnect()
            json.dump(self.s, open(STATE + ".tmp", "w"))
            os.replace(STATE + ".tmp", STATE)
        self.s["done"] = True
        json.dump(self.s, open(STATE, "w"))


def running():
    try:
        print(0 if not json.load(open(STATE)).get("done") else 1)
    except Exception:
        print(0)


def check(subject):
    s = json.load(open(STATE))
    c = connect({**HOSTS[subject], "dbname": "vcc"})
    q = lambda sql, a=(): c.execute(sql, a).fetchall()
    v = list(s["violations"])
    # dolt.commits lists every commit in the database (dolt.log only those
    # reachable from HEAD). A returned commit must still be listed, and read
    # AS OF its hash as it did when it was returned.
    known = {r[0] for r in q("SELECT commit_hash FROM dolt_commits")}
    lost = changed = unlisted = 0
    for h, snap in s["commits"].items():
        assert h.isalnum()
        try:
            now = sorted(list(r) for r in q(f"SELECT pk, a, b FROM t AS OF '{h}' ORDER BY pk"))
        except psycopg.Error as e:
            lost += 1
            log(f"returned commit {h[:8]} no longer reads: {str(e).splitlines()[0][:120]}")
            continue
        if h not in known:
            unlisted += 1
        if snap is not None and now != snap:
            changed += 1
            log(f"commit {h[:8]} reads differently than when it was returned")
    if unlisted:
        # Not a crash effect: a run with no crashes shows the same. Record
        # whether the unlisted commits are the ones no branch reaches.
        reach = set()
        for (b,) in q("SELECT name FROM dolt_branches"):
            assert b.replace("_", "").isalnum()
            reach |= {r[0] for r in q(f"SELECT commit_hash FROM dolt_log('{b}')")}
        missing = [h for h in s["commits"] if h not in known]
        unreachable = sum(1 for h in missing if h not in reach)
        log(f"note: {unlisted} returned commits read intact but are missing from dolt.commits, "
            f"{unreachable} of them reachable from no branch (the docs say it lists all commits)")
    branches = {r[0]: r[1] for r in q("SELECT name, hash FROM dolt_branches")}
    unreadable = 0
    for b, h in branches.items():
        try:
            q(f"SELECT count(*) FROM t AS OF '{h}'")
        except psycopg.Error:
            unreadable += 1
            log(f"branch {b} head {h[:8]} does not read")
    rows = q("SELECT branch FROM dolt_branch_control")
    controlled = {r[0] for r in rows}
    orphan_branches = sorted(b for b in branches if b != "main" and b not in controlled)
    leaked_rows = sorted(n for n in s["never_created"] if n in controlled)
    log(f"operations {s['ops']}; interrupted {s['unknown']}; commits returned {len(s['commits'])}; branches {len(branches)}")
    log(f"in-run violations {len(v)}; lost commits {lost}; changed {changed}; unreadable heads {unreadable}; "
        f"branches without a permission row {orphan_branches}; rows for never-created branches {leaked_rows}")
    deleted_rows = sorted(n for n in s["deleted"] if n in controlled)
    if deleted_rows:
        log(f"note: deleted branches keep their permission rows ({len(deleted_rows)}), as without crashes")
    if (s["unknown"] == 0 and not os.environ.get("NO_CRASH_CONTROL")) or s["ops"].get("merge", 0) < 5:
        raise SystemExit("no crash interrupted an operation, or too few merges ran: the run tests nothing")
    out = {
        "durability.no_partial_after_crash": len(v) + unreadable,
        "vc.history_immutable": lost + changed,
        "vc.branch_ops_atomic": len(orphan_branches) + len(leaked_rows),
        "vc.commit": lost + changed,
        "vc.merge.fast_forward": sum(1 for x in v if x.startswith("merge")),
        "vc.cherry_pick": sum(1 for x in v if x.startswith("cherry_pick")),
        "vc.revert": sum(1 for x in v if x.startswith("revert")),
        "vc.branch.create": len(orphan_branches) + len(leaked_rows),
    }
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        Client(sys.argv[2]).go(float(sys.argv[3]))
    elif cmd == "running":
        running()
    elif cmd == "check":
        check(sys.argv[2])
