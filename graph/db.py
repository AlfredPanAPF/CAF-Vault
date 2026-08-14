"""Postgres access + migrations. schema/*.sql applied in filename order, tracked
in schema_migrations."""
import psycopg
from psycopg.rows import dict_row

from . import config


def connect():
    return psycopg.connect(config.DB_URL, row_factory=dict_row)


def migrate():
    with connect() as con:
        con.execute("create table if not exists schema_migrations "
                    "(version text primary key, applied_at timestamptz default now())")
        done = {r["version"] for r in con.execute("select version from schema_migrations")}
        for f in sorted((config.REPO / "schema").glob("*.sql")):
            if f.name in done:
                continue
            print(f"applying {f.name}")
            con.execute(f.read_text())
            con.execute("insert into schema_migrations (version) values (%s)", (f.name,))
        con.commit()


def get_or_create_source(con, name, connector, url=None, is_internal=False):
    row = con.execute("select source_id from source where name=%s and connector=%s",
                      (name, connector)).fetchone()
    if row:
        return row["source_id"]
    return con.execute(
        "insert into source (name, connector, url, is_internal) values (%s,%s,%s,%s) "
        "returning source_id", (name, connector, url, is_internal)).fetchone()["source_id"]
