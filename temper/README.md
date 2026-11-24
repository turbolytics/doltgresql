# Temper map for Doltgres

This map audits Doltgres's full surface, starting from one question: what do
its users need it for? Each job is a functional group, and each operation in
a group is a feature. The inventory was approved before any test was written.
Every claim is declared, tested or not, so `MATRIX.md` shows what is proven
and what is still missing.

## The inventory

**Sources:** Doltgres's docs (doltgres.com/docs, from dolthub/docs-2
@73d0e0b53f1e), its README, and the 100 most recent issues labeled
"customer issue".

**15 functional groups, 176 operations** (`temper/features.yml`):

| Use it as Postgres | Version my data | Keep it running |
|---|---|---|
| `server.` run the server (9) | `vc.commit` record versions (13) | `replication.` replicas and failover (8) |
| `client.` connect from apps (9) | `vc.branch` work on branches (9) | `backup.` back up and recover (7) |
| `schema.` define and change schema (30) | `vc.merge` combine work (14) | |
| `query.` read and write data (20) | `vc.history` see what changed (19) | |
| `txn.` concurrent transactions (8) | `vc.remote` share and sync (8) | |
| `access.` control access (12) | | |
| `catalog.` let tools inspect (5) | | |
| `import.` move Postgres in (5) | | |

**6 lattices** (`temper/lattices/`). Four come from Doltgres's own support
pages, one member per row. Each Doltgres subject answers each member the way
the docs mark it. An unsupported answer must be proven: the statement fails
with a clear error and changes nothing.

| Lattice | Members | Doltgres's docs say |
|---|---|---|
| `sql.command` | 184 | 61 supported, 4 partial, 119 unsupported |
| `sql.type` | 133 | 69 supported, 64 unsupported |
| `sql.function` | 962 | 278 supported, 4 partial, 680 unsupported |
| `catalog.table` | 140 | 31 supported, 34 partial, 75 unsupported |
| `client.driver` | 27 | every documented client, plus psql |
| `txn.isolation` | 3 | read committed only (concepts/sql/transaction) |

`temper/recipe/lattices_from_docs.py` rebuilds the four page lattices and the
subjects' answers from the docs.

**47 invariants** (`temper/invariants.yml`) in eight families: answers,
schema, transactions (Jepsen's anomalies G0 to G2-item, per isolation level),
durability, versioning, sharing, access, and compatibility. Each names the
user issues that show it matters.

**3 subjects:** Doltgres 1.5.3 (the latest release), Doltgres main, and
PostgreSQL 17 as the reference. Claims about versioning, replication and
backups apply only to Doltgres. Every other claim applies to all three, so
each Doltgres cell has a PostgreSQL cell beside it.

**7 levels:** conformance, concurrency, versioning, faults, compatibility,
security, operations. Faults are an overlay on the operations chosen for
fault analysis, the ones whose `requires` lists `faults`: transactions and
writes, Dolt commits and merges, branch operations, remotes, replication,
backups, restarts, client connections, and pg_dump restores.

## Status

**Transactions: Hermitage** (`temper/scenarios/txn.yml`, oracle
`temper/recipe/oracle/hermitage.py`). Martin Kleppmann's Hermitage scripts
each anomaly, G0 to G2, as statements from two or three sessions
(github.com/ept/hermitage @f029bec). The oracle runs all 15 scripts at all
three isolation levels and decides each anomaly from what the sessions read,
which commits succeeded and the final state.

- PostgreSQL 17 reproduces Hermitage's table exactly, which validates the
  oracle: read committed allows predicate reads, lost updates, read skew and
  write skew; repeatable read allows only write skew; serializable prevents
  all of them.
- Doltgres 1.5.3 prevents every anomaly read committed must prevent. It
  behaves as snapshot isolation at every level: it also prevents lost
  updates and read skew, and allows write skew (G2-item and G2).
- Doltgres accepts `BEGIN ISOLATION LEVEL SERIALIZABLE` and `REPEATABLE
  READ`, reports `read committed`, and runs snapshot isolation. A client that
  asks for serializable gets write skew. This is by design
  (dolthub/doltgresql#3063: "accept transaction isolation levels, but always
  use REPEATABLE READ"), but the docs say Doltgres uses the Read Committed
  model, and the declaration follows the docs: the two levels are answered
  unsupported, so they must be refused. The 4 failing cells are that gap.

**Transactions: Elle** (`txn.elle`, `txn.elle_strict`, oracle
`temper/recipe/oracle/elle_append.py`). Eight clients run short append and
read transactions on six hot keys for 20 s per level, recording each outcome.
Elle (jepsen-io/elle, via ligurio/elle-cli 0.1.11) checks each history
against the level's model: read committed, snapshot isolation for
repeatable read, serializable. A negative control shows the workload
produces write skew Elle can see: PostgreSQL's repeatable-read history,
checked as serializable, finds G2-item.

- PostgreSQL 17 is valid at every level.
- Doltgres 1.5.3 is valid at read committed (11,884 committed transactions)
  and at repeatable read as snapshot isolation. At the serializable it
  accepts, Elle finds G2-item: the same write skew Hermitage shows.
- `txn.elle_strict` runs on PostgreSQL only. Doltgres is exempt from the
  read-skew and write-skew claims by its docs, and Hermitage proves (or, as
  now, fails to prove) its refusals.

**Versioning: a model of Dolt's docs** (`vc.model`, oracle
`temper/recipe/oracle/vc_model.py`). A seeded sequence of the operations
users run (writes, dolt_commit, dolt_branch, dolt_checkout, dolt_merge with
conflicts resolved --ours or --theirs, dolt_reset --hard, dolt_revert,
dolt_cherry_pick) runs against Doltgres and against a model of the documented
semantics: snapshots keyed by primary key, a three-way cell-wise merge, and a
fast-forward when the target is an ancestor. The working table must equal the
model after every operation; every commit read AS OF its hash must equal its
snapshot; dolt_diff between sampled commits must list exactly the rows that
differ. A negative control (the model merging row-wise, which the docs rule
out) reports violations, so the oracle can tell the two apart.

- Doltgres 1.5.3 and main both match the model: 4 seeds of 200 steps each,
  about 240 commits and 46 merges per subject (24 of them conflicted), with
  every commit's AS OF and every sampled diff exact. The 5 versioning
  invariants and 11 operations are covered on both.

**Durability: kill -9** (`durability.crash`, oracle
`temper/recipe/oracle/durability.py`). Writers commit ledger inserts, bank
transfers and (on Doltgres) a Dolt commit every second, recording every
outcome, while the database is killed with SIGKILL every 6-12 s for 50 s and
restarted. Afterwards every acknowledged insert is present exactly once, no
failed statement left an effect, the bank total holds, every balance equals
the opening balance replayed through the transfer log, every acknowledged Dolt
commit is in the log and holds every insert acknowledged before it, and no
operation hung. A negative control (one acknowledged row deleted, one balance
shifted) is reported as a loss, a torn balance and a broken total.

- All three subjects held through 5 kills each: about 15,000 inserts, 11,000
  transfers and 56 Dolt commits per Doltgres subject, with 35 outcomes left
  unknown by the crashes; nothing lost, duplicated, torn or hung.

**Version control across kill -9** (`vc.crash`, oracle
`temper/recipe/oracle/vc_crash.py`). One client runs branch create and
delete, checkout, writes, dolt_commit, merges with conflicts resolved,
cherry-picks and reverts while the server is killed every 4-8 s. An
interrupted operation must leave the old head or a correctly parented new
commit, with no merge left in progress; afterwards every returned commit
reads AS OF its hash as it did when returned, every branch head reads, every
branch has its permission row, and no failed creation left one.

- Doltgres 1.5.3 and main pass: about 1,270 commits, 650 merges, 365 branch
  creations and 7-8 interrupted operations each; nothing lost, changed or
  half-done.
- Two observations, not crash effects (a run with no crashes shows the
  same): `dolt.commits` omits commits no branch reaches (about 150 per run),
  though its docs say it "shows ALL commits"; and a deleted branch keeps its
  `dolt_branch_control` row. The second belongs to the security family.

**Doltgres main** is built from this checkout by
`temper/recipe/build_main.sh` (its binary reports 1.5.2, so the map
identifies it by commit). It matches 1.5.3 on every check so far, including
the silent serializable.

Everything else is declared and not yet tested.

## Regenerating

With Temper v0.1.9 or later (the matrix splits into an index and group pages):

    sh temper/recipe/build_main.sh        # the doltgres.main image, once
    temper check
    temper clean
    temper run --level concurrency -- temper scenarios run --scenario txn.hermitage --build
    temper run --level concurrency -- temper scenarios run --scenario txn.elle
    temper run --level concurrency -- temper scenarios run --scenario txn.elle_strict
    temper run --level versioning -- temper scenarios run --scenario vc.model
    temper run --level faults -- temper scenarios run --scenario durability.crash
    temper run --level faults -- temper scenarios run --scenario vc.crash
    temper matrix --all-levels
