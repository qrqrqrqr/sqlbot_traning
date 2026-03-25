#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any

import requests


def parse_args():
    parser = argparse.ArgumentParser(
        description="Capture SQLBot retrieval context for each question in a JSONL source set."
    )
    parser.add_argument("--sqlbot-base", required=True, help="SQLBot base url, for example http://127.0.0.1:8000")
    parser.add_argument("--username", required=True, help="SQLBot login username")
    parser.add_argument("--password", required=True, help="SQLBot login password")
    parser.add_argument("--in-jsonl", required=True, help="Input JSONL path")
    parser.add_argument("--out-jsonl", required=True, help="Output capture JSONL path")
    parser.add_argument("--datasource-id", default=None, help="SQLBot datasource id")
    parser.add_argument("--question-limit", type=int, default=0, help="Only capture the first N rows")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    parser.add_argument(
        "--finish-step",
        default="GENERATE_SQL",
        choices=["GENERATE_SQL", "QUERY_DATA", "GENERATE_CHART"],
        help="Where SQLBot should stop",
    )
    parser.add_argument("--disable-terms", action="store_true")
    parser.add_argument("--disable-sql-examples", action="store_true")
    parser.add_argument("--disable-custom-prompt", action="store_true")
    parser.add_argument("--include-log-history", action="store_true")
    parser.add_argument("--keep-full-response", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip question_ids already present in output")
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


def build_capture_record(
    source_row: dict[str, Any],
    sqlbot_response: dict[str, Any] | None,
    error_type: str = "",
    error_message: str = "",
    keep_full_response: bool = False,
) -> dict[str, Any]:
    debug_payload = (sqlbot_response or {}).get("debug") or {}
    retrieval = debug_payload.get("retrieval") or {}
    datasource = debug_payload.get("datasource") or {}

    record = {
        "question_id": source_row.get("question_id"),
        "db_id": source_row.get("db_id"),
        "question": source_row.get("question", ""),
        "evidence": source_row.get("evidence", ""),
        "difficulty": source_row.get("difficulty"),
        "source_sql": source_row.get("SQL") or source_row.get("sql") or "",
        "gold_sql": source_row.get("mysql_sql") or source_row.get("gold_sql") or source_row.get("SQL") or source_row.get("sql") or "",
        "sqlbot_record_id": (sqlbot_response or {}).get("record_id"),
        "pred_sql": clean_sql((sqlbot_response or {}).get("sql")),
        "executed_sql": clean_sql((sqlbot_response or {}).get("executed_sql") or (sqlbot_response or {}).get("sql")),
        "success": bool((sqlbot_response or {}).get("success", False)),
        "message": (sqlbot_response or {}).get("message", ""),
        "error_type": error_type,
        "error_message": error_message,
        "sqlbot_context": {
            "engine": datasource.get("engine", ""),
            "datasource": datasource,
            "db_schema": retrieval.get("db_schema", ""),
            "terminologies": retrieval.get("terminologies", ""),
            "sql_examples": retrieval.get("sql_examples", ""),
            "custom_prompt": retrieval.get("custom_prompt", ""),
        },
    }
    if (sqlbot_response or {}).get("log_history") is not None:
        record["log_history"] = (sqlbot_response or {}).get("log_history")
    if keep_full_response and sqlbot_response is not None:
        record["raw_response"] = sqlbot_response
    return record


def main():
    args = parse_args()

    in_path = Path(args.in_jsonl)
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_path.with_name(f"{out_path.stem}_summary.json")

    source_rows = load_jsonl(in_path)
    if args.question_limit and args.question_limit > 0:
        source_rows = source_rows[:args.question_limit]

    existing_question_ids: set[Any] = set()
    existing_lines: list[str] = []
    if args.resume and out_path.exists():
        existing_lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for line in existing_lines:
            try:
                existing_question_ids.add(json.loads(line).get("question_id"))
            except json.JSONDecodeError:
                continue

    mcp_info = login_and_start(args.sqlbot_base, args.username, args.password, args.timeout)
    token = mcp_info["access_token"]
    chat_id = mcp_info["chat_id"]

    captured = 0
    skipped = 0
    errors = 0
    written = 0

    with out_path.open("a" if args.resume and out_path.exists() else "w", encoding="utf-8") as file:
        for source_row in source_rows:
            question_id = source_row.get("question_id")
            if question_id in existing_question_ids:
                skipped += 1
                continue

            payload = {
                "question": source_row.get("question", ""),
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
                capture_record = build_capture_record(
                    source_row=source_row,
                    sqlbot_response=sqlbot_response,
                    keep_full_response=args.keep_full_response,
                )
                captured += 1
            except Exception as exc:
                capture_record = build_capture_record(
                    source_row=source_row,
                    sqlbot_response=None,
                    error_type="sqlbot_request_error",
                    error_message=str(exc),
                    keep_full_response=args.keep_full_response,
                )
                errors += 1

            file.write(json.dumps(capture_record, ensure_ascii=False) + "\n")
            written += 1

    summary = {
        "sqlbot_base": args.sqlbot_base,
        "datasource_id": args.datasource_id,
        "chat_id": chat_id,
        "finish_step": args.finish_step,
        "disable_terms": args.disable_terms,
        "disable_sql_examples": args.disable_sql_examples,
        "disable_custom_prompt": args.disable_custom_prompt,
        "input_rows": len(source_rows),
        "captured_rows": captured,
        "error_rows": errors,
        "skipped_rows": skipped,
        "written_rows": written,
        "output_path": str(out_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"capture={out_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
