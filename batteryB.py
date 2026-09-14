"""
バッテリー消耗 かつ Option B でも failure のままの行 — 手動検証用サンプル抽出
Rows containing バッテリー消耗 that STILL classify as `failure` under Option B — sample
extraction for manual verification.

背景 / Background:
    battery_wear_option_ab_simulation.py の実行結果、facility_id=1 で category+note に
    「バッテリー消耗」を含む 2,745 件のうち 2,696 件が maintenance/inspection へ変わり、
    残り 49 件 (2,745 - 2,696) は Option B でも `failure` のままだった。
    worklog では「rule1 が別の故障シグナル (例: バッテリー破損) を拾っているため failure の
    ままになっている」と説明したが、これは未検証の推測だった。Miyazawa-san の
    2026-09-14 レビューで「実際にそのデータを見て確認するように」と指摘されたため、
    この 49 件を実際に特定して中身を読めるようにするスクリプト。

    After battery_wear_option_ab_simulation.py, 2,696 of the 2,745 バッテリー消耗 rows
    (facility_id=1) move to maintenance/inspection; the remaining 49 stay `failure` under
    Option B. The worklog's explanation ("another failure signal such as バッテリー破損
    co-occurs, so rule1 still fires") was an unverified assumption. Per Miyazawa-san's
    2026-09-14 review, this script identifies those exact rows so they can be read
    manually instead of assumed.

本番コードは変更しない / does NOT modify the shipped failure_classifier.py:
    battery_wear_option_ab_simulation.py と同じディレクトリに置いて実行する前提。
    そのファイルの classify_option_b / _build_haystack をそのまま再利用する。
    Place this file in the SAME directory as battery_wear_option_ab_simulation.py and run
    it there — it imports classify_option_b / _build_haystack from that file rather than
    duplicating the logic, so the two scripts can never silently drift apart.

実行方法 / How to run (same environment as the other two scripts):
    python battery_wear_remaining_failure_sample.py --medical-facility-id 1
    python battery_wear_remaining_failure_sample.py --medical-facility-id 1 --csv remaining_failure_rows.csv
"""

import argparse
import csv as csv_module

from sqlalchemy import create_engine

# 同じフォルダの既存スクリプトから再利用する（ロジックを重複させない）
# Reuse from the sibling script instead of duplicating the classification logic.
from battery import _QUERY, _build_haystack, classify_option_b
from streamedix_datacuration.core.failure_classifier import FAILURE


def find_remaining_failure_rows(rows):
    """
    「バッテリー消耗」を含み、かつ Option B でも failure のままの行だけを抽出する。
    Filter down to rows that contain バッテリー消耗 AND still classify as `failure`
    under Option B — this is exactly the 49-row set referenced in the worklog.

    どのルール・どの語が実際にマッチして failure になったかも一緒に返す。これが
    「本当に別の故障シグナルが原因か」を確認するための一番大事な情報。
    Also returns which rule/keyword actually matched, since that is the key piece of
    evidence for checking whether a genuine co-occurring failure signal is the real cause.
    """
    matches = []
    for (cat, ev, fr, wn, rr) in rows:
        _, _, haystack = _build_haystack(cat, ev, fr, wn, rr)
        if "バッテリー消耗" not in haystack:
            continue

        b_cls, b_rule, b_matched = classify_option_b(cat, ev, fr, wn, rr)
        if b_cls != FAILURE:
            continue  # Option B で変化した行（2,696件側）はここでは対象外

        matches.append(
            {
                "repair_category": cat,
                "event_note": ev,
                "failure_reason": fr,
                "work_note": wn,
                "repair_result": rr,
                "option_b_rule": b_rule,
                "option_b_matched_word": b_matched,
            }
        )
    return matches


def main() -> int:
    p = argparse.ArgumentParser(
        description="バッテリー消耗を含み、かつ Option B でも failure のままの行を抽出（手動検証用）"
    )
    p.add_argument("--medical-facility-id", type=int, required=True)
    p.add_argument("--db-url", default=None)
    p.add_argument("--csv", default=None, help="指定するとCSVにも書き出す / also write to this CSV path if given")
    args = p.parse_args()

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    # 本番と同じクエリ（battery_wear_option_ab_simulation.py と共通）でデータを取得
    # Fetch data using the same query as the other simulation script, so results line up.
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(_QUERY, {"fid": args.medical_facility_id}).fetchall()

    matches = find_remaining_failure_rows(rows)

    print(f"Total facility_id={args.medical_facility_id} rows scanned: {len(rows):,}")
    print(f"Rows containing バッテリー消耗 AND still 'failure' under Option B: {len(matches)}")
    print()

    for i, row in enumerate(matches, start=1):
        print(f"--- row {i} ---")
        for k, v in row.items():
            print(f"  {k}: {v!r}")
        print()

    if args.csv:
        fieldnames = list(matches[0].keys()) if matches else [
            "repair_category",
            "event_note",
            "failure_reason",
            "work_note",
            "repair_result",
            "option_b_rule",
            "option_b_matched_word",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matches)
        print(f"Wrote {len(matches)} rows to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())