"""Dolt's version control in SQL, checked against a model of its docs.

A seeded sequence of the operations users run (writes, dolt_commit,
dolt_branch, dolt_checkout, dolt_merge with conflicts resolved --ours or
--theirs, dolt_reset --hard, dolt_revert, dolt_cherry_pick) runs against the
subject and against a model of the documented semantics:

- A commit is a snapshot of the table, rows keyed by primary key.
- A merge is three-way and cell-wise (concepts/git/conflicts): a cell takes
  the side that changed it; both sides changing one cell to different values
  is a conflict, as is a delete on one side and a change on the other. A
  merge whose target is an ancestor fast-forwards (concepts/git/merge).
- dolt_conflicts_resolve('--ours'|'--theirs') takes that side's row for each
  conflicted row.
- A cherry-pick applies one commit's changes (a three-way merge from its
  parent); a revert applies the inverse of a commit's changes.

After every operation the working table must equal the model. At the end,
every commit read AS OF its hash must equal its snapshot, and dolt_diff
between commits must list exactly the rows whose snapshots differ.

Usage: python vc_model.py SUBJECT [SEEDS] [STEPS]
"""
import json
import os
import random
import sys

import psycopg

from hermitage import HOSTS, log

COLS = ("a", "b")


def merge3(base, ours, theirs):
    """The documented cell-wise merge of one row. None means absent.
    Returns (row, conflict)."""
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False
    if ours is None or theirs is None:
        return None, True  # deleted on one side, changed on the other
    if os.environ.get("MUTATE") == "rowwise":
        return None, True  # negative control: a row-level merge, which the docs rule out
    out = []
    for i in range(len(COLS)):
        b = base[i] if base else None
        o, t = ours[i], theirs[i]
        if o == t:
            out.append(o)
        elif o == b:
            out.append(t)
        elif t == b:
            out.append(o)
        else:
            return None, True
    return tuple(out), False


def merge_tables(base, ours, theirs):
    out, conflicts = {}, {}
    for pk in sorted(set(base) | set(ours) | set(theirs)):
        row, c = merge3(base.get(pk), ours.get(pk), theirs.get(pk))
        if c:
            conflicts[pk] = (ours.get(pk), theirs.get(pk))
            row = ours.get(pk)  # unresolved: ours stays until resolution
        if row is not None:
            out[pk] = row
    return out, conflicts


class Model:
    def __init__(self, init_hash):
        self.snap = {init_hash: {}}        # commit -> {pk: (a, b)}
        self.parents = {init_hash: []}
        self.heads = {"main": init_hash}
        self.branch = "main"
        self.work = {}

    def head(self):
        return self.heads[self.branch]

    def ancestors(self, c):
        seen, stack = set(), [c]
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(self.parents[x])
        return seen

    def merge_base(self, a, b):
        common = self.ancestors(a) & self.ancestors(b)
        # the common ancestor that is not an ancestor of another common one
        best = [c for c in common if not any(c != d and c in self.ancestors(d) for d in common)]
        return best[0] if len(best) == 1 else None

    def add_commit(self, h, parents, snap):
        self.snap[h] = dict(snap)
        self.parents[h] = parents
        self.heads[self.branch] = h
        self.work = dict(snap)


class Subject:
    def __init__(self, conninfo):
        self.c = psycopg.connect(**conninfo, autocommit=True)

    def q(self, sql, args=()):
        cur = self.c.execute(sql, args)
        return cur.fetchall() if cur.description else None

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        v = rows[0][0]
        return v[0] if isinstance(v, list) else v

    def table(self, rev=None):
        sql = "SELECT pk, a, b FROM t" + (f" AS OF '{rev}'" if rev else "") + " ORDER BY pk"
        return {r[0]: (r[1], r[2]) for r in self.q(sql)}


class Run:
    def __init__(self, conninfo, seed, steps):
        self.rng = random.Random(seed)
        self.seed, self.steps = seed, steps
        self.s = Subject(conninfo)
        self.violations = {k: [] for k in ("vc.merge_no_silent_loss", "vc.history_immutable", "vc.diff_complete",
                                           "vc.reset_restores", "vc.revert_inverse")}
        self.ops = {}
        self.db = f"vc{seed}"
        boot = Subject(conninfo)
        boot.q(f"DROP DATABASE IF EXISTS {self.db}")
        boot.q(f"CREATE DATABASE {self.db}")
        boot.c.close()
        self.s = Subject({**conninfo, "dbname": self.db})
        self.s.q("CREATE TABLE t (pk int PRIMARY KEY, a int, b int)")
        h = self.s.one("SELECT dolt_commit('-Am', 'create t')")
        self.m = Model(h)
        self.nbranch = 0

    def bad(self, inv, what):
        self.violations[inv].append(what)
        log(f"  VIOLATION {inv}: {what}")

    def expect_work(self, inv, what):
        got, want = self.s.table(), self.m.work
        if got != want:
            diff = {pk: (want.get(pk), got.get(pk)) for pk in set(got) | set(want) if got.get(pk) != want.get(pk)}
            self.bad(inv, f"seed {self.seed} after {what}: rows (model, subject) {dict(list(diff.items())[:5])}")
            self.m.work = got  # resync so one divergence is reported once

    def count(self, op):
        self.ops[op] = self.ops.get(op, 0) + 1

    # operations ------------------------------------------------------------

    def write(self):
        rng, pk = self.rng, self.rng.randrange(12)
        if pk in self.m.work and rng.random() < 0.25:
            self.s.q("DELETE FROM t WHERE pk = %s", (pk,))
            del self.m.work[pk]
        elif pk in self.m.work:
            col = rng.choice(COLS)
            v = rng.randrange(100)
            self.s.q(f"UPDATE t SET {col} = %s WHERE pk = %s", (v, pk))
            row = list(self.m.work[pk])
            row[COLS.index(col)] = v
            self.m.work[pk] = tuple(row)
        else:
            row = (rng.randrange(100), rng.randrange(100))
            self.s.q("INSERT INTO t VALUES (%s, %s, %s)", (pk, *row))
            self.m.work[pk] = row
        self.count("write")

    def commit(self):
        if self.m.work == self.m.snap[self.m.head()]:
            return
        h = self.s.one("SELECT dolt_commit('-Am', %s)", (f"step on {self.m.branch}",))
        self.m.add_commit(h, [self.m.head()], self.m.work)
        self.count("commit")

    def clean(self):
        """Commit pending writes, so branch switches and merges start clean."""
        self.commit()

    def branch(self):
        self.clean()
        self.nbranch += 1
        name = f"b{self.nbranch}"
        self.s.q("SELECT dolt_branch(%s)", (name,))
        self.m.heads[name] = self.m.head()
        self.count("branch")

    def checkout(self):
        self.clean()
        name = self.rng.choice(sorted(self.m.heads))
        self.s.q("SELECT dolt_checkout(%s)", (name,))
        self.m.branch = name
        self.m.work = dict(self.m.snap[self.m.head()])
        self.expect_work("vc.merge_no_silent_loss", f"checkout {name}")
        self.count("checkout")

    def merge(self):
        self.clean()
        others = [b for b in self.m.heads if b != self.m.branch]
        if not others:
            return
        other = self.rng.choice(sorted(others))
        ours_c, theirs_c = self.m.head(), self.m.heads[other]
        if theirs_c in self.m.ancestors(ours_c):
            return  # already up to date
        base_c = self.m.merge_base(ours_c, theirs_c)
        if base_c is None:
            return  # criss-cross history: no single documented base
        side = self.rng.choice(["--ours", "--theirs"])
        self.s.q("BEGIN")
        row = self.s.q("SELECT hash, fast_forward, conflicts FROM dolt_merge(%s)", (other,))[0]
        h, ff, conflicts = row[0], int(row[1]), int(row[2])
        if base_c == ours_c:  # fast-forward
            self.s.q("COMMIT")
            if not ff:
                self.bad("vc.merge_no_silent_loss", f"seed {self.seed}: merge of {other} should fast-forward")
            self.m.heads[self.m.branch] = theirs_c
            self.m.work = dict(self.m.snap[theirs_c])
            self.expect_work("vc.merge_no_silent_loss", f"fast-forward merge of {other}")
            self.count("merge_ff")
            return
        merged, conf = merge_tables(self.m.snap[base_c], self.m.snap[ours_c], self.m.snap[theirs_c])
        if bool(conflicts) != bool(conf):
            self.bad("vc.merge_no_silent_loss",
                     f"seed {self.seed}: merge of {other} reported conflicts={conflicts}, the model expects {sorted(conf)}")
        if conflicts:
            got = {r[0] for r in self.s.q("SELECT our_pk FROM dolt_conflicts_t")} | \
                  {r[0] for r in self.s.q("SELECT their_pk FROM dolt_conflicts_t")}
            got.discard(None)
            if got != set(conf):
                self.bad("vc.merge_no_silent_loss",
                         f"seed {self.seed}: conflicted rows {sorted(got)}, the model expects {sorted(conf)}")
            self.s.q("SELECT dolt_conflicts_resolve(%s, 't')", (side,))
            for pk, (o, t) in conf.items():
                pick = o if side == "--ours" else t
                if pick is None:
                    merged.pop(pk, None)
                else:
                    merged[pk] = pick
            h = self.s.one("SELECT dolt_commit('-Am', %s)", (f"merge {other} {side}",))
            self.count("merge_conflict")
        else:
            self.s.q("COMMIT")
            self.count("merge")
        self.m.add_commit(h, [ours_c, theirs_c], merged)
        self.expect_work("vc.merge_no_silent_loss", f"merge of {other} ({len(conf)} conflicts, {side})")

    def reset(self):
        for _ in range(self.rng.randint(1, 3)):
            self.write()
        self.s.q("SELECT dolt_reset('--hard')")
        self.m.work = dict(self.m.snap[self.m.head()])
        self.expect_work("vc.reset_restores", "dolt_reset('--hard')")
        self.count("reset")

    def revert(self):
        self.clean()
        head = self.m.head()
        if len(self.m.parents[head]) != 1 or not self.m.parents[self.m.parents[head][0]]:
            return
        parent = self.m.parents[head][0]
        row = self.s.q("SELECT hash, data_conflicts FROM dolt_revert('HEAD')")[0]
        if int(row[1]):
            self.bad("vc.revert_inverse", f"seed {self.seed}: reverting HEAD reported conflicts")
            self.s.q("SELECT dolt_reset('--hard')")
            return
        self.m.add_commit(row[0], [head], self.m.snap[parent])
        self.expect_work("vc.revert_inverse", "dolt_revert('HEAD')")
        self.count("revert")

    def cherry_pick(self):
        self.clean()
        here = self.m.ancestors(self.m.head())
        picks = [c for c in self.m.snap if c not in here and len(self.m.parents[c]) == 1]
        if not picks:
            return
        c = self.rng.choice(sorted(picks))
        parent = self.m.parents[c][0]
        merged, conf = merge_tables(self.m.snap[parent], self.m.work, self.m.snap[c])
        if conf or merged == self.m.work:
            return  # pick only clean, non-empty applications
        row = self.s.q("SELECT hash, data_conflicts FROM dolt_cherry_pick(%s)", (c,))[0]
        if int(row[1]):
            self.bad("vc.merge_no_silent_loss", f"seed {self.seed}: cherry-pick of {c[:8]} reported conflicts; the model expects none")
            self.s.q("SELECT dolt_reset('--hard')")
            return
        self.m.add_commit(row[0], [self.m.head()], merged)
        self.expect_work("vc.merge_no_silent_loss", f"cherry-pick of {c[:8]}")
        self.count("cherry_pick")

    # final checks ----------------------------------------------------------

    def check_history(self):
        for h, snap in self.m.snap.items():
            got = self.s.table(h)
            if got != snap:
                self.bad("vc.history_immutable", f"seed {self.seed}: AS OF {h[:8]} differs from its snapshot")

    def check_diffs(self, pairs=25):
        commits = sorted(self.m.snap)
        for _ in range(pairs):
            a, b = self.rng.sample(commits, 2)
            want = {pk for pk in set(self.m.snap[a]) | set(self.m.snap[b]) if self.m.snap[a].get(pk) != self.m.snap[b].get(pk)}
            assert a.isalnum() and b.isalnum()
            # Literals, not bind parameters: dolt_diff() rejects those (#3753).
            rows = self.s.q(f"SELECT from_pk, to_pk FROM dolt_diff('{a}', '{b}', 't')")
            got = {r[1] if r[1] is not None else r[0] for r in rows}
            if got != want:
                self.bad("vc.diff_complete", f"seed {self.seed}: dolt_diff {a[:8]}..{b[:8]} lists {sorted(got)}, the snapshots differ at {sorted(want)}")

    def go(self):
        ops = [(self.write, 40), (self.commit, 14), (self.branch, 6), (self.checkout, 12), (self.merge, 24),
               (self.reset, 4), (self.revert, 3), (self.cherry_pick, 12)]
        fns, weights = zip(*ops)
        for _ in range(self.steps):
            self.rng.choices(fns, weights)[0]()
        self.clean()
        self.check_history()
        self.check_diffs()
        self.s.c.close()


def main(subject, seeds=4, steps=150):
    totals = {}
    ops = {}
    for seed in range(1, seeds + 1):
        r = Run(HOSTS[subject], seed, steps)
        r.go()
        for k, v in r.violations.items():
            totals[k] = totals.get(k, 0) + len(v)
        for k, v in r.ops.items():
            ops[k] = ops.get(k, 0) + v
        log(f"seed {seed}: {len(r.m.snap)} commits, {len(r.m.heads)} branches")
    log(f"operations run: {ops}")
    for need in ("merge", "merge_conflict", "merge_ff", "reset", "revert", "cherry_pick"):
        if ops.get(need, 0) == 0:
            raise SystemExit(f"no {need} ran: the sequence tests too little")
    # Each feature is decided by the check that exercises it, never by a
    # statement merely not failing.
    features = {
        "vc.commit": "vc.history_immutable", "vc.as_of": "vc.history_immutable",
        "vc.branch.create": "vc.merge_no_silent_loss", "vc.checkout": "vc.merge_no_silent_loss",
        "vc.merge.fast_forward": "vc.merge_no_silent_loss", "vc.conflicts": "vc.merge_no_silent_loss",
        "vc.conflicts_resolve": "vc.merge_no_silent_loss", "vc.cherry_pick": "vc.merge_no_silent_loss",
        "vc.reset.hard": "vc.reset_restores", "vc.revert": "vc.revert_inverse", "vc.diff_fn": "vc.diff_complete",
    }
    out = dict(totals)
    out.update({f: totals[inv] for f, inv in features.items()})
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1], *(int(a) for a in sys.argv[2:]))
