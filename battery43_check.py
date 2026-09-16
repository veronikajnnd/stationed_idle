"""
battery43.py の追加確認 — 台帳リンク率の比較、およびベンダー/担当者名の偏りの定量化
Follow-up checks for battery43.py's output.

背景 / Background:
    battery43.py を facility_id=1 で実行した結果、43件の `修理不能` 対象行すべてで
    `medical_device_ledger_id` が NULL だった（台帳リンク率 0%）。これが facility 全体
    としても普通のことなのか、この43件に限った異常なのかをまず確認する必要がある。
    また、43件中ほぼ全ての `repair_result` に同じ担当者名（岩渕さん）と同じ業者名
    （イノメディックス）が繰り返し出ていることが手動確認で見えたため、その偏りを
    実際の件数で定量化する。この2点は、モデル別の集中がそのままモデル固有の故障率
    上昇として解釈してよいかどうかを判断する材料になる。

    Running battery43.py for facility_id=1 showed that all 43 target 修理不能 rows have
    a NULL `medical_device_ledger_id` (0% ledger link rate). Before treating that as a
    finding on its own, we need to know whether that link rate is normal for this facility
    overall, or specific to this 43-row subset. Separately, manual reading of the 43 rows'
    `repair_result` text showed the same technician name (岩渕さん) and vendor name
    (イノメディックス) repeating across nearly all of them; this script quantifies that
    instead of relying on eyeballing the printed rows. Both numbers matter for deciding
    whether the model-level concentration found in battery43.py can be read as a genuine
    device-level failure-rate signal, or whether it is confounded by one vendor's reporting
    habits.

本番コードは変更しない / does NOT modify the shipped classifier:
    `battery.py` / `battery43.py` と同じディレクトリに置いて実行する。両方から
    再利用するだけで、新しいSQLクエリは追加しない（battery43.py の _QUERY をそのまま使う）。
    Place this file in the SAME directory as `battery.py` and `battery43.py`. It reuses
    battery43.py's `_QUERY` (facility-wide rows, LEFT JOIN to the ledger) instead of
    writing a new query, and battery.py's `_build_haystack` / `classify_option_b` for the
    same target-row filter battery43.py used.

実行方法 / How to run (same venv as the other scripts):
    python battery43_check.py --medical-facility-id 1
"""

import argparse

from sqlalchemy import create_engine

from battery import _build_haystack, classify_option_b
from battery43 import _QUERY

# 「岩渕さん」表記ゆれ（岩渕/岩淵）と業者名「イノメ」（イノメディックス）をまとめて拾うマーカー。
# repair_result の手動確認で見えた表記ゆれをそのままここに反映している。
# Markers covering the spelling variants seen in repair_result during manual reading
# (岩渕 vs 岩淵 for the technician's name, イノメ as the short form of イノメディックス).
_VENDOR_MARKERS = ["岩渕", "岩淵", "イノメ"]


def main() -> int:
    p = argparse.ArgumentParser(
        description="台帳リンク率(全体 vs 対象43件)とベンダー名の偏りを確認"
    )
    p.add_argument("--medical-facility-id", type=int, required=True)
    p.add_argument("--db-url", default=None)
    args = p.parse_args()

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(_QUERY, {"fid": args.medical_facility_id}).fetchall()

    # facility全体の台帳リンク率と、対象43件（バッテリー消耗+Option Bでもfailure+修理不能一致）
    # を同じ1回のフェッチから同時に集計する。
    # Compute the facility-wide ledger link rate and the same 43-row target filter as
    # battery43.py in a single pass over the already-fetched rows.
    total = len(rows)
    total_linked = 0
    target_rows = []

    for row in rows:
        (
            cat, ev, fr, wn, rr,
            ledger_id,
            repair_client_device_number, repair_device_number,
            repair_product_name, repair_model_number, repair_manufacturer_name,
            ledger_product_name, ledger_model_number, ledger_manufacturer_name,
            ledger_device_category,
        ) = row

        if ledger_id is not None:
            total_linked += 1

        _, _, haystack = _build_haystack(cat, ev, fr, wn, rr)
        if "バッテリー消耗" not in haystack:
            continue

        b_cls, b_rule, b_matched = classify_option_b(cat, ev, fr, wn, rr)
        if b_cls != "failure" or b_matched != "修理不能":
            continue

        target_rows.append({"repair_result": rr or ""})

    print(f"=== facility_id={args.medical_facility_id}: ledger link rate, all rows ===")
    print(f"  total rows: {total:,}")
    print(f"  linked (medical_device_ledger_id is not null): {total_linked:,}")
    all_pct = (total_linked / total * 100) if total else 0.0
    print(f"  link rate: {all_pct:.1f}%")
    print()

    # 対象43件側は battery43.py の結果と一致するはず（クロスチェック用にここでも表示する）
    # Should match battery43.py's own count; printed here too as a cross-check.
    target_n = len(target_rows)
    print(f"=== target rows (バッテリー消耗 + Option B failure + 修理不能 match) ===")
    print(f"  total target rows: {target_n}")
    print(f"  (battery43.py reported: 43 rows, 0 linked, for facility_id=1)")
    print()

    # repair_result 内に岩渕/イノメ系のマーカーが含まれる行数をカウントする
    # Count how many target rows mention any vendor/technician marker in repair_result.
    vendor_hits = [r for r in target_rows if any(m in r["repair_result"] for m in _VENDOR_MARKERS)]
    print(f"=== vendor/technician concentration check (markers: {_VENDOR_MARKERS}) ===")
    print(f"  rows mentioning any marker: {len(vendor_hits)} / {target_n}")
    print(f"  rows NOT mentioning any marker: {target_n - len(vendor_hits)} / {target_n}")
    print()

    print("=== rows NOT mentioning any vendor marker (for manual reading) ===")
    for r in target_rows:
        if not any(m in r["repair_result"] for m in _VENDOR_MARKERS):
            print(f"  repair_result: {r['repair_result']!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())