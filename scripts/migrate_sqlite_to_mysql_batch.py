#!/usr/bin/env python3
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import pymysql


GENERIC_PARENT_DIRS = {
    "train",
    "train_assets",
    "train_databases",
    "raw",
    "dev",
    "dev_databases",
    "database",
    "databases",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Recursively migrate SQLite databases into one-MySQL-database-per-db_id."
    )
    parser.add_argument(
        "--sqlite-root",
        action="append",
        required=True,
        help="Root directory to scan. Can be passed multiple times.",
    )
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-user", required=True)
    parser.add_argument("--mysql-password", required=True)
    parser.add_argument(
        "--mysql-admin-user",
        default="",
        help="Optional admin user for CREATE/DROP DATABASE. Falls back to mysql-user.",
    )
    parser.add_argument(
        "--mysql-admin-password",
        default="",
        help="Optional admin password for CREATE/DROP DATABASE. Falls back to mysql-password.",
    )
    parser.add_argument("--mysql-charset", default="utf8mb4")
    parser.add_argument("--drop-existing", action="store_true", help="Drop target database before import")
    parser.add_argument(
        "--db-id-from",
        choices=["parent", "stem"],
        default="parent",
        help="How to derive db_id from sqlite path",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N databases")
    parser.add_argument("--out-registry", default="", help="Optional JSON path for db registry output")
    return parser.parse_args()


def sqlite_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def mysql_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def normalize_db_name(name: str) -> str:
    return name.replace("-", "_").strip()


def infer_db_id(sqlite_file: Path, mode: str) -> str:
    if mode == "stem":
        return normalize_db_name(sqlite_file.stem)

    parent = sqlite_file.parent.name
    if parent and parent.lower() not in GENERIC_PARENT_DIRS:
        return normalize_db_name(parent)
    return normalize_db_name(sqlite_file.stem)


def map_sqlite_type(sqlite_type: str, is_pk: bool = False) -> str:
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "BIGINT"
    if any(x in t for x in ["CHAR", "CLOB", "TEXT", "VARCHAR"]):
        return "VARCHAR(255)" if is_pk else "LONGTEXT"
    if "BLOB" in t or t == "":
        return "VARBINARY(255)" if is_pk else "LONGBLOB"
    if any(x in t for x in ["REAL", "FLOA", "DOUB"]):
        return "DOUBLE"
    if any(x in t for x in ["NUMERIC", "DECIMAL"]):
        return "DECIMAL(38,10)"
    if "BOOL" in t:
        return "BOOLEAN"
    if "DATE" in t and "TIME" in t:
        return "DATETIME"
    if "DATE" in t:
        return "DATE"
    if "TIME" in t:
        return "TIME"
    return "LONGTEXT"


def discover_sqlite_files(roots: list[str], mode: str) -> list[tuple[str, Path]]:
    discovered: dict[str, Path] = {}
    for root_str in roots:
        root = Path(root_str)
        if not root.exists():
            continue
        for sqlite_file in sorted(list(root.rglob("*.sqlite")) + list(root.rglob("*.db"))):
            db_id = infer_db_id(sqlite_file, mode)
            discovered.setdefault(db_id, sqlite_file)
    return sorted(discovered.items(), key=lambda item: item[0])


def get_mysql_conn(host: str, port: int, user: str, password: str, database: str | None = None):
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=False,
    )


def ensure_database(admin_conf: dict[str, Any], db_name: str, charset: str, drop_existing: bool):
    conn = get_mysql_conn(**admin_conf, database=None)
    try:
        with conn.cursor() as cur:
            if drop_existing:
                cur.execute(f"DROP DATABASE IF EXISTS {mysql_quote(db_name)}")
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {mysql_quote(db_name)} CHARACTER SET {charset} COLLATE {charset}_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def migrate_one(sqlite_file: Path, db_name: str, mysql_conf: dict[str, Any]):
    src = sqlite3.connect(str(sqlite_file))
    src.row_factory = sqlite3.Row
    dst = get_mysql_conn(**mysql_conf, database=db_name)

    table_summaries: list[dict[str, Any]] = []

    try:
        scur = src.cursor()
        dcur = dst.cursor()

        scur.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        tables = [row[0] for row in scur.fetchall()]

        for table in tables:
            scur.execute(f"PRAGMA table_info({sqlite_quote(table)})")
            columns = scur.fetchall()
            if not columns:
                continue

            pk_columns = [col["name"] for col in sorted(columns, key=lambda item: item["pk"]) if col["pk"] > 0]

            column_defs = []
            column_names = []
            for col in columns:
                column_name = col["name"]
                is_pk = col["pk"] > 0
                mysql_type = map_sqlite_type(col["type"], is_pk=is_pk)
                not_null = " NOT NULL" if (col["notnull"] or is_pk) else ""
                column_defs.append(f"{mysql_quote(column_name)} {mysql_type}{not_null}")
                column_names.append(column_name)

            pk_clause = ""
            if pk_columns:
                pk_clause = f", PRIMARY KEY ({', '.join(mysql_quote(name) for name in pk_columns)})"

            create_sql = (
                f"CREATE TABLE {mysql_quote(table)} ("
                f"{', '.join(column_defs)}{pk_clause}"
                f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )

            dcur.execute(f"DROP TABLE IF EXISTS {mysql_quote(table)}")
            dcur.execute(create_sql)
            dst.commit()

            scur.execute(f"SELECT * FROM {sqlite_quote(table)}")
            placeholders = ", ".join(["%s"] * len(column_names))
            insert_sql = (
                f"INSERT INTO {mysql_quote(table)} "
                f"({', '.join(mysql_quote(name) for name in column_names)}) "
                f"VALUES ({placeholders})"
            )

            inserted = 0
            while True:
                rows = scur.fetchmany(2000)
                if not rows:
                    break
                batch = [tuple(row[name] for name in column_names) for row in rows]
                dcur.executemany(insert_sql, batch)
                dst.commit()
                inserted += len(batch)

            table_summaries.append({"table": table, "rows": inserted, "columns": len(column_names)})

    finally:
        src.close()
        dst.close()

    return table_summaries


def main():
    args = parse_args()

    admin_user = args.mysql_admin_user or args.mysql_user
    admin_password = args.mysql_admin_password or args.mysql_password

    admin_conf = {
        "host": args.mysql_host,
        "port": args.mysql_port,
        "user": admin_user,
        "password": admin_password,
    }
    mysql_conf = {
        "host": args.mysql_host,
        "port": args.mysql_port,
        "user": args.mysql_user,
        "password": args.mysql_password,
    }

    discovered = discover_sqlite_files(args.sqlite_root, args.db_id_from)
    if args.limit and args.limit > 0:
        discovered = discovered[:args.limit]

    results = []
    for index, (db_id, sqlite_file) in enumerate(discovered, start=1):
        print(f"[{index}/{len(discovered)}] migrating {db_id} from {sqlite_file}", flush=True)
        try:
            ensure_database(admin_conf, db_id, args.mysql_charset, args.drop_existing)
            table_summaries = migrate_one(sqlite_file, db_id, mysql_conf)
            result = {
                "db_id": db_id,
                "sqlite_file": str(sqlite_file),
                "mysql_database": db_id,
                "status": "ok",
                "table_count": len(table_summaries),
                "tables": table_summaries,
            }
        except Exception as exc:
            result = {
                "db_id": db_id,
                "sqlite_file": str(sqlite_file),
                "mysql_database": db_id,
                "status": "error",
                "error": str(exc),
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    summary = {
        "total": len(results),
        "ok": sum(1 for item in results if item["status"] == "ok"),
        "error": sum(1 for item in results if item["status"] == "error"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.out_registry:
        registry = {
            item["db_id"]: {
                "sqlite_file": item["sqlite_file"],
                "mysql_database": item["mysql_database"],
                "status": item["status"],
            }
            for item in results
        }
        Path(args.out_registry).write_text(
            json.dumps({"summary": summary, "databases": registry}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"registry={args.out_registry}")


if __name__ == "__main__":
    main()
