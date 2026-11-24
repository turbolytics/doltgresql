"""Hermitage's isolation tests, run against one subject.

Martin Kleppmann's Hermitage (github.com/ept/hermitage @f029bec, postgres.md)
scripts each anomaly as statements from two or three sessions in a fixed
order, and says which isolation levels PostgreSQL prevents it at. This oracle
runs every script at every level, one connection per session. A statement
that does not return within BLOCK seconds is blocked: the script moves on,
and the session's next statement waits for it.

An anomaly is decided from what the sessions observed (the rows each read,
which commits succeeded, and the final state), never from whether a
statement blocked: Doltgres commits optimistically and does not block where
PostgreSQL does. Each test names the anomaly it detects and the levels that
must prevent it, from PostgreSQL's documented guarantees.

Usage: python hermitage.py SUBJECT          prints the verdict JSON
       python hermitage.py SUBJECT --explain  also prints every observation
"""
import json
import os
import queue
import sys
import threading
import time

import psycopg
import yaml

BLOCK = 1.5      # seconds before a statement counts as blocked
FINISH = 20      # seconds a blocked statement may take once unblocked
LEVELS = ["read committed", "repeatable read", "serializable"]
RC, RR, SER = LEVELS
HOSTS = {
    "postgres.reference": dict(host="db_postgres", user="postgres", password="password", dbname="postgres"),
    "doltgres.release": dict(host="db_release", user="postgres", password="password", dbname="postgres"),
    "doltgres.main": dict(host="db_main", user="postgres", password="password", dbname="postgres"),
}
SETUP = ["DROP TABLE IF EXISTS test",
         "CREATE TABLE test (id int PRIMARY KEY, value int)",
         "INSERT INTO test (id, value) VALUES (1, 10), (2, 20)"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Session:
    """One connection, run by its own thread so a blocked statement can wait."""

    def __init__(self, conninfo, name):
        self.name = name
        self.conn = psycopg.connect(**conninfo, autocommit=True, connect_timeout=10)
        self.jobs = queue.Queue()
        self.pending = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            sql, done = self.jobs.get()
            out = {"sql": sql}
            try:
                cur = self.conn.execute(sql)
                out["rows"] = sorted(tuple(r) for r in cur.fetchall()) if cur.description else None
            except psycopg.Error as e:
                out["error"] = (e.sqlstate or "?") + " " + str(e).splitlines()[0][:160]
            done.put(out)

    def send(self, sql):
        self.wait()
        done = queue.Queue()
        self.jobs.put((sql, done))
        try:
            res = done.get(timeout=BLOCK)
            res["blocked"] = False
            return res
        except queue.Empty:
            self.pending = (sql, done)
            return {"sql": sql, "blocked": True}

    def wait(self):
        if self.pending:
            sql, done = self.pending
            self.pending = None
            try:
                res = done.get(timeout=FINISH)
            except queue.Empty:
                res = {"sql": sql, "error": "timeout still blocked"}
            res["blocked"] = True
            return res
        return None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def rows(r):
    return dict(r.get("rows") or [])


def ok(r):
    return r is not None and "error" not in r


class Run:
    """One script at one level: sessions, a log of observations, the final state."""

    def __init__(self, conninfo, level):
        self.conninfo, self.level = conninfo, level
        self.s, self.obs, self.late = {}, [], {}

    def begin(self, t):
        self.s[t] = Session(self.conninfo, t)
        r = self.s[t].send(f"BEGIN ISOLATION LEVEL {self.level.upper()}")
        self.obs.append((t, r))
        return r

    def do(self, t, sql):
        prev = self.s[t].wait()
        if prev:
            self.obs.append((t, prev))
            self.late[(t, prev["sql"])] = prev
        r = self.s[t].send(sql)
        self.obs.append((t, r))
        return r

    def result(self, t, r):
        """The final outcome of a statement that may have blocked."""
        if not r.get("blocked") or "rows" in r or "error" in r:
            return r
        late = self.s[t].wait()
        if late:
            self.obs.append((t, late))
            return late
        return self.late.get((t, r["sql"]), r)

    def final(self):
        for t, s in self.s.items():
            late = s.wait()
            if late:
                self.obs.append((t, late))
            s.close()
        with psycopg.connect(**self.conninfo, autocommit=True) as c:
            return dict(c.execute("SELECT id, value FROM test").fetchall())


# Each test returns True when it observed the anomaly. `must` lists the levels
# PostgreSQL's guarantees say prevent it.

def g0(r):
    """Write cycles: both commit, and the final state mixes their writes."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    a = r.do("T2", "UPDATE test SET value = 12 WHERE id = 1")
    r.do("T1", "UPDATE test SET value = 21 WHERE id = 2")
    c1 = r.do("T1", "COMMIT")
    r.result("T2", a)
    r.do("T2", "UPDATE test SET value = 22 WHERE id = 2")
    c2 = r.result("T2", r.do("T2", "COMMIT"))
    f = r.final()
    return ok(c1) and ok(c2) and f not in ({1: 12, 2: 22}, {1: 11, 2: 21})


def g1a(r):
    """Aborted reads: T2 sees a write T1 then rolled back."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = 101 WHERE id = 1")
    x = r.do("T2", "SELECT * FROM test")
    r.do("T1", "ROLLBACK")
    y = r.do("T2", "SELECT * FROM test")
    r.do("T2", "COMMIT")
    r.final()
    return rows(x).get(1) == 101 or rows(y).get(1) == 101


def g1b(r):
    """Intermediate reads: T2 sees a value T1 later overwrote."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = 101 WHERE id = 1")
    x = r.do("T2", "SELECT * FROM test")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    r.do("T1", "COMMIT")
    y = r.do("T2", "SELECT * FROM test")
    r.do("T2", "COMMIT")
    r.final()
    return rows(x).get(1) == 101 or rows(y).get(1) == 101


def g1c(r):
    """Circular information flow: each sees the other's uncommitted write."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    r.do("T2", "UPDATE test SET value = 22 WHERE id = 2")
    x = r.do("T1", "SELECT * FROM test WHERE id = 2")
    y = r.do("T2", "SELECT * FROM test WHERE id = 1")
    r.do("T1", "COMMIT")
    r.result("T2", r.do("T2", "COMMIT"))
    r.final()
    return rows(x).get(2) == 22 or rows(y).get(1) == 11


def otv(r):
    """Observed transaction vanishes: T3 sees T2's write, then T1's overwritten one."""
    r.begin("T1"); r.begin("T2"); r.begin("T3")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    r.do("T1", "UPDATE test SET value = 19 WHERE id = 2")
    a = r.do("T2", "UPDATE test SET value = 12 WHERE id = 1")
    r.do("T1", "COMMIT")
    r.do("T3", "SELECT * FROM test WHERE id = 1")
    r.result("T2", a)
    r.do("T2", "UPDATE test SET value = 18 WHERE id = 2")
    mid = r.do("T3", "SELECT * FROM test WHERE id = 2")
    r.result("T2", r.do("T2", "COMMIT"))
    y2 = r.do("T3", "SELECT * FROM test WHERE id = 2")
    y1 = r.do("T3", "SELECT * FROM test WHERE id = 1")
    r.do("T3", "COMMIT")
    r.final()
    dirty = rows(mid).get(2) == 18
    vanished = rows(y2).get(2) == 18 and rows(y1).get(1) == 11
    return dirty or vanished


def pmp(r):
    """Predicate-many-preceders: a repeated predicate read sees a new row."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE value = 30")
    r.do("T2", "INSERT INTO test (id, value) VALUES (3, 30)")
    r.do("T2", "COMMIT")
    y = r.do("T1", "SELECT * FROM test WHERE value % 3 = 0")
    r.do("T1", "COMMIT")
    r.final()
    return 3 in rows(y)


def pmp_write(r):
    """PMP for write predicates: both commit, and the result matches no serial order."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = value + 10")
    d = r.do("T2", "DELETE FROM test WHERE value = 20")
    c1 = r.do("T1", "COMMIT")
    d = r.result("T2", d)
    c2 = r.result("T2", r.do("T2", "COMMIT")) if ok(d) else None
    f = r.final()
    return ok(c1) and ok(c2) and f not in ({2: 30}, {1: 20})


def p4(r):
    """Lost update: both read 10, write a value computed from it, and both commit."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE id = 1")
    r.do("T2", "SELECT * FROM test WHERE id = 1")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    a = r.do("T2", "UPDATE test SET value = 12 WHERE id = 1")
    c1 = r.do("T1", "COMMIT")
    a = r.result("T2", a)
    c2 = r.result("T2", r.do("T2", "COMMIT")) if ok(a) else None
    r.final()
    return ok(c1) and ok(c2)


def p4_increment(r):
    """Lost increment: two UPDATE value = value + 1 commit, and the value rises by one."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE test SET value = value + 1 WHERE id = 1")
    a = r.do("T2", "UPDATE test SET value = value + 1 WHERE id = 1")
    c1 = r.do("T1", "COMMIT")
    a = r.result("T2", a)
    c2 = r.result("T2", r.do("T2", "COMMIT")) if ok(a) else None
    f = r.final()
    return ok(c1) and ok(c2) and f.get(1) != 12


def g_single(r):
    """Read skew: T1 reads 1 before T2's commit and 2 after it."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE id = 1")
    r.do("T2", "SELECT * FROM test WHERE id = 1")
    r.do("T2", "SELECT * FROM test WHERE id = 2")
    r.do("T2", "UPDATE test SET value = 12 WHERE id = 1")
    r.do("T2", "UPDATE test SET value = 18 WHERE id = 2")
    r.do("T2", "COMMIT")
    y = r.do("T1", "SELECT * FROM test WHERE id = 2")
    r.do("T1", "COMMIT")
    r.final()
    return rows(y).get(2) == 18


def g_single_predicate(r):
    """Read skew through predicates: T1's second predicate read sees T2's change."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE value % 5 = 0")
    r.do("T2", "UPDATE test SET value = 12 WHERE value = 10")
    r.do("T2", "COMMIT")
    y = r.do("T1", "SELECT * FROM test WHERE value % 3 = 0")
    r.do("T1", "COMMIT")
    r.final()
    return 1 in rows(y)


def g_single_write(r):
    """Read skew through a write predicate: T1 commits a delete on a stale snapshot."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE id = 1")
    r.do("T2", "SELECT * FROM test")
    r.do("T2", "UPDATE test SET value = 12 WHERE id = 1")
    r.do("T2", "UPDATE test SET value = 18 WHERE id = 2")
    r.do("T2", "COMMIT")
    d = r.result("T1", r.do("T1", "DELETE FROM test WHERE value = 20"))
    c1 = r.result("T1", r.do("T1", "COMMIT")) if ok(d) else None
    r.final()
    return ok(c1)


def g2_item(r):
    """Write skew: both read 1 and 2, each updates one, and both commit."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE id IN (1, 2)")
    r.do("T2", "SELECT * FROM test WHERE id IN (1, 2)")
    r.do("T1", "UPDATE test SET value = 11 WHERE id = 1")
    r.do("T2", "UPDATE test SET value = 21 WHERE id = 2")
    c1 = r.result("T1", r.do("T1", "COMMIT"))
    c2 = r.result("T2", r.do("T2", "COMMIT"))
    r.final()
    return ok(c1) and ok(c2)


def g2(r):
    """Anti-dependency cycle: both read a predicate, each inserts into it, both commit."""
    r.begin("T1"); r.begin("T2")
    r.do("T1", "SELECT * FROM test WHERE value % 3 = 0")
    r.do("T2", "SELECT * FROM test WHERE value % 3 = 0")
    r.do("T1", "INSERT INTO test (id, value) VALUES (3, 30)")
    r.do("T2", "INSERT INTO test (id, value) VALUES (4, 42)")
    c1 = r.result("T1", r.do("T1", "COMMIT"))
    c2 = r.result("T2", r.do("T2", "COMMIT"))
    r.final()
    return ok(c1) and ok(c2)


def g2_two_edges(r):
    """Fekete et al.: a read-only T3 sees T2's commit, and T1 still commits on its old snapshot."""
    r.begin("T1")
    r.do("T1", "SELECT * FROM test")
    r.begin("T2")
    r.do("T2", "UPDATE test SET value = value + 5 WHERE id = 2")
    r.do("T2", "COMMIT")
    r.begin("T3")
    r.do("T3", "SELECT * FROM test")
    r.do("T3", "COMMIT")
    u = r.result("T1", r.do("T1", "UPDATE test SET value = 0 WHERE id = 1"))
    c1 = r.result("T1", r.do("T1", "COMMIT")) if ok(u) else None
    r.final()
    return ok(c1)


def cross_column(r):
    """Two updates to one row, each filtering on the column the other writes.
    T1 closes an account while its balance is 0; T2 deposits while it is open.
    PostgreSQL's read committed makes T2 wait and re-check its WHERE against
    the newest row (it then matches nothing); repeatable read and serializable
    fail one with 40001. No serial order ends closed with the deposit in it.
    (dolthub/doltgresql#3617 fixed the same-column case.)"""
    with psycopg.connect(**r.conninfo, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS acct")
        c.execute("CREATE TABLE acct (id int PRIMARY KEY, status text NOT NULL, balance int NOT NULL)")
        c.execute("INSERT INTO acct VALUES (1, 'open', 0)")
    r.begin("T1"); r.begin("T2")
    r.do("T1", "UPDATE acct SET status = 'closed' WHERE id = 1 AND balance = 0")
    a = r.do("T2", "UPDATE acct SET balance = balance + 100 WHERE id = 1 AND status = 'open'")
    c1 = r.do("T1", "COMMIT")
    a = r.result("T2", a)
    c2 = r.result("T2", r.do("T2", "COMMIT")) if ok(a) else None
    r.final()
    with psycopg.connect(**r.conninfo, autocommit=True) as c:
        final = c.execute("SELECT status, balance FROM acct WHERE id = 1").fetchone()
    return ok(c1) and ok(c2) and tuple(final) == ("closed", 100)


# test, the anomaly, the levels that must prevent it, the invariant it decides
TESTS = [
    (g0, "G0", [RC, RR, SER], "txn.no_dirty_write"),
    (g1a, "G1a", [RC, RR, SER], "txn.no_dirty_read"),
    (g1b, "G1b", [RC, RR, SER], "txn.no_dirty_read"),
    (g1c, "G1c", [RC, RR, SER], "txn.no_circular_info"),
    (otv, "OTV", [RC, RR, SER], None),
    (p4_increment, "P4 (UPDATE value = value + 1)", [RC, RR, SER], "txn.no_lost_update"),
    (cross_column, "Lost update across columns (#3617)", [RC, RR, SER], "txn.no_lost_update"),
    (pmp, "PMP", [RR, SER], None),
    (pmp_write, "PMP (write predicate)", [RR, SER], None),
    (p4, "P4", [RR, SER], "txn.no_lost_update"),
    (g_single, "G-single", [RR, SER], "txn.no_read_skew"),
    (g_single_predicate, "G-single (predicate)", [RR, SER], "txn.no_read_skew"),
    (g_single_write, "G-single (write predicate)", [RR, SER], "txn.no_read_skew"),
    (g2_item, "G2-item", [SER], "txn.no_write_skew"),
    (g2, "G2", [SER], "txn.no_write_skew"),
    (g2_two_edges, "G2 (two anti-dependency edges)", [SER], "txn.no_write_skew"),
]


def declared(subject):
    """The isolation levels the subject declares supported, from its file."""
    d = yaml.safe_load(open(os.path.join("/temper/subjects", subject + ".yml")))
    ans = d["answers"]["txn.isolation"]
    sup = [l for l in LEVELS if ans[l] == "supported" or (isinstance(ans[l], dict) and ans[l].get("supported"))]
    exempt = {e["invariant"] for e in d.get("exempt", [])}
    return sup, exempt


def refused(conninfo, level):
    """Requesting the level fails: it is not silently run at another."""
    with psycopg.connect(**conninfo, autocommit=True) as c:
        try:
            c.execute(f"BEGIN ISOLATION LEVEL {level.upper()}")
        except psycopg.Error:
            return True
        try:
            got = c.execute("SELECT current_setting('transaction_isolation')").fetchone()[0]
        except psycopg.Error:
            got = "?"
        c.execute("ROLLBACK")
        log(f"  {level}: accepted (transaction_isolation reports {got!r})")
        return False


def main(subject, explain=False):
    conninfo = HOSTS[subject]
    supported, exempt = declared(subject)
    verdict = {}
    anomalies = {}  # (invariant, level) -> count
    for level in LEVELS:
        if level not in supported:
            r = refused(conninfo, level)
            verdict[f"txn.isolation/{level}"] = 0 if r else 1
            log(f"{level}: declared unsupported, {'refused' if r else 'NOT refused'}")
            if r:
                continue
        bad = 0
        for fn, name, must, inv in TESTS:
            with psycopg.connect(**conninfo, autocommit=True) as c:
                for s in SETUP:
                    c.execute(s)
            run = Run(conninfo, level)
            try:
                seen = fn(run)
            except Exception as e:  # a script that cannot run is an error, not a pass
                raise SystemExit(f"{name} at {level} could not run: {e}")
            prevented_required = level in must
            mark = "ANOMALY" if seen else "prevented"
            if seen and prevented_required:
                bad += 1
                if inv:
                    anomalies[(inv, level)] = anomalies.get((inv, level), 0) + 1
            log(f"{level:16} {name:34} {mark}{'  (must prevent)' if prevented_required else ''}")
            if explain:
                for t, o in run.obs:
                    log(f"    {t}: {o}")
        if level in supported:
            verdict[f"txn.isolation/{level}"] = bad
    for inv in sorted({t[3] for t in TESTS if t[3]}):
        if inv in exempt:
            # The exemption's proof: every level the invariant needs is refused.
            need = [l for l in LEVELS if all(l in t[2] for t in TESTS if t[3] == inv)]
            verdict[inv] = sum(verdict.get(f"txn.isolation/{l}", 1) for l in need if l not in supported)
        else:
            verdict[inv] = sum(n for (i, _), n in anomalies.items() if i == inv)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1], "--explain" in sys.argv)
