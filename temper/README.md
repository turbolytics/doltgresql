# Doltgres map

[Temper](https://github.com/turbolytics/temper) map of what PostgreSQL
guarantees to concurrent transactions and to ordinary queries, checked against
PostgreSQL 17 and Doltgres 1.5.1. `MATRIX.md` at the repository root is the result.

- `lattices/txn.contract.yml`: the guarantees. A write that reports success is
  never lost. A server may refuse a write with an error, such as `40001`.
- `lattices/query.contract.yml`: ordinary queries return PostgreSQL's rows, and
  an index never changes a result.
- `subjects/`: one file per server. Doltgres's row locking is a known gap
  (#3625).
- `../testing/temper/`: the tests. Each one states what it
  proves with `@pytest.mark.covers`.

Run it:

```sh
docker run -d --name temper-postgres -e POSTGRES_PASSWORD=temper -p 25432:5432 postgres:17
docker run -d --name temper-doltgres -e DOLTGRES_PASSWORD=temper -p 25433:5432 dolthub/doltgresql:1.5.1
python3 -m venv .venv-temper
.venv-temper/bin/pip install "psycopg[binary]" pytest temper-python
brew install turbolytics/tap/temper   # or: go install github.com/turbolytics/temper/cmd/temper@v0.1.3
temper clean
temper run --level ci -- .venv-temper/bin/pytest -q testing/temper
temper matrix --all-levels
```

`temper matrix` exits 1 while any cell is failing.
