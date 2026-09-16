"""
task11 commit 1 の再分類 before/after 比較
Reclassification before/after comparison for task11 commit 1 (rule2.5 implementation).

背景 / Background:
    ADR-2026-06-16 (2026-09-15 改訂) Option B の実装確認として、パッチ適用前
    (failure_classifier_before.py = 元の rule1 に「バッテリー消耗」が入っている版) と
    パッチ適用後 (failure_classifier_after.py = rule2.5 実装版) の分類結果を、実データ
    全行に対して突き合わせる。ADR-2026-09-15 決定事項6「対象語だけが動くことを確認する」
    に対応: 変化した行が全て「バッテリー消耗」を含むこと (collateral = 0) も同時に検証する。

    Verifies ADR-2026-06-16's (2026-09-15 revision) Option B implementation by running
    both the pre-patch classifier (failure_classifier_before.py, rule1 still contains
    バッテリー消耗) and the post-patch classifier (failure_classifier_after.py, rule2.5
    implemented) over every row and diffing the results. Also checks ADR-2026-09-15
    decision 6's requirement that only the targeted word's rows move (every changed row
    must contain バッテリー消耗; any other change is a regression / collateral damage).

期待される結果 / Expected numbers (ADR-2026-06-16, Option B):
    failure → maintenance : 2,686 行
    failure → inspection  : 10 行
    failure → failure (不変、修理不能等の真の不具合語を伴う) : 49 行
    それ以外の遷移 (collateral) : 0 行

本番コードは変更しない / does NOT modify the shipped classifier:
    `failure_classifier_before.py` (パッチ前のコピー) と `failure_classifier_after.py`
    (パッチ後の版、あなたのリポジトリに適用する前の下書き) を同じディレクトリに置いて
    実行する。DBには一切書き込まない、読み取り専用の比較。

実行方法 / How to run (same venv as the other scripts):
    python task11_before_after.py --medical-facility-id 1
    python task11_before_after.py --medical-facility-id 1 --csv task11_before_after_changed_rows.csv
"""

import argparse
import csv as csv_module
from collections import Counter

from sqlalchemy import create_engine, text

from failure_classifier_after import (
    FAILURE as FAILURE_CONST,
    INSPECTION as INSPECTION_CONST,
    MAINTENANCE as MAINTENANCE_CONST,
    classify_repair_explained as classify_after,
)
from failure_classifier_before import classify_repair_explained as classify_before

# クラス定数はどちらのモジュールでも同じ文字列値なので、after 側から拝借する
# (before/after で enum 値そのものが変わることはない、変わるのは判定ロジックだけ)。
# The classification constants are identical string values in both modules (only the
# decision logic differs, not the enum values themselves), so it's safe to import them
# once from the "after" module for readability in this script.

# バッテリー消耗の有無をチェックするためだけに使う (集中度/collateral検証用)。
# _build_haystack と同じ組み立て方 (区分+ノート) を独自に再現する、他スクリプトへの
# 依存を増やさないため。
# Used only to check for バッテリー消耗's presence (for the collateral-row check below).
# Rebuilds the same category+note haystack independently, to avoid adding a dependency on
# another investigation script just for this one string check.
_BATTERY_WEAR_WORD = "バッテリー消耗"

_QUERY = text(
    """
    SELECT
        medical_device_repair_history_id,
        device_number,
        client_device_number,
        repair_category, event_note, failure_reason, work_note, repair_result
    FROM cur.medical_device_repair_history
    WHERE medical_facility_id = :fid
    """
)


def _haystack(cat, ev, fr, wn, rr) -> str:
    category = (cat or "").strip()
    note_text = " ".join(str(n) for n in (ev, fr, wn, rr) if n)
    return f"{category} {note_text}"


def main() -> int:
    p = argparse.ArgumentParser(description="task11 commit1 (rule2.5) の再分類 before/after 比較")
    p.add_argument("--medical-facility-id", type=int, required=True)
    p.add_argument("--db-url", default=None)
    p.add_argument("--csv", default=None, help="変化した行を全件CSVに書き出す場合のパス")
    args = p.parse_args()

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(_QUERY, {"fid": args.medical_facility_id}).fetchall()

    # 遷移の集計 (before_classification, after_classification) -> 件数
    # Transition tally: (before_classification, after_classification) -> count.
    transitions = Counter()
    # collateral行 (バッテリー消耗を含まないのに分類が変わった行。0件のはず)
    # Collateral rows: classification changed despite NOT containing バッテリー消耗 (should
    # be zero; any hit here is a regression, since rule2.5 only touches that one word).
    collateral_rows = []
    changed_rows_for_csv = []
    # ルール発火の変化も見る (failure→failureで不変の行が、本当にrule1のまま発火しているか)
    # Also track rule-of-record changes, to confirm the 49 unchanged-failure rows still
    # fire via rule1 (not accidentally moved to a different rule that happens to also
    # resolve to failure).
    rule_transitions = Counter()

    for row in rows:
        (
            hist_id, device_number, client_device_number,
            cat, ev, fr, wn, rr,
        ) = row

        b_cls, b_is_failure, b_rule, b_matched = classify_before(cat, ev, fr, wn, rr)
        a_cls, a_is_failure, a_rule, a_matched = classify_after(cat, ev, fr, wn, rr)

        transitions[(b_cls, a_cls)] += 1
        if b_cls == FAILURE_CONST or a_cls == FAILURE_CONST:
            rule_transitions[(b_rule, a_rule)] += 1

        if b_cls != a_cls:
            has_battery_wear = _BATTERY_WEAR_WORD in _haystack(cat, ev, fr, wn, rr)
            record = {
                "medical_device_repair_history_id": hist_id,
                "device_number": device_number,
                "client_device_number": client_device_number,
                "before_classification": b_cls,
                "before_rule": b_rule,
                "before_matched": b_matched,
                "after_classification": a_cls,
                "after_rule": a_rule,
                "after_matched": a_matched,
                "has_battery_wear_word": has_battery_wear,
            }
            changed_rows_for_csv.append(record)
            if not has_battery_wear:
                collateral_rows.append(record)

    print(f"Total facility_id={args.medical_facility_id} rows scanned: {len(rows):,}")
    print()

    print("=== transition matrix (before_classification -> after_classification) ===")
    for (b, a), n in sorted(transitions.items(), key=lambda x: -x[1]):
        marker = "" if b == a else "  <-- changed"
        print(f"  {b:>11} -> {a:<11} : {n:>7,}{marker}")
    print()

    changed_total = sum(n for (b, a), n in transitions.items() if b != a)
    fail_to_maint = transitions.get((FAILURE_CONST, MAINTENANCE_CONST), 0)
    fail_to_insp = transitions.get((FAILURE_CONST, INSPECTION_CONST), 0)
    fail_to_fail = transitions.get((FAILURE_CONST, FAILURE_CONST), 0)

    print("=== ADR-2026-06-16 (Option B) expected numbers check ===")
    print(f"  failure -> maintenance : {fail_to_maint:>7,}  (expected 2,686)")
    print(f"  failure -> inspection  : {fail_to_insp:>7,}  (expected 10)")
    print(f"  total changed rows     : {changed_total:>7,}  (expected 2,696 = 2,686 + 10)")
    print()

    print("=== collateral check (ADR-2026-09-15 決定事項6: 対象語だけが動くこと) ===")
    print(f"  changed rows NOT containing 「{_BATTERY_WEAR_WORD}」: {len(collateral_rows)}  (expected 0)")
    if collateral_rows:
        print("  !!! COLLATERAL ROWS FOUND, inspect before proceeding:")
        for r in collateral_rows[:20]:
            print(f"    {r}")
    print()

    print("=== rule-of-record transitions touching failure (for sanity reading) ===")
    for (br, ar), n in sorted(rule_transitions.items(), key=lambda x: -x[1]):
        marker = "" if br == ar else "  <-- rule changed"
        print(f"  {br:>22} -> {ar:<22} : {n:>7,}{marker}")
    print()

    if args.csv:
        fieldnames = [
            "medical_device_repair_history_id", "device_number", "client_device_number",
            "before_classification", "before_rule", "before_matched",
            "after_classification", "after_rule", "after_matched",
            "has_battery_wear_word",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(changed_rows_for_csv)
        print(f"Wrote {len(changed_rows_for_csv)} changed rows to {args.csv}")

    print()
    print(
        "NOTE: this script only covers row-level classification before/after. "
        "entry9/10/16/18 の前後比較は、これらが下流のメトリクス計算 (PoC Superset "
        "metric calculation, dataloop-poc repo) 側の定義なので、このスクリプトの対象外。"
        "そちらは既存のメトリクス計算パイプラインで別途、このスクリプトのbefore/after "
        "分類結果を入力にして再計算する必要がある。"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())