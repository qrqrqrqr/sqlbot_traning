#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any


def parse_args():
    parser = argparse.ArgumentParser(
        description="Render SQLBot capture JSONL into SFT-ready SQLBot-aligned JSONL."
    )
    parser.add_argument("--capture-jsonl", required=True, help="Captured SQLBot context JSONL path")
    parser.add_argument("--out-jsonl", required=True, help="Output aligned JSONL path")
    parser.add_argument(
        "--target-sql-field",
        default="auto",
        help="Which field to use as assistant target. auto prefers gold_sql, then source_sql",
    )
    parser.add_argument("--dataset-name", default="sqlbot_aligned_capture", help="Dataset name for meta")
    parser.add_argument("--transpile-read", default="", help="Optional source dialect for sqlglot transpile")
    parser.add_argument("--transpile-write", default="", help="Optional target dialect for sqlglot transpile")
    parser.add_argument("--include-evidence", action="store_true", help="Append evidence to user content")
    parser.add_argument("--keep-debug-meta", action="store_true", help="Keep raw debug-style meta fields")
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


def maybe_transpile(sql: str, read_dialect: str, write_dialect: str) -> str:
    if not sql or not read_dialect or not write_dialect:
        return sql
    try:
        from sqlglot import transpile
    except ImportError as exc:  # pragma: no cover - runtime dependency check
        raise RuntimeError("sqlglot is required when transpile flags are provided") from exc

    return transpile(sql, read=read_dialect, write=write_dialect)[0]


def choose_target_sql(row: dict[str, Any], target_sql_field: str) -> str:
    if target_sql_field != "auto":
        return clean_sql(row.get(target_sql_field))
    return clean_sql(
        row.get("gold_sql")
        or row.get("mysql_sql")
        or row.get("source_sql")
        or row.get("SQL")
        or row.get("sql")
    )


def build_system_content(context: dict[str, Any]) -> str:
    engine = context.get("engine") or "SQL"
    db_schema = context.get("db_schema") or ""
    terminologies = context.get("terminologies") or ""
    sql_examples = context.get("sql_examples") or ""
    custom_prompt = context.get("custom_prompt") or ""

    parts = [
        f"You are SQLBOT, a text-to-SQL assistant. Generate one valid {engine} query only.",
        "Use only the provided schema and retrieved context.",
        "Return SQL only.",
        "",
        "<Info>",
        "<db-engine>",
        str(engine),
        "</db-engine>",
        "<m-schema>",
        db_schema,
        "</m-schema>",
    ]

    if terminologies:
        parts.extend([terminologies])
    if sql_examples:
        parts.extend([sql_examples])
    if custom_prompt:
        parts.extend(["<Other-Infos>", custom_prompt, "</Other-Infos>"])

    parts.extend(["</Info>"])
    return "\n".join(part for part in parts if part is not None)


def build_user_content(row: dict[str, Any], include_evidence: bool) -> str:
    question = row.get("question", "")
    parts = ["<user-question>", question, "</user-question>"]
    if include_evidence and row.get("evidence"):
        parts.extend(["<evidence>", str(row.get("evidence")), "</evidence>"])
    return "\n".join(parts)


def main():
    args = parse_args()

    capture_path = Path(args.capture_jsonl)
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_path.with_name(f"{out_path.stem}_summary.json")

    rows = load_jsonl(capture_path)

    rendered_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []

    for row in rows:
        context = row.get("sqlbot_context") or {}
        target_sql = choose_target_sql(row, args.target_sql_field)
        target_sql = maybe_transpile(target_sql, args.transpile_read, args.transpile_write)

        if not row.get("question"):
            skipped_rows.append({"question_id": row.get("question_id"), "reason": "missing_question"})
            continue
        if not target_sql:
            skipped_rows.append({"question_id": row.get("question_id"), "reason": "missing_target_sql"})
            continue
        if not context.get("db_schema"):
            skipped_rows.append({"question_id": row.get("question_id"), "reason": "missing_db_schema"})
            continue

        rendered = {
            "question_id": row.get("question_id"),
            "db_id": row.get("db_id"),
            "task_type": "sqlbot_aligned_sft",
            "prompt_pack": {
                "engine": context.get("engine", ""),
                "db_schema": context.get("db_schema", ""),
                "terminologies": context.get("terminologies", ""),
                "sql_examples": context.get("sql_examples", ""),
                "custom_prompt": context.get("custom_prompt", ""),
                "question": row.get("question", ""),
            },
            "messages": [
                {
                    "role": "system",
                    "content": build_system_content(context),
                },
                {
                    "role": "user",
                    "content": build_user_content(row, args.include_evidence),
                },
                {
                    "role": "assistant",
                    "content": target_sql,
                },
            ],
            "target": {
                "gold_sql": clean_sql(row.get("gold_sql") or row.get("source_sql")),
                "source_sql": clean_sql(row.get("source_sql")),
                "target_sql": target_sql,
            },
            "meta": {
                "dataset_name": args.dataset_name,
                "question": row.get("question", ""),
                "evidence": row.get("evidence", ""),
                "difficulty": row.get("difficulty"),
                "pred_sql": clean_sql(row.get("pred_sql")),
                "executed_sql": clean_sql(row.get("executed_sql")),
                "engine": context.get("engine", ""),
                "datasource": context.get("datasource", {}),
            },
        }

        if args.keep_debug_meta:
            rendered["meta"]["success"] = row.get("success")
            rendered["meta"]["message"] = row.get("message", "")
            rendered["meta"]["error_type"] = row.get("error_type", "")
            rendered["meta"]["error_message"] = row.get("error_message", "")
            if row.get("log_history") is not None:
                rendered["meta"]["log_history"] = row.get("log_history")
            if row.get("raw_response") is not None:
                rendered["meta"]["raw_response"] = row.get("raw_response")

        rendered_rows.append(rendered)

    with out_path.open("w", encoding="utf-8") as file:
        for row in rendered_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "dataset_name": args.dataset_name,
        "input_rows": len(rows),
        "rendered_rows": len(rendered_rows),
        "skipped_rows": len(skipped_rows),
        "target_sql_field": args.target_sql_field,
        "transpile_read": args.transpile_read,
        "transpile_write": args.transpile_write,
        "output_path": str(out_path),
    }
    if skipped_rows:
        summary["skipped_examples"] = skipped_rows[:10]

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"aligned_jsonl={out_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
