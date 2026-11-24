"""Elle's list-append test, run against one subject at every isolation level.

Clients run short transactions of appends and reads on a few hot keys, each
recording its outcome: ok (COMMIT returned), fail (the transaction aborted
before or at COMMIT, so none of it happened), or info (the connection broke,
so its outcome is unknown). Elle (jepsen-io/elle, via ligurio/elle-cli
0.1.11) builds the dependency graph from the history and searches it for the
anomalies the level must prevent:

    read committed   -> read-committed       (G0, G1a, G1b, G1c, dirty update)
    repeatable read  -> snapshot-isolation   (PostgreSQL's repeatable read)
    serializable     -> serializable

Each anomaly type maps to the invariant it violates. A level the subject
declares unsupported must be refused; if it is accepted, its history is still
checked and logged.

Usage: python elle_append.py SUBJECT SECONDS common|strict
  common: the claims every subject holds (G0, G1, lost update, read committed)
  strict: read skew, write skew, repeatable read and serializable
"""
import json
import os
import random
import subprocess
import sys
import threading
import time

import psycopg

from hermitage import HOSTS, LEVELS, declared, log, refused

ELLE = ["java", "-jar", "/elle/elle-cli.jar", "--model", "list-append", "--format", "json", "--output", "json",
        "--cycle-search-timeout", "10000"]  # ms per cluster; 1 s left dense read-committed histories unknown
MODEL = {"read committed": "read-committed", "repeatable read": "snapshot-isolation", "serializable": "serializable"}
# Elle's anomaly types, by the invariant each violates.
INVARIANT = {
    "G0": "txn.no_dirty_write", "G0-process": "txn.no_dirty_write", "G0-realtime": "txn.no_dirty_write",
    "dirty-update": "txn.no_dirty_write",
    "G1a": "txn.no_dirty_read", "G1b": "txn.no_dirty_read",
    "G1c": "txn.no_circular_info", "G1c-process": "txn.no_circular_info",
    "lost-update": "txn.no_lost_update",
    "G-single": "txn.no_read_skew", "G-single-process": "txn.no_read_skew", "G-single-realtime": "txn.no_read_skew",
    "G-nonadjacent": "txn.no_read_skew", "GSIa": "txn.no_read_skew", "GSIb": "txn.no_read_skew",
    "G2-item": "txn.no_write_skew", "G2-item-process": "txn.no_write_skew", "G2-item-realtime": "txn.no_write_skew",
    "G2": "txn.no_write_skew", "G2-process": "txn.no_write_skew", "write-skew": "txn.no_write_skew",
}
# What each level must prevent, from PostgreSQL's guarantees. Elle may report
# anomalies a level allows (write skew at read committed); those are logged,
# not counted.
_RC = {"G0", "dirty-update", "G1a", "G1b", "G1c"}
_SI = _RC | {"lost-update", "G-single", "GSIa", "GSIb"}
FORBIDDEN = {"read committed": _RC, "repeatable read": _SI,
             "serializable": _SI | {"G-nonadjacent", "G2-item", "G2", "write-skew"}}
CLIENTS, KEYS = 8, 6


def workload(conninfo, level, seconds, seed):
    with psycopg.connect(**conninfo, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS lists")
        c.execute("CREATE TABLE lists (k int PRIMARY KEY, v text NOT NULL)")
    history, lock = [], threading.Lock()
    counter = {k: 0 for k in range(KEYS)}
    stop = time.time() + seconds
    procs = iter(range(10**6))

    def record(op):
        with lock:
            op["index"] = len(history)
            history.append(op)

    def client(n):
        rng = random.Random(f"{seed}-{n}")
        proc = next(procs)
        conn = psycopg.connect(**conninfo, autocommit=True)
        while time.time() < stop:
            txn = []
            for _ in range(rng.randint(1, 4)):
                k = rng.randrange(KEYS)
                if rng.random() < 0.5:
                    with lock:
                        counter[k] += 1
                        txn.append(["append", k, counter[k]])
                else:
                    txn.append(["r", k, None])
            record({"type": "invoke", "process": proc, "value": [list(m) for m in txn]})
            done, outcome = [], "ok"
            try:
                conn.execute(f"BEGIN ISOLATION LEVEL {level.upper()}")
                for f, k, v in txn:
                    if f == "append":
                        conn.execute("INSERT INTO lists (k, v) VALUES (%s, %s) "
                                     "ON CONFLICT (k) DO UPDATE SET v = lists.v || ',' || excluded.v", (k, str(v)))
                        done.append([f, k, v])
                    else:
                        row = conn.execute("SELECT v FROM lists WHERE k = %s", (k,)).fetchone()
                        done.append([f, k, [int(x) for x in row[0].split(",")] if row else []])
                conn.execute("COMMIT")
            except psycopg.Error as e:
                if e.sqlstate:
                    # The server answered with an error, 40001 included: the
                    # transaction aborted, so none of it happened.
                    outcome = "fail"
                    try:
                        conn.execute("ROLLBACK")
                    except psycopg.Error:
                        pass
                else:
                    outcome = "info"  # no answer: the outcome is unknown
            record({"type": outcome, "process": proc,
                    "value": done if outcome == "ok" else [list(m) for m in txn]})
            if outcome == "info":
                try:
                    conn.close()
                except Exception:
                    pass
                proc = next(procs)  # Jepsen's rule: a process with an unknown outcome is retired
                conn = psycopg.connect(**conninfo, autocommit=True)
        conn.close()

    threads = [threading.Thread(target=client, args=(n,)) for n in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return history


def check(history, level):
    path = f"/tmp/history-{level.replace(' ', '-')}.json"
    json.dump(history, open(path, "w"))
    out = subprocess.run(ELLE + ["--consistency-models", MODEL[level], path],
                         capture_output=True, text=True, timeout=600)
    text = out.stdout.strip()
    start = text.find("{")
    if start < 0:
        raise SystemExit(f"elle-cli printed no result for {level}: {out.stdout[-400:]} {out.stderr[-400:]}")
    res = json.loads(text[start:text.rfind("}") + 1])
    return res


COMMON = ["txn.no_dirty_write", "txn.no_dirty_read", "txn.no_circular_info", "txn.no_lost_update",
          "txn.isolation/read committed"]
STRICT = ["txn.no_read_skew", "txn.no_write_skew", "txn.isolation/repeatable read", "txn.isolation/serializable"]


def main(subject, seconds, scope):
    conninfo = HOSTS[subject]
    supported, _ = declared(subject)
    keys = COMMON if scope == "common" else STRICT
    levels = LEVELS if scope == "common" else ["repeatable read", "serializable"]
    verdict, anomalies = {}, {}
    for level in levels:
        if level not in supported:
            if refused(conninfo, level):
                log(f"{level}: declared unsupported, refused")
                continue
            log(f"{level}: declared unsupported, NOT refused; checking what it does anyway")
        h = workload(conninfo, level, seconds, seed=7)
        counts = {t: sum(1 for o in h if o["type"] == t) for t in ("ok", "fail", "info")}
        res = check(h, level)
        found = sorted(res.get("anomaly-types") or [])
        log(f"{level:16} {counts}  model {MODEL[level]}: valid {res.get('valid?')}  anomalies {found}")
        if counts["ok"] < 50:
            raise SystemExit(f"{level}: only {counts['ok']} transactions committed; the history tests too little")
        if res.get("valid?") == "unknown":
            raise SystemExit(f"{level}: Elle could not finish the analysis")
        bad = [a for a in found if a in FORBIDDEN[level]]
        for a in bad:
            inv = INVARIANT.get(a)
            if inv:
                anomalies[inv] = anomalies.get(inv, 0) + 1
        if f"txn.isolation/{level}" in keys and level in supported:
            verdict[f"txn.isolation/{level}"] = len(bad)
    for k in keys:
        if k.startswith("txn.isolation/"):
            if k not in verdict:
                raise SystemExit(f"{k}: the subject does not support it; this scope cannot decide it")
        else:
            verdict[k] = anomalies.get(k, 0)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), sys.argv[3])
