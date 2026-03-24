#!/usr/bin/env python3
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


MEDICAL_KEYWORDS = {
    "synthea": 20,
    "thrombosis": 18,
    "toxicology": 18,
    "patient": 8,
    "patients": 8,
    "diagnosis": 8,
    "diagnostic": 6,
    "disease": 7,
    "clinical": 7,
    "hospital": 7,
    "clinic": 6,
    "medicine": 7,
    "medical": 7,
    "drug": 7,
    "medication": 8,
    "medications": 8,
    "allergy": 8,
    "allergies": 8,
    "condition": 7,
    "conditions": 7,
    "encounter": 8,
    "encounters": 8,
    "observation": 8,
    "observations": 8,
    "immunization": 8,
    "immunizations": 8,
    "careplan": 7,
    "careplans": 7,
    "symptom": 7,
    "symptoms": 7,
    "lab": 6,
    "laboratory": 8,
    "blood": 6,
    "urine": 6,
    "serum": 6,
    "toxin": 6,
    "admission": 6,
    "outpatient": 6,
    "inpatient": 6,
    "diagnosed": 6,
    "treatment": 7,
    "therapy": 6,
    "antibody": 7,
    "creatinine": 6,
    "bilirubin": 6,
    "cholesterol": 5,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build a medical-domain training split from BIRD train data."
    )
    parser.add_argument("--train-jsonl", required=True, help="Path to bird23_train_filtered.jsonl")
    parser.add_argument("--column-meaning", required=True, help="Path to train_column_meaning.json")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument(
        "--selected-db-ids",
        default="",
        help="Comma-separated db ids. If provided, skip auto selection and use these dbs."
    )
    parser.add_argument(
        "--auto-score-threshold",
        type=int,
        default=40,
        help="Minimum score for auto-selecting a medical database."
    )
    parser.add_argument("--top-k-report", type=int, default=30, help="How many candidate DBs to include in report")
    parser.add_argument("--val-ratio", type=float, default=0.1, help="Validation split ratio")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score_text(text: str) -> tuple[int, set[str]]:
    lowered = text.lower()
    score = 0
    hits = set()
    for keyword, weight in MEDICAL_KEYWORDS.items():
        if keyword in lowered:
            score += weight
            hits.add(keyword)
    return score, hits


def main():
    args = parse_args()
    random.seed(args.seed)

    train_path = Path(args.train_jsonl)
    column_path = Path(args.column_meaning)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_jsonl(train_path)
    column_meaning = json.loads(column_path.read_text(encoding="utf-8"))

    db_scores = Counter()
    db_hits: dict[str, set[str]] = defaultdict(set)
    db_row_counts = Counter()
    db_example_questions: dict[str, list[str]] = defaultdict(list)
    db_schema_examples: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        db_id = row["db_id"]
        db_row_counts[db_id] += 1
        text = f"{db_id} {row.get('question', '')} {row.get('evidence', '')}"
        score, hits = score_text(text)
        db_scores[db_id] += score
        db_hits[db_id].update(hits)
        if len(db_example_questions[db_id]) < 5:
            db_example_questions[db_id].append(row.get("question", ""))

    for full_key, description in column_meaning.items():
        parts = full_key.split("|", 2)
        if len(parts) != 3:
            continue
        db_id, table_name, column_name = parts
        text = f"{db_id} {table_name} {column_name} {description}"
        score, hits = score_text(text)
        db_scores[db_id] += score
        db_hits[db_id].update(hits)
        if hits and len(db_schema_examples[db_id]) < 10:
            db_schema_examples[db_id].append(
                f"{table_name}.{column_name}: {str(description).splitlines()[0]}"
            )

    ranked = []
    for db_id, score in db_scores.most_common():
        ranked.append(
            {
                "db_id": db_id,
                "score": score,
                "row_count": db_row_counts.get(db_id, 0),
                "hits": sorted(db_hits[db_id]),
                "sample_questions": db_example_questions[db_id],
                "schema_examples": db_schema_examples[db_id],
            }
        )

    report = ranked[: args.top_k_report]
    (out_dir / "medical_db_candidates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.selected_db_ids.strip():
        selected_db_ids = [
            db_id.strip() for db_id in args.selected_db_ids.split(",") if db_id.strip()
        ]
    else:
        selected_db_ids = [
            item["db_id"] for item in ranked if item["score"] >= args.auto_score_threshold
        ]

    # Keep the most obvious medical DBs if they exist even when score threshold is too strict.
    for must_have in ("synthea", "thrombosis_prediction", "toxicology"):
        if must_have in db_row_counts and must_have not in selected_db_ids:
            selected_db_ids.append(must_have)

    selected_db_ids = sorted(set(selected_db_ids))
    (out_dir / "medical_selected_db_ids.json").write_text(
        json.dumps(selected_db_ids, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    selected_rows = [row for row in rows if row["db_id"] in selected_db_ids]
    with (out_dir / "medical_train_pool.jsonl").open("w", encoding="utf-8") as file:
        for row in selected_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        grouped_rows[row["db_id"]].append(row)

    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    split_stats = []

    for db_id, db_rows in grouped_rows.items():
        shuffled = db_rows[:]
        random.shuffle(shuffled)

        if len(shuffled) <= 1:
            db_train = shuffled
            db_val = []
        else:
            val_count = max(1, round(len(shuffled) * args.val_ratio))
            val_count = min(val_count, len(shuffled) - 1)
            db_val = shuffled[:val_count]
            db_train = shuffled[val_count:]

        train_rows.extend(db_train)
        val_rows.extend(db_val)
        split_stats.append(
            {
                "db_id": db_id,
                "score": db_scores[db_id],
                "total": len(shuffled),
                "train": len(db_train),
                "val": len(db_val),
                "hits": sorted(db_hits[db_id]),
            }
        )

    random.shuffle(train_rows)
    random.shuffle(val_rows)

    with (out_dir / "medical_train.jsonl").open("w", encoding="utf-8") as file:
        for row in train_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    with (out_dir / "medical_val.jsonl").open("w", encoding="utf-8") as file:
        for row in val_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    (out_dir / "medical_split_stats.json").write_text(
        json.dumps(sorted(split_stats, key=lambda item: (-item["score"], item["db_id"])), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = {
        "selected_db_ids": selected_db_ids,
        "selected_db_count": len(selected_db_ids),
        "pool_rows": len(selected_rows),
        "train_rows": len(train_rows),
        "val_rows": len(val_rows),
        "auto_score_threshold": args.auto_score_threshold,
        "seed": args.seed,
    }
    (out_dir / "medical_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={out_dir / 'medical_db_candidates.json'}")
    print(f"selected_db_ids={out_dir / 'medical_selected_db_ids.json'}")
    print(f"pool={out_dir / 'medical_train_pool.jsonl'}")
    print(f"train={out_dir / 'medical_train.jsonl'}")
    print(f"val={out_dir / 'medical_val.jsonl'}")


if __name__ == "__main__":
    main()
