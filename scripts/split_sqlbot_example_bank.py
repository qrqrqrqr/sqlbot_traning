#!/usr/bin/env python3
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args():
    parser = argparse.ArgumentParser(
        description="Split a train set into SQL example bank and capture-ready train source."
    )
    parser.add_argument("--train-jsonl", required=True, help="Training JSONL path")
    parser.add_argument("--val-jsonl", required=True, help="Validation JSONL path")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument(
        "--example-ratio",
        type=float,
        default=0.2,
        help="Portion of train rows to reserve as SQLBot example bank",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--prefix",
        default="medical",
        help="Output file prefix, for example medical or synthea",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]):
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    args = parse_args()
    random.seed(args.seed)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    train_rows = load_jsonl(Path(args.train_jsonl))
    val_rows = load_jsonl(Path(args.val_jsonl))

    grouped_train: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in train_rows:
        grouped_train[row.get("db_id", "unknown")] .append(row)

    example_rows: list[dict[str, Any]] = []
    capture_train_rows: list[dict[str, Any]] = []
    split_stats: list[dict[str, Any]] = []

    for db_id, rows in grouped_train.items():
        shuffled = rows[:]
        random.shuffle(shuffled)

        if len(shuffled) <= 1:
            db_example_rows = []
            db_capture_rows = shuffled
        else:
            example_count = max(1, round(len(shuffled) * args.example_ratio))
            example_count = min(example_count, len(shuffled) - 1)
            db_example_rows = shuffled[:example_count]
            db_capture_rows = shuffled[example_count:]

        example_rows.extend(db_example_rows)
        capture_train_rows.extend(db_capture_rows)
        split_stats.append(
            {
                "db_id": db_id,
                "train_total": len(shuffled),
                "example_bank": len(db_example_rows),
                "capture_train": len(db_capture_rows),
            }
        )

    random.shuffle(example_rows)
    random.shuffle(capture_train_rows)

    prefix = args.prefix
    example_path = out_dir / f"{prefix}_example_bank.jsonl"
    capture_train_path = out_dir / f"{prefix}_capture_train_source.jsonl"
    capture_val_path = out_dir / f"{prefix}_capture_val_source.jsonl"
    summary_path = out_dir / f"{prefix}_example_split_summary.json"

    write_jsonl(example_path, example_rows)
    write_jsonl(capture_train_path, capture_train_rows)
    write_jsonl(capture_val_path, val_rows)

    summary = {
        "prefix": prefix,
        "seed": args.seed,
        "example_ratio": args.example_ratio,
        "train_total": len(train_rows),
        "example_bank_rows": len(example_rows),
        "capture_train_rows": len(capture_train_rows),
        "capture_val_rows": len(val_rows),
        "train_db_counts": Counter(row.get("db_id", "unknown") for row in train_rows),
        "example_db_counts": Counter(row.get("db_id", "unknown") for row in example_rows),
        "capture_train_db_counts": Counter(row.get("db_id", "unknown") for row in capture_train_rows),
        "capture_val_db_counts": Counter(row.get("db_id", "unknown") for row in val_rows),
        "per_db_split": sorted(split_stats, key=lambda item: item["db_id"]),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"example_bank={example_path}")
    print(f"capture_train={capture_train_path}")
    print(f"capture_val={capture_val_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
