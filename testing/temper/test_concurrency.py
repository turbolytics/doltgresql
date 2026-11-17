"""The txn.contract lattice (temper/lattices/txn.contract.yml).

Each test states what it proves with @pytest.mark.covers (temper-python).
Without `temper run`, the marks do nothing.

Every check compares the final state with what the clients were told: a write
that reported success must be in the final state. A server may refuse a write
with an error, such as 40001, but it must not report success and drop it.
PostgreSQL is the reference.
"""

import os
import random
import threading
import time

import psycopg
import pytest

SERVERS = {
    "postgres": os.environ.get("TEMPER_POSTGRES_DSN", "host=localhost port=25432 user=postgres password=temper dbname=postgres"),
    "doltgres": os.environ.get("TEMPER_DOLTGRES_DSN", "host=localhost port=25433 user=postgres password=temper dbname=postgres"),
}
CLIENTS, OPS = 8, 40


def servers(member, only=None):
    return [
        pytest.param(name, id=name, marks=[pytest.mark.covers(f"txn.contract/{member}", subject=f"server.{name}"),
                                           pytest.mark.covers(f"server.{name}")])
        for name in SERVERS
        if only is None or name in only
    ]


def admin(name, *stmts):
    with psycopg.connect(SERVERS[name], autocommit=True) as c:
        for s in stmts:
            c.execute(s)


def one(name, sql):
    with psycopg.connect(SERVERS[name], autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


def run_clients(name, op, autocommit):
    """Run op(conn, client, i, rng) from CLIENTS connections; count successes."""
    conns = [psycopg.connect(SERVERS[name], autocommit=autocommit) for _ in range(CLIENTS)]
    ok = [0] * CLIENTS

    def client(k):
        rng = random.Random(k)
        for i in range(OPS):
            try:
                op(conns[k], k, i, rng)
                if not autocommit:
                    conns[k].commit()
                ok[k] += 1
            except psycopg.Error:
                if not autocommit:
                    conns[k].rollback()

    threads = [threading.Thread(target=client, args=(k,)) for k in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c in conns:
        c.close()
    return sum(ok)


def counter(name):
    admin(name, "drop table if exists t_counter", "create table t_counter (id int primary key, n int not null)",
          "insert into t_counter values (1, 0)")


@pytest.mark.parametrize("name", servers("two_committed_increments_both_count"))
def test_two_committed_increments_both_count(name):
    counter(name)
    a, b = psycopg.connect(SERVERS[name]), psycopg.connect(SERVERS[name])
    a.execute("update t_counter set n = n + 1 where id = 1")  # transaction A, not committed

    def session_b():
        b.execute("update t_counter set n = n + 1 where id = 1")  # transaction B
        b.commit()

    t = threading.Thread(target=session_b)
    t.start()
    time.sleep(0.5)
    a.commit()
    t.join()
    a.close()
    b.close()
    assert one(name, "select n from t_counter") == 2


@pytest.mark.parametrize("name", servers("concurrent_updates_lose_nothing_autocommit"))
def test_concurrent_updates_autocommit(name):
    counter(name)
    ok = run_clients(name, lambda c, k, i, r: c.execute("update t_counter set n = n + 1 where id = 1"), autocommit=True)
    assert one(name, "select n from t_counter") == ok


@pytest.mark.parametrize("name", servers("concurrent_updates_lose_nothing_in_transactions"))
def test_concurrent_updates_in_transactions(name):
    counter(name)
    ok = run_clients(name, lambda c, k, i, r: c.execute("update t_counter set n = n + 1 where id = 1"), autocommit=False)
    assert one(name, "select n from t_counter") == ok


@pytest.mark.parametrize("name", servers("concurrent_upserts_lose_nothing"))
def test_concurrent_upserts(name):
    admin(name, "drop table if exists t_upsert", "create table t_upsert (k int primary key, n int not null)")
    ok = run_clients(name, lambda c, k, i, r: c.execute(
        "insert into t_upsert values (%s, 1) on conflict (k) do update set n = t_upsert.n + 1", [i % 5]), autocommit=True)
    assert one(name, "select coalesce(sum(n), 0) from t_upsert") == ok


@pytest.mark.parametrize("name", servers("concurrent_transfers_keep_the_total"))
def test_concurrent_transfers(name):
    admin(name, "drop table if exists t_bank", "create table t_bank (id int primary key, bal int not null)",
          "insert into t_bank select g, 100 from generate_series(1, 8) g")

    def transfer(c, k, i, r):
        a, b = r.sample(range(1, 9), 2)
        amount = r.randint(1, 10)
        c.execute("update t_bank set bal = bal - %s where id = %s", [amount, a])
        c.execute("update t_bank set bal = bal + %s where id = %s", [amount, b])

    run_clients(name, transfer, autocommit=False)
    assert one(name, "select sum(bal) from t_bank") == 800


@pytest.mark.parametrize("name", servers("unique_key_holds_under_concurrent_inserts"))
def test_unique_key_under_concurrent_inserts(name):
    admin(name, "drop table if exists t_unique", "create table t_unique (k int primary key, who int)")
    run_clients(name, lambda c, k, i, r: c.execute("insert into t_unique values (%s, %s) on conflict do nothing", [i, k]),
                autocommit=True)
    assert one(name, "select count(*) from t_unique") == OPS
    assert one(name, "select count(distinct k) from t_unique") == OPS


@pytest.mark.parametrize("name", servers("select_for_update_serializes", only=["postgres"]))
def test_select_for_update_serializes(name):
    counter(name)

    def read_modify_write(c, k, i, r):
        n = c.execute("select n from t_counter where id = 1 for update").fetchone()[0]
        c.execute("update t_counter set n = %s where id = 1", [n + 1])

    ok = run_clients(name, read_modify_write, autocommit=False)
    assert ok > 0
    assert one(name, "select n from t_counter") == ok
