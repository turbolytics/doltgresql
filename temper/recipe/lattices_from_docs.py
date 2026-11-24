"""Build the support lattices and Doltgres's answers from Doltgres's own docs.

Each lattice is one support table on doltgres.com (source: dolthub/docs-2,
site/doltgres/src/content/reference/sql-support). Its members are every row
of the table; each Doltgres subject answers a member the way the docs mark it:
supported (and partial, with a note) or unsupported. An unsupported answer is
proven by the sql.support scenario: the statement fails with a clear error and
changes nothing. PostgreSQL 17 answers every member supported.

Usage: python3 lattices_from_docs.py DOCS_DIR DOCS_COMMIT TEMPER_DIR
DOCS_DIR holds the four pages, named as gh downloads them below.
"""
import os
import re
import sys

PAGES = {
    "sql.command": ("reference_sql-support_supported-commands.md", "supported-commands", "SQL command"),
    "sql.type": ("reference_sql-support_supported-types.md", "supported-types", "type"),
    "sql.function": ("reference_sql-support_supported-functions.md", "supported-functions", "function or operator"),
    "catalog.table": ("reference_sql-support_system-catalog-schema.md", "system-catalog-schema", "pg_catalog table or view"),
}
MARKS = {"✅": "supported", "🟠": "partial", "❌": "unsupported"}
DOLTGRES = ["doltgres.release", "doltgres.main"]


def cells(line):
    """Split a markdown table row on unescaped pipes."""
    parts = re.split(r"(?<!\\)\|", line.strip())[1:-1]
    return [p.replace("\\|", "|").strip() for p in parts]


def clean(name):
    name = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", name)  # links
    name = name.replace("**", "").replace("`", "").replace("\\", "")
    return re.sub(r"\s+", " ", name).strip()


def rows(path, keyed_by_section):
    section, seen, out = "", {}, []
    for line in open(path, encoding="utf-8"):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("|"):
            continue
        c = cells(line)
        if len(c) < 2:
            continue
        mark = next((MARKS[m] for m in MARKS if m in c[1]), None)
        if mark is None:
            continue
        name = clean(c[0])
        note = clean(c[2]) if len(c) > 2 else ""
        key = f"{section}: {name}" if keyed_by_section else name
        if key in seen:  # the commands page repeats commands in its summary tables
            continue
        seen[key] = True
        out.append((key, mark, note))
    return out


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(docs, commit, temper):
    answers = {s: {} for s in DOLTGRES}
    pg = {}
    for lattice, (page, slug, what) in PAGES.items():
        # The commands page lists each command in a section table and again
        # under "All SQL"; functions repeat across sections, so they are keyed
        # by section.
        found = rows(os.path.join(docs, page), keyed_by_section=(lattice == "sql.function"))
        level = "conformance"
        with open(os.path.join(temper, "lattices", lattice + ".yml"), "w") as f:
            f.write(f"id: {lattice}\n")
            f.write("description: >\n")
            f.write(f"  Every {what} on Doltgres's support page {slug}\n")
            f.write(f"  (dolthub/docs-2 @{commit[:12]}), one member per row. Built by\n")
            f.write("  temper/recipe/lattices_from_docs.py.\n")
            f.write("applies_to: database\n")
            f.write(f"requires: [{level}]\n")
            f.write("members:\n")
            for key, mark, note in found:
                f.write(f"  - key: {q(key)}\n")
        for key, mark, note in found:
            pg[(lattice, key)] = True
            for s in DOLTGRES:
                answers[s][(lattice, key)] = (mark, note)
        counts = {m: sum(1 for _, k, _ in found if k == m) for m in MARKS.values()}
        print(f"{lattice}: {len(found)} members {counts}")
    return answers, pg



DRIVERS = ['psql', 'pgJDBC', 'R2DBC PostgreSQL', 'node-postgres (pg)', 'Knex.js', 'Drizzle ORM', 'psycopg2', 'SQLAlchemy', 'pgx', 'lib/pq', 'libpq', 'libaprutil (apr_dbd)', 'libdbi', 'libpqxx', 'psqlODBC', 'DBD::Pg', 'pg (Ruby)', 'Sequel', 'ActiveRecord', 'pgsql (pg_connect)', 'PDO_PGSQL', 'RPostgres', 'RPostgreSQL', 'sqlx', 'Npgsql', 'Postgrex', 'PostgresNIO']
ISOLATION = ["read committed", "repeatable read", "serializable"]


def hand_answers(sid):
    out = "  client.driver:\n" + "".join(f"    {q(d)}: supported\n" for d in DRIVERS)
    out += "  txn.isolation:\n"
    for level in ISOLATION:
        if sid == "postgres.reference" or level == "read committed":
            out += f"    {q(level)}: supported\n"
        else:
            out += f"    {q(level)}:\n      unsupported: true\n"
            out += "      reason: \"The Doltgres docs (concepts/sql/transaction): Doltgres uses the Read Committed transaction model. Requesting this level must be refused, not silently run at another.\"\n"
            out += f"      proven_by: {q(f'txn.isolation/{sid}/txn.isolation/{level}')}\n"
    return out


HAND = {sid: hand_answers(sid) for sid in ["doltgres.release", "doltgres.main", "postgres.reference"]}


SUBJECTS = {
    "doltgres.release": ("Doltgres 1.5.3, the latest release (dolthub/doltgresql:1.5.3).", "server.doltgres", True),
    "doltgres.main": ("Doltgres main, built from source at the commit this map is on.", "server.doltgres", True),
    "postgres.reference": ("PostgreSQL 17 (postgres:17), the reference every answer is compared with.", "reference.postgres", False),
}


def write_subjects(temper, answers, pg):
    for sid, (doc, feature, versioned) in SUBJECTS.items():
        with open(os.path.join(temper, "subjects", sid + ".yml"), "w") as f:
            f.write(f"# {doc}\n")
            f.write("# Lattice answers come from Doltgres's own support pages (or, for\n")
            f.write("# PostgreSQL, from its being the reference), via\n")
            f.write("# temper/recipe/lattices_from_docs.py. The client.driver and\n")
            f.write("# txn.isolation answers are written by hand.\n")
            f.write(f"id: {sid}\nkind: database\nfeature: {feature}\n")
            if versioned:
                f.write("implements: [Versioned]\n")
            f.write("answers:\n")
            by_lattice = {}
            keys = answers[sid] if sid in answers else {k: ("supported", "") for k in pg}
            for (lattice, key), v in keys.items():
                by_lattice.setdefault(lattice, []).append((key, v))
            for lattice in PAGES:
                f.write(f"  {lattice}:\n")
                for key, (mark, note) in by_lattice[lattice]:
                    if mark == "supported":
                        f.write(f"    {q(key)}: supported\n")
                    elif mark == "partial":
                        f.write(f"    {q(key)}: {{supported: true, docs: partial, note: {q(note)}}}\n")
                    else:
                        f.write(f"    {q(key)}:\n")
                        f.write("      unsupported: true\n")
                        f.write(f"      reason: {q('The Doltgres docs mark it unsupported.' + (' ' + note if note else ''))}\n")
                        f.write(f"      proven_by: {q(f'sql.support/{sid}/{lattice}/{key}')}\n")
            f.write(HAND[sid])


if __name__ == "__main__":
    docs, commit, temper = sys.argv[1:4]
    answers, pg = main(docs, commit, temper)
    write_subjects(temper, answers, pg)
