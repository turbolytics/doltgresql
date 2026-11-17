"""The query.contract lattice (temper/lattices/query.contract.yml).

Each test states what it proves with @pytest.mark.covers (temper-python).
Without `temper run`, the marks do nothing.

PostgreSQL is the reference. The tests use their own database, temper_queries,
which sorts text in C order on both servers: Doltgres always does, and
PostgreSQL does when the database is created with lc_collate 'C'.
"""

import datetime as dt
import os
import random
from collections import Counter

import psycopg
import pytest

ADMIN = {
    "postgres": os.environ.get("TEMPER_POSTGRES_DSN", "host=localhost port=25432 user=postgres password=temper dbname=postgres"),
    "doltgres": os.environ.get("TEMPER_DOLTGRES_DSN", "host=localhost port=25433 user=postgres password=temper dbname=postgres"),
}
DB = "temper_queries"


def dsn(name):
    return psycopg.conninfo.make_conninfo(ADMIN[name], dbname=DB)


def servers(member):
    return [
        pytest.param(name, id=name, marks=[pytest.mark.covers(f"query.contract/{member}", subject=f"server.{name}"),
                                           pytest.mark.covers(f"server.{name}")])
        for name in ADMIN
    ]


@pytest.fixture(scope="module", autouse=True)
def database():
    for name, admin in ADMIN.items():
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f"drop database if exists {DB}")
            if name == "postgres":
                c.execute(f"create database {DB} template template0 lc_collate 'C' lc_ctype 'en_US.utf8'")
            else:
                c.execute(f"create database {DB}")


def rows_of(conn, sql, ordered):
    rows = [tuple(r) for r in conn.execute(sql).fetchall()]
    return rows if ordered else Counter(rows)


# --- index_never_changes_results ---------------------------------------------

INDEXES = {
    "none": None,
    "a": "create index {t}_ia on {t} (a)",
    "ab": "create index {t}_iab on {t} (a, b)",
    "a_desc": "create index {t}_iad on {t} (a desc, b)",
    "partial": "create index {t}_ip on {t} (a) where b > 0",
    "expr": "create index {t}_ie on {t} (lower(c))",
    "c": "create index {t}_ic on {t} (c)",
    "ca": "create index {t}_ica on {t} (c, a)",
    "e": "create index {t}_iee on {t} (e)",
    "f": "create index {t}_if on {t} (f, a)",
    "unique": "create unique index {t}_iu on {t} (id, a)",
}
WORDS = ["Apple", "apple", "APPLE", "banana", "Banana", "cherry", "", "date", "Éclair", "éclair", "fig", "grape", "a%b", "a_b"]


def dataset(rng):
    t = []
    for i in range(1, 2001):
        t.append((
            i,
            None if rng.random() < 0.15 else rng.randint(-5, 25),
            None if rng.random() < 0.15 else rng.randint(-3, 6),
            None if rng.random() < 0.12 else rng.choice(WORDS),
            None if rng.random() < 0.1 else dt.datetime(2024, 1, 1) + dt.timedelta(hours=rng.randint(-200, 2000)),
            None if rng.random() < 0.1 else rng.random() < 0.5,
        ))
    u = [(i, None if rng.random() < 0.15 else rng.randint(-5, 25), rng.choice(WORDS + [None])) for i in range(1, 301)]
    return t, u


def queries(rng, n):
    v = lambda: rng.randint(-6, 26)  # noqa: E731
    b = lambda: rng.randint(-3, 6)  # noqa: E731
    w = lambda: rng.choice(WORDS)  # noqa: E731
    ts = lambda: f"'{dt.datetime(2024, 1, 1) + dt.timedelta(hours=rng.randint(-200, 2000))}'"  # noqa: E731
    shapes = [
        lambda: (f"select id from {{T}} where a = {v()}", False),
        lambda: ("select id from {T} where a is null", False),
        lambda: (f"select id from {{T}} where a is not null and b = {b()}", False),
        lambda: (f"select id from {{T}} where a in ({v()}, {v()}, null)", False),
        lambda: (f"select id from {{T}} where a not in ({v()}, {v()})", False),
        lambda: (f"select id from {{T}} where a < {v()}", False),
        lambda: (f"select id from {{T}} where a > {v()} and a <= {v()}", False),
        lambda: (f"select id from {{T}} where a between {v()} and {v()}", False),
        lambda: (f"select id from {{T}} where a >= {v()} and b > 0", False),
        lambda: (f"select id from {{T}} where a = {v()} and b = {b()}", False),
        lambda: (f"select id from {{T}} where a = {v()} or a is null", False),
        lambda: (f"select id from {{T}} where (a, b) > ({v()}, {b()})", False),
        lambda: (f"select id from {{T}} where c = '{w()}'", False),
        lambda: (f"select id from {{T}} where c like '{w()[:2]}%'", False),
        lambda: (f"select id from {{T}} where c > '{w()}'", False),
        lambda: (f"select id from {{T}} where lower(c) = '{w().lower()}'", False),
        lambda: (f"select id from {{T}} where c = '{w()}' and a > {v()}", False),
        lambda: (f"select id from {{T}} where e > {ts()}", False),
        lambda: (f"select id from {{T}} where e between {ts()} and {ts()}", False),
        lambda: (f"select id from {{T}} where f and a > {v()}", False),
        lambda: ("select id from {T} where f is not true", False),
        lambda: (f"select id, a from {{T}} order by a, id limit {rng.randint(1, 30)}", True),
        lambda: (f"select id, a from {{T}} order by a desc, id limit {rng.randint(1, 30)}", True),
        lambda: (f"select id, a from {{T}} where a > {v()} order by a nulls first, id limit 20", True),
        lambda: ("select id from {T} where b > 0 order by a, id limit 15", True),
        lambda: (f"select id, c from {{T}} order by c, id limit {rng.randint(1, 30)}", True),
        lambda: (f"select min(a), max(a), count(a), count(*) from {{T}} where b > {b()}", False),
        lambda: ("select min(a), max(a) from {T}", False),
        lambda: (f"select max(a) from {{T}} where a < {v()}", False),
        lambda: ("select a, count(*) from {T} group by a", False),
        lambda: ("select distinct a from {T}", False),
        lambda: ("select count(distinct (a, b)) from {T}", False),
        lambda: ("select t.id, u.id from {T} t join u on t.a = u.a", False),
        lambda: ("select t.id, u.id from {T} t left join u on t.a = u.a and u.c is not null", False),
        lambda: ("select t.id from {T} t where t.a not in (select a from u where a is not null)", False),
        lambda: ("select t.id from {T} t where t.a in (select a from u)", False),
        lambda: ("select u.id, count(t.id) from u left join {T} t on t.a = u.a group by u.id", False),
        lambda: (f"select t.id from {{T}} t join u on lower(t.c) = lower(u.c) where t.a > {v()}", False),
    ]
    out = {}
    while len(out) < n:
        sql, ordered = rng.choice(shapes)()
        out[sql] = ordered
    return out


def load(conn, t, u):
    conn.execute("drop table if exists u")
    conn.execute("create table u (id int primary key, a int, c text)")
    conn.execute("insert into u values " + ",".join(["(%s,%s,%s)"] * len(u)), [x for r in u for x in r])
    conn.execute("create index u_ia on u (a)")
    for name, ddl in INDEXES.items():
        tbl = f"t_{name}"
        conn.execute(f"drop table if exists {tbl}")
        conn.execute(f"create table {tbl} (id int primary key, a int, b int, c text, e timestamp, f boolean)")
        for start in range(0, len(t), 500):
            chunk = t[start:start + 500]
            conn.execute(f"insert into {tbl} values " + ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(chunk)),
                         [x for r in chunk for x in r])
        if ddl:
            conn.execute(ddl.format(t=tbl))


@pytest.fixture(scope="module")
def sweep():
    rng = random.Random(1)
    t, u = dataset(rng)
    qs = queries(rng, 150)
    conns = {name: psycopg.connect(dsn(name), autocommit=True) for name in ADMIN}
    for c in conns.values():
        load(c, t, u)
    want = {sql: rows_of(conns["postgres"], sql.format(T="t_none"), ordered) for sql, ordered in qs.items()}
    yield qs, want, conns
    for c in conns.values():
        c.close()


@pytest.mark.parametrize("name", servers("index_never_changes_results"))
def test_index_never_changes_results(name, sweep):
    qs, want, conns = sweep
    wrong = []
    for sql, ordered in qs.items():
        for index in INDEXES:
            try:
                got = rows_of(conns[name], sql.format(T=f"t_{index}"), ordered)
            except psycopg.Error as e:
                got = f"error: {str(e).splitlines()[0][:80]}"
            if got != want[sql]:
                wrong.append(f"[{index}] {sql}")
    assert not wrong, f"{len(wrong)} of {len(qs) * len(INDEXES)} results differ from PostgreSQL, e.g. {wrong[:5]}"


# --- EXISTS and NOT EXISTS with NULL join keys -------------------------------


@pytest.mark.parametrize("name", servers("not_exists_over_nullable_foreign_key"))
def test_not_exists_over_nullable_foreign_key(name):
    with psycopg.connect(dsn(name), autocommit=True) as c:
        for s in [
            "drop table if exists tasks", "drop table if exists users",
            "create table users (id int primary key, name text not null)",
            # No CREATE INDEX: Doltgres indexes every foreign key column on its own.
            "create table tasks (id int primary key, title text not null, assignee_id int references users (id))",
            "insert into users values (1, 'ana'), (2, 'bo'), (3, 'cy')",
            "insert into tasks values (10, 'write spec', 1), (11, 'review', 2), (12, 'triage', null), (13, 'deploy', 1)",
        ]:
            c.execute(s)
        # users with no tasks
        got = c.execute("select u.id from users u where not exists "
                        "(select 1 from tasks t where t.assignee_id = u.id) order by 1").fetchall()
        assert got == [(3,)]


@pytest.mark.parametrize("name", servers("exists_over_nullable_indexed_columns"))
def test_exists_over_nullable_indexed_columns(name):
    with psycopg.connect(dsn(name), autocommit=True) as c:
        for s in [
            "drop table if exists orders", "drop table if exists stores",
            "create table stores (id int primary key, region_id int)",
            "create table orders (id int primary key, region_id int)",
            "create index stores_region_id on stores (region_id)",
            "create index orders_region_id on orders (region_id)",
            "insert into stores select g, g % 30 from generate_series(1, 10) g",
            "insert into orders select g, case when g = 1 then null else g % 20 end from generate_series(1, 10) g",
        ]:
            c.execute(s)
        exists = "select o.id from orders o where exists (select 1 from stores s where s.region_id = o.region_id)"
        assert sorted(r[0] for r in c.execute(exists)) == list(range(2, 11))
        assert [r[0] for r in c.execute(exists.replace("where exists", "where not exists"))] == [1]
