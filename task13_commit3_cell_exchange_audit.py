"""
task13 commit3: セル交換 を含む全行の棒卸し (Miyazawa-san 2026-10-02 レビュー対応)。
Audit of every row containing セル交換, moved or not (per Miyazawa-san's 2026-10-02 review).

背景 / Background:
    task13_commit3_before_after.py は「動いた行 (drift)」しか見ない。動いた15行は全部
    セル交換で説明できることは確認済みだが、「セル交換があるのに動かなかった行」は
    そのスクリプトには出てこない。Miyazawa-san のレビューで、26 (TJ 2026-09-15_002) と
    15 (動いた行) の差を「たぶん」で済ませず、数で確かめるよう指摘された。

    task13_commit3_before_after.py only looks at rows whose classification changed
    (drift). The 15 drifted rows are already confirmed to be explained by セル交換, but
    rows that contain セル交換 and did NOT move never appear in that script. Miyazawa-san's
    review asked for the 26 (TJ 2026-09-15_002) vs 15 (drifted) gap to be checked with
    numbers instead of "most likely".

このスクリプトがやること / What this script does:
    区分+ノート (NFKC正規化後) にセル交換を含む行を全件抽出する (動いたかどうかは問わない)。
    各行について before/after の分類・発火ルールを計算し、after_rule ごとに集計する。
    after_rule が何であれ、ADR-2026-06-16 のセル交換境界表のどの行に対応するかを
    突き合わせられるようにする:
        - rule2.5_exception_kw (matched=セル交換) → 境界表1行目 (note-only) か2行目 (区分=修理等)
        - rule1_failure_signal                   → 境界表3行目 (真の不具合語と共存 → failure 不変)
        - rule2_inspection_kw                     → バッテリー消耗と同じ構造 (区分=点検 → inspection 不変、
                                                      ADR本文には明記されていないが同形)
        - rule3_maintenance_kw (区分側に「交換」等) → 既にrule3で拾われていた行 (commit3以前から
                                                      maintenance。これが26と15の差の主要候補)
        - それ以外のrule                           → 未知のパターン。要調査。

    Extracts every row where category+note (after NFKC normalization) contains セル交換,
    regardless of whether it moved. For each, computes before/after classification and
    firing rule, then groups by after_rule so each group can be matched against a specific
    line of the ADR-2026-06-16 セル交換 boundary table:
        - rule2.5_exception_kw (matched=セル交換) -> boundary line 1 (note-only) or 2
          (category is a repair word etc.)
        - rule1_failure_signal                   -> boundary line 3 (co-occurs with a genuine
                                                      failure word -> stays failure)
        - rule2_inspection_kw                     -> same shape as battery wear (category is
                                                      点検-series -> stays inspection; not an
                                                      explicit ADR line but the same structure)
        - rule3_maintenance_kw (category itself contains 交換 etc.) -> already caught by rule3
          before commit3 existed (maintenance -> maintenance, no drift). Leading candidate for
          the 26 vs 15 gap.
        - any other rule                          -> unexpected pattern, needs investigation.

本番コードは変更しない / does not modify the shipped classifier (read-only, same as
task13_commit3_before_after.py). Reuses the exact same _classify logic -- imported from
that file rather than re-copied, so the two scripts can never drift out of sync with each
other.

実行方法 / How to run:
    同じディレクトリに task13_commit3_before_after.py と、それが必要とするファイル一式
    (streamedix_datacuration/, keywords_before.yaml, keywords_after.yaml) が必要。
    stationed_idle のような scratch ディレクトリで実行する (streamedix-datacuration の
    中では実行しない)。

    Needs task13_commit3_before_after.py and its dependencies
    (streamedix_datacuration/, keywords_before.yaml, keywords_after.yaml) in the same
    directory. Run in a scratch directory such as stationed_idle, never inside
    streamedix-datacuration.

    python task13_commit3_cell_exchange_audit.py --all-facilities
    python task13_commit3_cell_exchange_audit.py --all-facilities --csv cell_exchange_all_rows.csv
"""

import argparse
import csv as csv_module
from collections import Counter
from typing import List

from sqlalchemy import create_engine, text

from streamedix_datacuration.core.keyword_dictionary import load_dictionary, normalize
from task13_commit3_before_after import _classify, RULE_EXCEPTION_KW, RULE_MAINTENANCE_KW

_QUERY_ONE_FACILITY = text(
    """
    SELECT
        medical_device_repair_history_id,
        device_number,
        client_device_number,
        repair_category, event_note, failure_reason, work_note, repair_result
    FROM cur.medical_device_repair_history
    WHERE medical_facility_id = :fid
      AND (
            repair_category ILIKE :full OR repair_category ILIKE :half
         OR event_note       ILIKE :full OR event_note       ILIKE :half
         OR failure_reason   ILIKE :full OR failure_reason   ILIKE :half
         OR work_note        ILIKE :full OR work_note        ILIKE :half
         OR repair_result    ILIKE :full OR repair_result    ILIKE :half
      )
    """
)

_QUERY_ALL_FACILITIES = text(
    """
    SELECT
        medical_device_repair_history_id,
        device_number,
        client_device_number,
        repair_category, event_note, failure_reason, work_note, repair_result
    FROM cur.medical_device_repair_history
    WHERE (
            repair_category ILIKE :full OR repair_category ILIKE :half
         OR event_note       ILIKE :full OR event_note       ILIKE :half
         OR failure_reason   ILIKE :full OR failure_reason   ILIKE :half
         OR work_note        ILIKE :full OR work_note        ILIKE :half
         OR repair_result    ILIKE :full OR repair_result    ILIKE :half
      )
    """
)

# SQL側は粗めのフィルタ (全角/半角の両方を素朴にILIKEで拾うだけ)。
# 正確な判定 (NFKC正規化後の部分一致) はPython側でもう一度かける ("_contains_cell_exchange")。
# これはNFKCをSQLで表現しにくいための二段構え -- 粗く絞ってから正確にチェックする。
#
# The SQL filter is intentionally loose (plain ILIKE for both widths). The precise check
# (substring match after NFKC normalization) is re-applied in Python via
# _contains_cell_exchange. Two-stage because NFKC itself isn't easy to express in SQL --
# cast a wide net in SQL, then verify precisely in Python.
_FULL_WIDTH = "%セル交換%"
_HALF_WIDTH = "%ｾﾙ交換%"


def _contains_cell_exchange(cat, ev, fr, wn, rr) -> bool:
    """区分+ノートをNFKC正規化した上で、本当に「セル交換」を含むかどうかを判定する
    (分類ロジック本体と同じ正規化・結合方法。SQLの粗いILIKEフィルタの誤検出を防ぐ)。

    Checks whether category+note, once NFKC-normalized, truly contains セル交換 (same
    normalization and concatenation as the classifier itself; guards against false
    positives from the loose SQL ILIKE filter)."""
    category = (cat or "").strip()
    note_text = " ".join(str(n) for n in (ev, fr, wn, rr) if n)
    haystack = normalize(f"{category} {note_text}")
    return "セル交換" in haystack


def main() -> int:
    p = argparse.ArgumentParser(
        description="task13 commit3: セル交換を含む全行 (動いた/動かなかった問わず) の一覧と after_rule 別集計"
    )
    fid_group = p.add_mutually_exclusive_group(required=True)
    fid_group.add_argument("--medical-facility-id", type=int, help="対象施設ID (1施設のみ)")
    fid_group.add_argument("--all-facilities", action="store_true", help="全施設を対象にする (推奨)")
    p.add_argument("--db-url", default=None)
    p.add_argument("--before-yaml", default="keywords_before.yaml")
    p.add_argument("--after-yaml", default="keywords_after.yaml")
    p.add_argument("--csv", default=None, help="セル交換を含む全行をCSVに書き出す場合のパス")
    args = p.parse_args()

    load_dictionary.cache_clear()
    before_dict = load_dictionary(args.before_yaml)
    load_dictionary.cache_clear()
    after_dict = load_dictionary(args.after_yaml)
    load_dictionary.cache_clear()

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    params = {"full": _FULL_WIDTH, "half": _HALF_WIDTH}
    engine = create_engine(db_url)
    with engine.connect() as conn:
        if args.all_facilities:
            rows = conn.execute(_QUERY_ALL_FACILITIES, params).fetchall()
        else:
            rows = conn.execute(
                _QUERY_ONE_FACILITY, {**params, "fid": args.medical_facility_id}
            ).fetchall()

    scope = "all facilities" if args.all_facilities else f"facility_id={args.medical_facility_id}"
    print(f"Rows fetched by loose SQL ILIKE filter ({scope}): {len(rows):,}")

    records: List[dict] = []
    for row in rows:
        (hist_id, device_number, client_device_number, cat, ev, fr, wn, rr) = row
        if not _contains_cell_exchange(cat, ev, fr, wn, rr):
            continue  # SQLの粗いフィルタの誤検出 (NFKC正規化後は一致しない) を除外

        b_cls, b_rule, b_matched = _classify(before_dict, cat, ev, fr, wn, rr)
        a_cls, a_rule, a_matched = _classify(after_dict, cat, ev, fr, wn, rr)
        records.append({
            "medical_device_repair_history_id": hist_id,
            "device_number": device_number,
            "client_device_number": client_device_number,
            "repair_category": cat,
            "event_note": ev,
            "failure_reason": fr,
            "work_note": wn,
            "repair_result": rr,
            "before_classification": b_cls,
            "before_rule": b_rule,
            "before_matched": b_matched,
            "after_classification": a_cls,
            "after_rule": a_rule,
            "after_matched": a_matched,
            "moved": b_cls != a_cls,
        })

    print(f"Rows confirmed to contain セル交換 after NFKC normalization: {len(records):,}")
    print("(この数が TJ 2026-09-15_002 の実データ件数と一致するはず。"
          " survey_count を突き合わせたい比較対象の数として使う)")
    print()

    by_after_rule: Counter = Counter(r["after_rule"] for r in records)
    print("=== after_rule 別の件数 (ADR境界表との対応チェック用) ===")
    print("=== count by after_rule (match each group to an ADR boundary-table line) ===")
    for rule, n in sorted(by_after_rule.items(), key=lambda x: -x[1]):
        moved_n = sum(1 for r in records if r["after_rule"] == rule and r["moved"])
        unmoved_n = n - moved_n
        print(f"  {rule:<28} : {n:>4,}  (moved={moved_n:,}, unmoved={unmoved_n:,})")
    print()

    print("=== 行の内訳 (after_rule ごと) ===")
    print("=== row detail, grouped by after_rule ===")
    for rule in sorted(by_after_rule, key=lambda r: -by_after_rule[r]):
        group = [r for r in records if r["after_rule"] == rule]
        print(f"--- after_rule = {rule} ({len(group)} rows) ---")
        for r in group:
            moved_marker = "MOVED" if r["moved"] else "unmoved"
            print(
                f"  id={r['medical_device_repair_history_id']} device={r['device_number']} "
                f"category={r['repair_category']!r} [{moved_marker}]"
            )
            print(
                f"    before: {r['before_classification']} ({r['before_rule']}, matched={r['before_matched']!r})"
            )
            print(
                f"    after : {r['after_classification']} ({r['after_rule']}, matched={r['after_matched']!r})"
            )
        print()

    # rule2.5 は rule3 より優先度が高いため、セル交換を含む行の after_rule は commit3 後、
    # 必ず rule2.5 (他のもっと優先度が高いルールが先に発火しない限り) になる -- rule3 が
    # after 側に出てくることは無い。なので「rule3 がすでに拾っていた行」は before_rule
    # (commit3 前の状態) を見て判定する。26 対 15 の差の主要仮説
    # (rule3.maintenance_keywords に元々「セル交換」「交換」があるため)。
    #
    # rule2.5 outranks rule3, so after commit3 a row containing セル交換 will always show
    # after_rule = rule2.5 (unless an even higher-priority rule fires first) -- rule3 never
    # appears on the after side any more. So "rows rule3 already caught" has to be checked
    # via before_rule (the pre-commit3 state), not after_rule. Leading hypothesis for the
    # 26 vs 15 gap (rule3.maintenance_keywords already lists セル交換 and 交換).
    already_maintenance_via_rule3 = [
        r for r in records
        if r["before_rule"] == RULE_MAINTENANCE_KW
        and r["before_classification"] == "maintenance"
        and r["after_classification"] == "maintenance"
        and not r["moved"]
    ]
    print(f"=== 仮説チェック: rule3で既にmaintenanceだった行 (= 26と15の差の主要候補) ===")
    print(f"=== hypothesis check: rows already maintenance via rule3 (leading candidate for the gap) ===")
    print(f"  count: {len(already_maintenance_via_rule3):,}")
    for r in already_maintenance_via_rule3:
        print(f"  id={r['medical_device_repair_history_id']} category={r['repair_category']!r}")
    print()

    if args.csv:
        fieldnames = [
            "medical_device_repair_history_id", "device_number", "client_device_number",
            "repair_category", "event_note", "failure_reason", "work_note", "repair_result",
            "before_classification", "before_rule", "before_matched",
            "after_classification", "after_rule", "after_matched", "moved",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
        print(f"Wrote {len(records)} rows to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())