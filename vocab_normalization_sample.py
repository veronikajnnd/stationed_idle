"""
語彙の正規化調査 — rule3 (軽微作業/maintenance) キーワードの category-driven vs note-only 内訳、
および全角/半角の表記ゆれチェック
Vocabulary normalization investigation for rule3 (maintenance) keywords: category-driven
vs note-only breakdown, plus full-width/half-width variant-form duplicates.

背景 / Background:
    battery.py の Option A 実行結果、rule3 を category+note
    (haystack) 判定にした場合の「collateral」行 (バッテリー消耗と無関係にマッチする行) が
    1,712 件見つかり、その92%（1,574件）が「交換」1語だけで占められていた。
    Miyazawa-san の 2026-09-14 レビューで、この「交換」ほかの collateral キーワード群を
    語彙正規化の提案としてこのタスクで調べるよう指示された。あわせて、rule3のキーワード
    一覧に全角/半角の表記ゆれ（例: バージョンアップ / ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ）がないかも確認する。

    battery.py's Option A run found 1,712 "collateral" rows
    (rows that change purely because rule3 now reads notes, unrelated to バッテリー消耗
    itself), 92% of which (1,574) came from "交換" alone. Per Miyazawa-san's 2026-09-14
    review, this script investigates 交換 and the other collateral keywords as input for
    task [12]'s vocabulary-normalization proposal, and also checks rule3's keyword list
    for full-width/half-width duplicate forms (e.g. バージョンアップ vs ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ).

本番コードは変更しない / does NOT modify the shipped failure_classifier.py:
    battery.py と同じディレクトリに置いて実行する。
    そのファイルの _QUERY / _build_haystack をそのまま再利用する。
    Place this file in the SAME directory as battery.py — it
    imports _QUERY / _build_haystack from that file rather than duplicating them.

実行方法 / How to run (same venv as the other scripts):
    python vocab_normalization_sample.py --medical-facility-id 1
    python vocab_normalization_sample.py --medical-facility-id 1 --sample-size 10 --csv vocab_note_only_rows.csv
"""

import argparse
import csv as csv_module
from collections import Counter

from sqlalchemy import create_engine

from battery import _QUERY, _build_haystack
from streamedix_datacuration.core.failure_classifier import classify_repair_explained as classify_baseline

# rule3 の現行キーワード一覧を、表記ゆれ（全角/半角）がある語はグループにまとめて定義する。
# rule3's current keyword list, grouped so a full-width/half-width pair counts as one word
# for the purpose of this investigation (each group's "forms" are all checked together).
_KEYWORD_GROUPS = [
    {"label": "交換", "forms": ["交換"]},
    {"label": "調整", "forms": ["調整"]},
    {"label": "部品", "forms": ["部品"]},
    {"label": "バージョンアップ", "forms": ["バージョンアップ", "ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ"]},
    {"label": "オーバーホール", "forms": ["オーバーホール", "ｵｰﾊﾞｰﾎｰﾙ"]},
    {"label": "改修", "forms": ["改修"]},
    {"label": "改良", "forms": ["改良"]},
    {"label": "セル交換", "forms": ["セル交換"]},
    {"label": "メンテ", "forms": ["メンテ"]},
    {"label": "是正", "forms": ["是正"]},
]


def _any_form_in(text: str, forms) -> bool:
    return any(f in text for f in forms)


def main() -> int:
    p = argparse.ArgumentParser(
        description="rule3キーワードの category-driven / note-only 内訳と、表記ゆれ確認"
    )
    p.add_argument("--medical-facility-id", type=int, required=True)
    p.add_argument("--db-url", default=None)
    p.add_argument("--sample-size", type=int, default=8, help="キーワードごとに手動確認用に表示する件数")
    p.add_argument("--csv", default=None, help="note-only行(baseline=failure)を全件CSVに書き出す場合のパス")
    args = p.parse_args()

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(_QUERY, {"fid": args.medical_facility_id}).fetchall()

    # グループごとの集計と、手動確認用サンプルの保管場所
    # Per-group tallies, and storage for the manual-reading samples.
    category_driven_counts = Counter()
    note_only_counts = Counter()
    note_only_baseline_cls_counts = {g["label"]: Counter() for g in _KEYWORD_GROUPS}
    note_only_failure_samples = {g["label"]: [] for g in _KEYWORD_GROUPS}
    all_note_only_failure_rows_for_csv = []

    for (cat, ev, fr, wn, rr) in rows:
        category, note_text, _haystack = _build_haystack(cat, ev, fr, wn, rr)

        for group in _KEYWORD_GROUPS:
            label = group["label"]
            forms = group["forms"]
            in_category = _any_form_in(category, forms)
            in_note = _any_form_in(note_text, forms)

            if in_category:
                category_driven_counts[label] += 1
                continue  # 「ノートにしか無い」の対象外 / not a note-only occurrence

            if in_note:
                note_only_counts[label] += 1
                base_cls, _isf, base_rule, base_matched = classify_baseline(cat, ev, fr, wn, rr)
                note_only_baseline_cls_counts[label][base_cls] += 1

                if base_cls == "failure":
                    record = {
                        "keyword_group": label,
                        "repair_category": cat,
                        "event_note": ev,
                        "failure_reason": fr,
                        "work_note": wn,
                        "repair_result": rr,
                        "baseline_rule": base_rule,
                        "baseline_matched_word": base_matched,
                    }
                    all_note_only_failure_rows_for_csv.append(record)
                    if len(note_only_failure_samples[label]) < args.sample_size:
                        note_only_failure_samples[label].append(record)

    print(f"Total facility_id={args.medical_facility_id} rows: {len(rows):,}")
    print()
    print("=== category-driven vs note-only, by keyword group ===")
    for group in _KEYWORD_GROUPS:
        label = group["label"]
        forms_shown = "/".join(group["forms"])
        cat_n = category_driven_counts[label]
        note_n = note_only_counts[label]
        print(f"  {label} ({forms_shown}): category-driven={cat_n:,}  note-only={note_n:,}")
        if note_n:
            breakdown = dict(note_only_baseline_cls_counts[label])
            print(f"      note-only baseline classification breakdown: {breakdown}")
    print()

    print("=== sample rows: note-only AND currently classified 'failure' (for manual reading) ===")
    for group in _KEYWORD_GROUPS:
        label = group["label"]
        samples = note_only_failure_samples[label]
        if not samples:
            continue
        print(f"\n--- {label}, showing {len(samples)} of {note_only_baseline_cls_counts[label].get('failure', 0):,} ---")
        for i, row in enumerate(samples, start=1):
            print(f"  [{i}]")
            for k, v in row.items():
                if k == "keyword_group":
                    continue
                print(f"    {k}: {v!r}")

    if args.csv:
        fieldnames = [
            "keyword_group",
            "repair_category",
            "event_note",
            "failure_reason",
            "work_note",
            "repair_result",
            "baseline_rule",
            "baseline_matched_word",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_note_only_failure_rows_for_csv)
        print(f"\nWrote {len(all_note_only_failure_rows_for_csv)} note-only/failure rows to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())