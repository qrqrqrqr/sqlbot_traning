#!/usr/bin/env python3
import argparse
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
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
        description="Validate a dataset by comparing SQLite gold execution against transpiled MySQL execution."
    )
    parser.add_argument("--dataset-jsonl", required=True, help="Dataset JSONL path")
    parser.add_argument(
        "--sqlite-root",
        action="append",
        required=True,
        help="SQLite root directories. Can be passed multiple times.",
    )
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-user", required=True)
    parser.add_argument("--mysql-password", required=True)
    parser.add_argument("--out-verified", required=True)
    parser.add_argument("--out-rejected", required=True)
    parser.add_argument("--db-id-from", choices=["parent", "stem"], default="parent")
    parser.add_argument("--float-places", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--target-sql-field", default="SQL", help="Dataset field containing source SQL")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def clean_sql(sql: str | None) -> str:
    if not sql:
        return ""
    result = sql.strip()
    if result.startswith("```"):
        lines = result.splitlines()
        if len(lines) >= 3:
            result = "\n".join(lines[1:-1]).strip()
    if result.lower().startswith("sql\n"):
        result = result[4:].strip()
    if result.endswith(";"):
        result = result[:-1].strip()
    return result


def infer_db_id(sqlite_file: Path, mode: str) -> str:
    if mode == "stem":
        return sqlite_file.stem.replace("-", "_").strip()
    parent = sqlite_file.parent.name
    if parent and parent.lower() not in GENERIC_PARENT_DIRS:
        return parent.replace("-", "_").strip()
    return sqlite_file.stem.replace("-", "_").strip()


def discover_sqlite_map(roots: list[str], mode: str) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for root_str in roots:
        root = Path(root_str)
        if not root.exists():
            continue
        for sqlite_file in sorted(list(root.rglob("*.sqlite")) + list(root.rglob("*.db"))):
            db_id = infer_db_id(sqlite_file, mode)
            discovered.setdefault(db_id, sqlite_file)
    return discovered


def normalize_value(value: Any, float_places: int) -> Any:
    if isinstance(value, Decimal):
        return round(float(value), float_places)
    if isinstance(value, float):
        return round(value, float_places)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return repr(value)
    return value


def canonicalize_rows(rows: list[tuple[Any, ...]], ordered: bool, float_places: int) -> list[tuple[Any, ...]]:
    normalized = [tuple(normalize_value(cell, float_places) for cell in row) for row in rows]
    if not ordered:
        normalized = sorted(normalized, key=lambda row: json.dumps(row, ensure_ascii=False))
    return normalized


def is_order_sensitive(sql: str) -> bool:
    lower_sql = f" {sql.lower()} "
    return " order by " in lower_sql or " limit " in lower_sql


def run_sqlite(sqlite_file: Path, sql: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(str(sqlite_file))
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        connection.close()


def run_mysql(host: str, port: int, user: str, password: str, database: str, sql: str) -> list[tuple[Any, ...]]:
    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())
    finally:
        connection.close()


def transpile_sql(sql: str) -> str:
    try:
        from sqlglot import transpile
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("sqlglot is required for dataset verification") from exc

    transpiled = transpile(sql, read="sqlite", write="mysql")[0]
    return transpiled.replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP")


def main():
    args = parse_args()

    sqlite_map = discover_sqlite_map(args.sqlite_root, args.db_id_from)
    dataset_rows = load_jsonl(Path(args.dataset_jsonl))
    if args.limit and args.limit > 0:
        dataset_rows = dataset_rows[:args.limit]

    verified_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []

    for row in dataset_rows:
        db_id = row.get("db_id")
        source_sql = clean_sql(row.get(args.target_sql_field) or row.get("sql") or "")
        item = {
            "question_id": row.get("question_id"),
            "db_id": db_id,
            "question": row.get("question", ""),
            "evidence": row.get("evidence", ""),
            "difficulty": row.get("difficulty"),
            "sqlite_sql": source_sql,
        }

        sqlite_file = sqlite_map.get(db_id)
        if sqlite_file is None:
            item["stage"] = "missing_sqlite_db"
            rejected_rows.append(item)
            continue

        item["sqlite_file"] = str(sqlite_file)
        item["mysql_database"] = db_id

        try:
            sqlite_result = run_sqlite(sqlite_file, source_sql)
        except Exception as exc:
            item["stage"] = "sqlite_exec"
            item["error"] = str(exc)
            rejected_rows.append(item)
            continue

        try:
            mysql_sql = transpile_sql(source_sql)
            item["mysql_sql"] = mysql_sql
        except Exception as exc:
            item["stage"] = "transpile"
            item["error"] = str(exc)
            rejected_rows.append(item)
            continue

        try:
            mysql_result = run_mysql(
                args.mysql_host,
                args.mysql_port,
                args.mysql_user,
                args.mysql_password,
                db_id,
                mysql_sql,
            )
        except Exception as exc:
            item["stage"] = "mysql_exec"
            item["error"] = str(exc)
            rejected_rows.append(item)
            continue

        ordered = is_order_sensitive(source_sql) or is_order_sensitive(mysql_sql)
        sqlite_norm = canonicalize_rows(sqlite_result, ordered, args.float_places)
        mysql_norm = canonicalize_rows(mysql_result, ordered, args.float_places)

        if sqlite_norm == mysql_norm:
            item["result_preview"] = sqlite_norm[:5]
            verified_rows.append(item)
        else:
            item["stage"] = "result_mismatch"
            item["sqlite_result_preview"] = sqlite_norm[:5]
            item["mysql_result_preview"] = mysql_norm[:5]
            item["sqlite_count"] = len(sqlite_norm)
            item["mysql_count"] = len(mysql_norm)
            rejected_rows.append(item)

    verified_path = Path(args.out_verified)
    rejected_path = Path(args.out_rejected)
    verified_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)

    with verified_path.open("w", encoding="utf-8") as file:
        for row in verified_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    with rejected_path.open("w", encoding="utf-8") as file:
        for row in rejected_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "dataset_jsonl": args.dataset_jsonl,
        "total": len(dataset_rows),
        "verified": len(verified_rows),
        "rejected": len(rejected_rows),
        "out_verified": str(verified_path),
        "out_rejected": str(rejected_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
