"""The lost update across columns, alone: Hermitage's cross_column script,
run N times at read committed. Prints {"txn.no_lost_update": anomalies}.

T1 closes an account WHERE balance = 0 while T2 deposits WHERE status =
'open'. PostgreSQL's read committed makes T2 wait and re-check its row, so
the deposit matches nothing; no serial order ends closed with the deposit.

Usage: python cross_column.py SUBJECT [N]
"""
import json
import sys

import psycopg

import hermitage as h


def main(subject, n=10):
    conninfo = h.HOSTS[subject]
    bad = 0
    for i in range(n):
        with psycopg.connect(**conninfo, autocommit=True) as c:
            for st in h.SETUP:
                c.execute(st)
        seen = h.cross_column(h.Run(conninfo, "read committed"))
        bad += seen
        h.log(f"run {i + 1}: {'ANOMALY: closed with the deposit' if seen else 'prevented'}")
    h.log(f"{subject}: anomaly in {bad}/{n}")
    print(json.dumps({"txn.no_lost_update": bad}))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 10)
