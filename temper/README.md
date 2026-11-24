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

Declared, not yet tested: 4,734 cells, all missing. Cells fill as each
family's scenarios land, highest risk first: transactions, versioning and
durability.

## Regenerating

    temper check
    temper matrix --all-levels
