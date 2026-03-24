#!/usr/bin/env python3
import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql
import requests


def parse_args():
    parser = argparse.ArgumentParser(
        description="Call SQLBot in batch and compare generated SQL results with verified MySQL gold."
    )
    parser.add_argument("--sqlbot-base", required=True, help="SQLBot base url, e.g. http://127.0.0.1:8000")
    parser.add_argument("--username", required=True, help="SQLBot login username")
    parser.add_argument("--password", required=True, help="SQLBot login password")
    parser.add_argument("--gold", required=True, help="Verified MySQL gold JSONL path")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--datasource-id", default=None, help="Optional SQLBot datasource id")
    parser.add_argument("--question-limit", type=int, default=0, help="Only run the first N questions, 0 means all")
    parser.add_argument("--float-places", type=int, default=4, help="Float rounding precision for comparison")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-user", required=True)
    parser.add_argument("--mysql-password", required=True)
    parser.add_argument("--mysql-database", required=True)
    parser.add_argument("--config-name", default="sqlbot_eval")
    parser.add_argument("--disable-terms", action="store_true")
    parser.add_argument("--disable-sql-examples", action="store_true")
    parser.add_argument("--disable-custom-prompt", action="store_true")
    parser.add_argument("--include-log-history", action="store_true")
    parser.add_argument(
        "--finish-step",
        default="GENERATE_SQL",
        choices=["GENERATE_SQL", "QUERY_DATA", "GENERATE_CHART"],
        help="Where SQLBot should stop."
    )
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    return parser.parse_args()


def load_jsonl(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def run_sql(conn_args: dict[str, Any], sql: str) -> list[tuple[Any, ...]]:
    connection = pymysql.connect(
        host=conn_args["host"],
        port=conn_args["port"],
        user=conn_args["user"],
        password=conn_args["password"],
        database=conn_args["database"],
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())
    finally:
        connection.close()


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


def login_and_start(base_url: str, username: str, password: str, timeout: int) -> dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/v1/mcp/mcp_start",
        json={"username": username, "password": password},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def ask_sqlbot(base_url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/v1/mcp/mcp_question",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def main():
    args = parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_gold_rows = load_jsonl(args.gold)
    gold_rows = all_gold_rows[:args.question_limit] if args.question_limit and args.question_limit > 0 else all_gold_rows

    mcp_info = login_and_start(args.sqlbot_base, args.username, args.password, args.timeout)
    token = mcp_info["access_token"]
    chat_id = mcp_info["chat_id"]

    mysql_conn_args = {
        "host": args.mysql_host,
        "port": args.mysql_port,
        "user": args.mysql_user,
        "password": args.mysql_password,
        "database": args.mysql_database,
    }

    details: list[dict[str, Any]] = []
    total = len(gold_rows)
    generated = 0
    exec_ok = 0
    result_correct = 0

    for index, gold in enumerate(gold_rows, start=1):
        question_id = gold.get("question_id")
        question = gold.get("question", "")
        gold_sql = clean_sql(gold.get("mysql_sql") or gold.get("sql") or "")

        item = {
            "config": args.config_name,
            "index": index,
            "question_id": question_id,
            "question": question,
            "gold_sql": gold_sql,
            "pred_sql": "",
            "executed_sql": "",
            "exec_ok": False,
            "result_correct": False,
            "error_type": "",
            "sqlbot_record_id": None,
            "notes": "",
            "debug": None,
        }

        payload = {
            "question": question,
            "chat_id": chat_id,
            "token": token,
            "stream": False,
            "datasource_id": args.datasource_id,
            "finish_step": args.finish_step,
            "disable_terms": args.disable_terms,
            "disable_sql_examples": args.disable_sql_examples,
            "disable_custom_prompt": args.disable_custom_prompt,
            "include_debug_payload": True,
            "include_log_history": args.include_log_history,
        }

        try:
            sqlbot_response = ask_sqlbot(args.sqlbot_base, payload, args.timeout)
        except Exception as exc:
            item["error_type"] = "sqlbot_request_error"
            item["notes"] = str(exc)
            details.append(item)
            continue

        item["sqlbot_record_id"] = sqlbot_response.get("record_id")
        item["pred_sql"] = clean_sql(sqlbot_response.get("sql"))
        item["executed_sql"] = clean_sql(sqlbot_response.get("executed_sql") or sqlbot_response.get("sql"))
        item["debug"] = sqlbot_response.get("debug")

        if not item["pred_sql"]:
            item["error_type"] = "missing_sql"
            item["notes"] = json.dumps(sqlbot_response, ensure_ascii=False)[:1000]
            details.append(item)
            continue

        generated += 1

        try:
            gold_result = run_sql(mysql_conn_args, gold_sql)
        except Exception as exc:
            item["error_type"] = "gold_exec_error"
            item["notes"] = str(exc)
            details.append(item)
            continue

        try:
            pred_result = run_sql(mysql_conn_args, item["executed_sql"] or item["pred_sql"])
            item["exec_ok"] = True
            exec_ok += 1
        except Exception as exc:
            item["error_type"] = "exec_error"
            item["notes"] = str(exc)
            details.append(item)
            continue

        ordered = is_order_sensitive(gold_sql) or is_order_sensitive(item["executed_sql"] or item["pred_sql"])
        gold_norm = canonicalize_rows(gold_result, ordered, args.float_places)
        pred_norm = canonicalize_rows(pred_result, ordered, args.float_places)

        if gold_norm == pred_norm:
            item["result_correct"] = True
            item["error_type"] = "correct"
            result_correct += 1
        else:
            item["error_type"] = "wrong_result"
            item["notes"] = (
                f"gold_preview={gold_norm[:5]} | pred_preview={pred_norm[:5]}"
            )

        details.append(item)

    summary = {
        "config": args.config_name,
        "total": total,
        "generated_sql": generated,
        "exec_ok": exec_ok,
        "result_correct": result_correct,
        "sql_generation_rate": round(generated / total, 4) if total else 0.0,
        "exec_rate": round(exec_ok / total, 4) if total else 0.0,
        "result_acc": round(result_correct / total, 4) if total else 0.0,
        "conditional_acc": round(result_correct / exec_ok, 4) if exec_ok else 0.0,
        "sqlbot_base": args.sqlbot_base,
        "chat_id": chat_id,
        "finish_step": args.finish_step,
        "disable_terms": args.disable_terms,
        "disable_sql_examples": args.disable_sql_examples,
        "disable_custom_prompt": args.disable_custom_prompt,
    }

    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with (out_dir / "details.jsonl").open("w", encoding="utf-8") as file:
        for row in details:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    with (out_dir / "details.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "config",
                "index",
                "question_id",
                "question",
                "gold_sql",
                "pred_sql",
                "executed_sql",
                "exec_ok",
                "result_correct",
                "error_type",
                "sqlbot_record_id",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows([{k: row.get(k) for k in writer.fieldnames} for row in details])

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"details_jsonl={out_dir / 'details.jsonl'}")
    print(f"details_csv={out_dir / 'details.csv'}")


if __name__ == "__main__":
    main()
