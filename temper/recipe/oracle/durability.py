"""Durability and atomicity across kill -9, against one subject.

While the scenario kills and restarts the database, writers commit three
kinds of work and record each outcome the moment it arrives, in a ledger on
the oracle's volume:

- ledger inserts: one row per id, committed alone. An id whose COMMIT
  returned must be there after the crashes, exactly once.
- bank transfers: two balance updates and a transfer_log row, in one
  transaction. The total never changes, and every balance equals the opening
  balance replayed through the log: a transfer is all or nothing.
- Dolt commits (Versioned subjects): dolt_commit after a batch of inserts.
  A hash that was returned must be in the log, and the table AS OF it must
  hold every ledger id acknowledged before that dolt_commit was sent.

A connection that breaks leaves its work's outcome unknown: the writer
reconnects and moves on, and the check allows such work to be present or
absent, but never twice. Every operation's latency is recorded: a client
must get an answer or an error within its timeout, never hang.

Usage: python durability.py setup SUBJECT
       python durability.py write SUBJECT SECONDS
       python durability.py writing          (1 once the writers finished)
       python durability.py check SUBJECT    (the verdict JSON)
"""
import json
import os
import random
import sys
import threading
import time

import psycopg

from hermitage import HOSTS, log

LEDGER = "/ledger"
ACCOUNTS, OPENING = 8, 100
HANG = 30.0  # seconds: an operation slower than this hung
VERSIONED = ("doltgres.release", "doltgres.main")


def connect(conninfo):
    """Connect, waiting out a restart."""
    deadline = time.time() + 120
    while True:
        try:
            return psycopg.connect(**conninfo, autocommit=True, connect_timeout=5)
        except psycopg.Error:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def setup(subject):
    os.makedirs(LEDGER, exist_ok=True)
    for f in os.listdir(LEDGER):
        os.remove(os.path.join(LEDGER, f))
    c = connect(HOSTS[subject])
    for s in ["DROP TABLE IF EXISTS ledger", "DROP TABLE IF EXISTS accounts", "DROP TABLE IF EXISTS transfer_log",
              "CREATE TABLE ledger (id int, writer int)",  # no key: a double apply must be visible
              "CREATE TABLE accounts (id int PRIMARY KEY, balance int NOT NULL)",
              "CREATE TABLE transfer_log (id int PRIMARY KEY, src int, dst int, amount int)"]:
        c.execute(s)
    for i in range(ACCOUNTS):
        c.execute("INSERT INTO accounts VALUES (%s, %s)", (i, OPENING))
    if subject in VERSIONED:
        c.execute("SELECT dolt_commit('-Am', 'durability setup')")
    c.close()
    print("ok")


def write(subject, seconds):
    conninfo = HOSTS[subject]
    stop = time.time() + float(seconds)
    out = open(os.path.join(LEDGER, "events.jsonl"), "w", buffering=1)
    lock = threading.Lock()

    def rec(**e):
        e["t"] = time.time()
        with lock:
            out.write(json.dumps(e) + "\n")

    def timed(conn, kind, fn):
        t0 = time.time()
        try:
            fn(conn)
            outcome = "ok"
        except psycopg.Error as e:
            outcome = "fail" if e.sqlstate and not isinstance(e, psycopg.OperationalError) else "unknown"
            if e.sqlstate in ("40001", "40P01"):
                outcome = "fail"
        rec(kind="latency", op=kind, seconds=round(time.time() - t0, 3), outcome=outcome)
        return outcome

    def inserter(w):
        conn, i = connect(conninfo), 0
        while time.time() < stop:
            i += 1
            id_ = w * 1_000_000 + i
            rec(kind="insert", id=id_, state="sent")
            o = timed(conn, "insert", lambda c: c.execute("INSERT INTO ledger VALUES (%s, %s)", (id_, w)))
            rec(kind="insert", id=id_, state=o)
            if o == "unknown":
                conn.close(); conn = connect(conninfo)
            time.sleep(0.01)
        conn.close()

    def transferer(w):
        rng = random.Random(w)
        conn, i = connect(conninfo), 0
        while time.time() < stop:
            i += 1
            id_ = w * 1_000_000 + i
            src, dst = rng.sample(range(ACCOUNTS), 2)
            amt = rng.randint(1, 5)

            def tx(c):
                c.execute("BEGIN")
                try:
                    c.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amt, src))
                    c.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amt, dst))
                    c.execute("INSERT INTO transfer_log VALUES (%s, %s, %s, %s)", (id_, src, dst, amt))
                    c.execute("COMMIT")
                except psycopg.Error:
                    try:
                        c.execute("ROLLBACK")
                    except psycopg.Error:
                        pass
                    raise
            o = timed(conn, "transfer", tx)
            rec(kind="transfer", id=id_, state=o)
            if o == "unknown":
                conn.close(); conn = connect(conninfo)
            time.sleep(0.01)
        conn.close()

    def committer():
        conn = connect(conninfo)
        while time.time() < stop:
            time.sleep(1.0)
            sent = time.time()
            rec(kind="dolt_commit", state="sent", sent=sent)
            holder = {}

            def dc(c):
                holder["h"] = c.execute("SELECT dolt_commit('-Am', 'durability')").fetchone()[0]
            o = timed(conn, "dolt_commit", dc)
            h = holder.get("h")
            if isinstance(h, list):
                h = h[0]
            rec(kind="dolt_commit", state=o, hash=h, sent=sent)
            if o == "unknown":
                conn.close(); conn = connect(conninfo)
        conn.close()

    threads = [threading.Thread(target=inserter, args=(w,)) for w in range(3)]
    threads += [threading.Thread(target=transferer, args=(w,)) for w in range(10, 13)]
    if subject in VERSIONED:
        threads.append(threading.Thread(target=committer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out.close()
    open(os.path.join(LEDGER, "done"), "w").write("1")
    print("done")


def writing():
    print(1 if os.path.exists(os.path.join(LEDGER, "done")) else 0)


def check(subject):
    events = [json.loads(l) for l in open(os.path.join(LEDGER, "events.jsonl"))]
    c = connect(HOSTS[subject])
    v = {}
    if os.environ.get("CONTROL"):
        # Negative control: do what a durability bug would, then check.
        victim = next(e["id"] for e in events if e["kind"] == "insert" and e["state"] == "ok")
        c.execute("DELETE FROM ledger WHERE id = %s", (victim,))
        c.execute("UPDATE accounts SET balance = balance + 1 WHERE id = 0")

    # ledger: acknowledged ids present, exactly once; nothing anyone never sent
    acked = {e["id"] for e in events if e["kind"] == "insert" and e["state"] == "ok"}
    sent = {e["id"] for e in events if e["kind"] == "insert"}
    counts = {}
    for (i,) in c.execute("SELECT id FROM ledger").fetchall():
        counts[i] = counts.get(i, 0) + 1
    lost = sorted(acked - set(counts))
    dup = sorted(i for i, n in counts.items() if n > 1)
    phantom = sorted(set(counts) - sent)
    failed = {e["id"] for e in events if e["kind"] == "insert" and e["state"] == "fail"}
    failed_present = sorted(i for i in counts if i in failed)

    # bank: the total, and every balance replayed through the log
    bal = dict(c.execute("SELECT id, balance FROM accounts").fetchall())
    logged = c.execute("SELECT id, src, dst, amount FROM transfer_log").fetchall()
    replay = {i: OPENING for i in range(ACCOUNTS)}
    for _, s, d, a in logged:
        replay[s] -= a
        replay[d] += a
    torn = sum(1 for i in range(ACCOUNTS) if bal.get(i) != replay[i])
    t_acked = {e["id"] for e in events if e["kind"] == "transfer" and e["state"] == "ok"}
    t_lost = sorted(t_acked - {r[0] for r in logged})

    # Dolt commits: in the log, and AS OF each holds what was acknowledged before it
    dc_lost = dc_short = 0
    if subject in VERSIONED:
        log_hashes = {r[0] for r in c.execute("SELECT commit_hash FROM dolt_log").fetchall()}
        for e in events:
            if e["kind"] == "dolt_commit" and e["state"] == "ok":
                if e["hash"] not in log_hashes:
                    dc_lost += 1
                    log(f"acknowledged dolt commit {e['hash']} is not in dolt_log")
                    continue
                before = {x["id"] for x in events if x["kind"] == "insert" and x["state"] == "ok" and x["t"] < e["sent"]}
                assert str(e["hash"]).isalnum()
                have = {r[0] for r in c.execute(f"SELECT id FROM ledger AS OF '{e['hash']}'").fetchall()}
                if before - have:
                    dc_short += 1
                    log(f"dolt commit {e['hash'][:8]} lacks {len(before - have)} ids acknowledged before it")

    slow = [e for e in events if e["kind"] == "latency" and e["seconds"] > HANG]
    unknown = sum(1 for e in events if e["kind"] == "latency" and e["outcome"] == "unknown")
    oks = {k: sum(1 for e in events if e["kind"] == k and e.get("state") == "ok") for k in ("insert", "transfer", "dolt_commit")}
    log(f"acknowledged {oks}; unknown outcomes {unknown}; ledger rows {sum(counts.values())}")
    log(f"lost {len(lost)} {lost[:5]}, duplicated {len(dup)} {dup[:5]}, phantom {len(phantom)}, failed-yet-present {len(failed_present)}")
    log(f"bank total {sum(bal.values())} of {ACCOUNTS * OPENING}; torn balances {torn}; lost transfers {len(t_lost)}")
    log(f"dolt commits lost {dc_lost}, short {dc_short}; operations over {HANG:.0f} s: {len(slow)}")
    if oks["insert"] < 100 or oks["transfer"] < 100 or unknown == 0:
        raise SystemExit("the writers did too little, or no crash interrupted them: the run tests nothing")

    v["durability.commit_survives_crash"] = len(lost) + len(t_lost) + dc_lost
    v["durability.no_partial_after_crash"] = torn + dc_short
    v["client.no_double_apply"] = len(dup) + len(phantom)
    v["txn.bank_conserved"] = int(sum(bal.values()) != ACCOUNTS * OPENING)
    v["txn.atomic"] = torn + len(failed_present)  # a statement that failed left an effect
    v["client.clear_failure"] = len(slow)
    # the operations, decided by the checks that exercise them
    v["txn.commit"] = v["durability.commit_survives_crash"] + torn
    v["query.insert"] = len(lost) + len(dup)
    v["query.update"] = torn
    v["server.restart"] = v["durability.commit_survives_crash"]
    v["client.connect"] = len(slow)
    print(json.dumps(v, sort_keys=True))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "setup":
        setup(sys.argv[2])
    elif cmd == "write":
        write(sys.argv[2], sys.argv[3])
    elif cmd == "writing":
        writing()
    elif cmd == "check":
        check(sys.argv[2])
