"""
task13 (= task11 繰越) commit3 の再分類 before/after 比較
セル交換を rule2.5 (個別例外) に追加したことで、動いたのが狙った行だけか (ADR-2026-06-16
2026-09-18改訂 決定1、バッテリー消耗の B 案と同じやり方) を実データで確認する。

Reclassification before/after comparison for task13 (= task11 carried over) commit3.
Confirms that adding セル交換 to rule2.5 (word-level exceptions) only moves the rows it
should (same verification shape as battery wear's Option B).

背景 / Background:
    commit3 は YAML のみの変更 (config/keywords.yaml の exceptions に1行追加。
    failure_classifier.py / keyword_dictionary.py は無変更)。期待値は「セル交換が
    区分+ノートに含まれる行だけが動き、件数は実データ調査 (TJ 2026-09-15_002) の
    26行と一致する。それ以外は1行も動かない」。

    commit3 is a pure YAML change (one line added to config/keywords.yaml's exceptions;
    failure_classifier.py / keyword_dictionary.py are untouched). Expected: only rows
    containing セル交換 in category+note move, and the count matches the 26 rows found
    in the earlier vocabulary survey (TJ 2026-09-15_002); zero rows move for any other
    reason.

本番コードは変更しない / does NOT modify the shipped classifier:
    「before」 と「after」で classify_repair_explained を直接比較するのではなく
    (_DICT がモジュールロード時に1回だけキャッシュされるため、同一プロセス内で2つの
    辞書を切り替えて使えない)、_classify_with_rule と同じロジックをこのファイル内に
    「辞書を引数で受け取る」形で複製し、2つの KeywordDictionary インスタンス
    (before=現行の config/keywords.yaml、after=セル交換追加後) に対して同じロジックを
    適用する。ロジック自体は streamedix_datacuration.core.failure_classifier の
    _classify_with_rule と同一 (コピーした時点のもの。本番コードは一切書き換えない、
    読み取り専用の比較)。

    Rather than diffing classify_repair_explained directly ("before" vs "after" can't
    coexist in one process because _DICT is cached once at module import), this file
    duplicates _classify_with_rule's logic in a form that takes the dictionary as an
    argument, and runs it against two KeywordDictionary instances (before = the current
    real config/keywords.yaml, after = with セル交換 added). The logic itself is an exact
    copy of streamedix_datacuration.core.failure_classifier._classify_with_rule at the time
    of writing. This script does not modify the shipped classifier; it's a read-only
    comparison, DB is never written to.

実行方法 / How to run (same pattern as task11_commit2_before_after.py -- run in a scratch
directory such as stationed_idle, NOT inside streamedix-datacuration, so this never touches
that repo):
    このディレクトリ構成が必要 / this directory layout is required:
        ./task13_commit3_before_after.py   (このファイル)
        ./streamedix_datacuration/__init__.py
        ./streamedix_datacuration/core/__init__.py
        ./streamedix_datacuration/core/keyword_dictionary.py   (実際の keyword_dictionary.py
                                                                  をそのままコピー)
        ./keywords_before.yaml   (現行の本番 config/keywords.yaml をそのままコピー)
        ./keywords_after.yaml    (セル交換を exceptions に追加した版。commit3 で配布した
                                   config/keywords.yaml をそのままコピー)

    python task13_commit3_before_after.py --medical-facility-id 1
    python task13_commit3_before_after.py --all-facilities
    python task13_commit3_before_after.py --all-facilities --csv cell_exchange_drift_rows.csv
"""

import argparse
import csv as csv_module
from collections import Counter
from typing import Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

from streamedix_datacuration.core.keyword_dictionary import (
    KeywordDictionary,
    load_dictionary,
    normalize,
)

_QUERY_ONE_FACILITY = text(
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

_QUERY_ALL_FACILITIES = text(
    """
    SELECT
        medical_device_repair_history_id,
        device_number,
        client_device_number,
        repair_category, event_note, failure_reason, work_note, repair_result
    FROM cur.medical_device_repair_history
    """
)

FAILURE, INSPECTION, MAINTENANCE, NO_FAULT = "failure", "inspection", "maintenance", "no_fault"
VALID_CLASSIFICATIONS = (FAILURE, INSPECTION, MAINTENANCE, NO_FAULT)

RULE_FACILITY_OVERRIDE = "rule0_facility_override"
RULE_FAILURE_SIGNAL = "rule1_failure_signal"
RULE_CANONICAL_TOKEN = "rule1.5_canonical_token"
RULE_INSPECTION_KW = "rule2_inspection_kw"
RULE_EXCEPTION_KW = "rule2.5_exception_kw"
RULE_MAINTENANCE_KW = "rule3_maintenance_kw"
RULE_NO_FAULT_KW = "rule4_no_fault_kw"
RULE_REPAIR_KW = "rule4.5_repair_kw"
RULE_NO_FAULT_NOTE = "rule4.8_no_fault_note"
RULE_DEFAULT_FAILURE = "rule5_default_failure"


def _first_match(text_: str, keywords) -> Optional[str]:
    for kw in keywords:
        if kw in text_:
            return kw
    return None


def _classify(
    d: KeywordDictionary,
    repair_category: Optional[str],
    event_note: Optional[str],
    failure_reason: Optional[str],
    work_note: Optional[str],
    repair_result: Optional[str],
    facility_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, str]:
    """streamedix_datacuration.core.failure_classifier._classify_with_rule と同一ロジック。
    辞書 (d) を引数で受け取れるようにした以外の変更は無い (本番コードの複製、読み取り専用比較用)。

    Identical logic to failure_classifier._classify_with_rule, the only change being that
    the dictionary (d) is passed in as an argument instead of read from a module-level
    cache. A straight copy for this read-only comparison script."""
    category = (repair_category or "").strip()

    if facility_overrides:
        override = facility_overrides.get(category)
        if override in VALID_CLASSIFICATIONS:
            return override, RULE_FACILITY_OVERRIDE, category

    note_text = " ".join(
        str(n) for n in (event_note, failure_reason, work_note, repair_result) if n
    )

    norm_category = normalize(category)
    norm_note_text = normalize(note_text)
    norm_haystack = f"{norm_category} {norm_note_text}"

    m = d.failure_regex.search(norm_haystack)
    if m:
        return FAILURE, RULE_FAILURE_SIGNAL, m.group(0)

    canon = d.canonical_tokens.get(norm_category.lower())
    if canon is not None:
        return canon, RULE_CANONICAL_TOKEN, norm_category.lower()

    if d.inspection_keyword in norm_category:
        return INSPECTION, RULE_INSPECTION_KW, d.inspection_keyword

    for exc in d.exceptions:
        if exc.word in norm_haystack:
            return exc.classification, RULE_EXCEPTION_KW, exc.word

    kw = _first_match(norm_category, d.maintenance_keywords)
    if kw:
        return MAINTENANCE, RULE_MAINTENANCE_KW, kw

    kw = _first_match(norm_category, d.no_fault_category_keywords)
    if kw:
        return NO_FAULT, RULE_NO_FAULT_KW, kw

    kw = _first_match(norm_category, d.repair_keywords)
    if kw:
        return FAILURE, RULE_REPAIR_KW, kw

    kw = _first_match(norm_note_text, d.no_fault_category_keywords)
    if kw:
        return NO_FAULT, RULE_NO_FAULT_NOTE, kw

    return FAILURE, RULE_DEFAULT_FAILURE, ""


class DriftKind:
    CELL_EXCHANGE_EXPLAINED = "cell_exchange_explained"  # セル交換で変わった行 = 想定内
    UNEXPLAINED = "unexplained"                            # それ以外の理由で変わった = 要調査


def _classify_drift(before_dict, after_dict, cat, ev, fr, wn, rr) -> Optional[dict]:
    """1行分の before/after 判定を行い、差分があれば内訳付きの dict を返す (無ければ None)。"""
    b_cls, b_rule, b_matched = _classify(before_dict, cat, ev, fr, wn, rr)
    a_cls, a_rule, a_matched = _classify(after_dict, cat, ev, fr, wn, rr)

    if b_cls == a_cls:
        return None

    # 想定内 = after 側が rule2.5 で、決め手の語が「セル交換」であること
    kind = (
        DriftKind.CELL_EXCHANGE_EXPLAINED
        if (a_rule == RULE_EXCEPTION_KW and a_matched == "セル交換")
        else DriftKind.UNEXPLAINED
    )

    return {
        "before_classification": b_cls,
        "before_rule": b_rule,
        "before_matched": b_matched,
        "after_classification": a_cls,
        "after_rule": a_rule,
        "after_matched": a_matched,
        "kind": kind,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="task13 commit3 (セル交換 → maintenance) の再分類 before/after 比較")
    fid_group = p.add_mutually_exclusive_group(required=True)
    fid_group.add_argument("--medical-facility-id", type=int, help="対象施設ID (1施設のみ)")
    fid_group.add_argument("--all-facilities", action="store_true", help="全施設を対象にする (推奨)")
    p.add_argument("--db-url", default=None)
    p.add_argument("--before-yaml", default="keywords_before.yaml")
    p.add_argument("--after-yaml", default="keywords_after.yaml")
    p.add_argument("--csv", default=None, help="差分があった行を全件CSVに書き出す場合のパス")
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

    engine = create_engine(db_url)
    with engine.connect() as conn:
        if args.all_facilities:
            rows = conn.execute(_QUERY_ALL_FACILITIES).fetchall()
        else:
            rows = conn.execute(_QUERY_ONE_FACILITY, {"fid": args.medical_facility_id}).fetchall()

    scope = "all facilities" if args.all_facilities else f"facility_id={args.medical_facility_id}"
    print(f"Total rows scanned ({scope}): {len(rows):,}")
    print()

    transitions: Counter = Counter()
    drift_rows: List[dict] = []
    cell_exchange_explained: List[dict] = []
    unexplained: List[dict] = []
    csv_rows: List[dict] = []

    for row in rows:
        (hist_id, device_number, client_device_number, cat, ev, fr, wn, rr) = row

        drift = _classify_drift(before_dict, after_dict, cat, ev, fr, wn, rr)
        if drift is None:
            b_cls, _, _ = _classify(before_dict, cat, ev, fr, wn, rr)
            transitions[(b_cls, b_cls)] += 1
            continue

        transitions[(drift["before_classification"], drift["after_classification"])] += 1
        record = {
            "medical_device_repair_history_id": hist_id,
            "device_number": device_number,
            "client_device_number": client_device_number,
            "repair_category": cat,
            **drift,
        }
        drift_rows.append(record)
        csv_rows.append(record)
        if drift["kind"] == DriftKind.CELL_EXCHANGE_EXPLAINED:
            cell_exchange_explained.append(record)
        else:
            unexplained.append(record)

    print("=== transition matrix (before_classification -> after_classification) ===")
    for (b, a), n in sorted(transitions.items(), key=lambda x: -x[1]):
        marker = "" if b == a else "  <-- DRIFT"
        print(f"  {b:>11} -> {a:<11} : {n:>7,}{marker}")
    print()

    print("=== task13 commit3 pass condition: only セル交換 rows move ===")
    print(f"  rows with any classification change : {len(drift_rows):,}  (expect: == セル交換の実データ件数, TJ 2026-09-15_002 では26)")
    print(f"    ...explained by セル交換 (rule2.5)      : {len(cell_exchange_explained):,}")
    print(f"    ...UNEXPLAINED (other cause)             : {len(unexplained):,}  (expected 0 -- must be root-caused before merging)")
    print()

    if cell_exchange_explained:
        print("=== セル交換-caused rows (log count + row content) ===")
        for r in cell_exchange_explained:
            print(f"  id={r['medical_device_repair_history_id']} device={r['device_number']} category={r['repair_category']!r}")
            print(f"    before: {r['before_classification']} ({r['before_rule']}, matched={r['before_matched']!r})")
            print(f"    after : {r['after_classification']} ({r['after_rule']}, matched={r['after_matched']!r})")
        print()

    if unexplained:
        print("=== !!! UNEXPLAINED DRIFT -- investigate before merging commit3 !!! ===")
        for r in unexplained:
            print(f"  id={r['medical_device_repair_history_id']} device={r['device_number']} category={r['repair_category']!r}")
            print(f"    before: {r['before_classification']} ({r['before_rule']}, matched={r['before_matched']!r})")
            print(f"    after : {r['after_classification']} ({r['after_rule']}, matched={r['after_matched']!r})")
        print()

    if not drift_rows:
        print("NOTE: 0 rows changed -- if セル交換 is genuinely present in the real data, this would be unexpected.")
        print()

    if args.csv:
        fieldnames = [
            "medical_device_repair_history_id", "device_number", "client_device_number",
            "repair_category", "kind", "before_classification", "before_rule", "before_matched",
            "after_classification", "after_rule", "after_matched",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"Wrote {len(csv_rows)} drifted rows to {args.csv}")

    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
