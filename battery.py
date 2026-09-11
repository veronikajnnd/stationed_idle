"""
バッテリー消耗 再分類インパクト・シミュレーション — Option A vs Option B
Battery-wear reclassification impact simulation — Option A vs Option B

本番の failure_classifier.py は一切変更しない、比較専用のスタンドアロンスクリプト。
This does NOT modify the shipped failure_classifier.py — it is a standalone
comparison script, run separately.

背景 / Background:
    ADR-2026-06-16 の 2026-09-08 改訂で「バッテリー消耗」は failure から maintenance へ
    移すことが決まったが、実装上は以下の2案がある (Miyazawa-san, 2026-09-11 review):
      Option A: ルール3 (軽微作業) を rule1 と同じように category+note (haystack) で
                判定するようにし、「バッテリー消耗」をそのキーワード一覧に追加する。
                副作用として、ルール3の他のキーワード (調整/バージョンアップ/等) も
                ノート側で拾われるようになる可能性がある。
      Option B: 「バッテリー消耗」専用の判定を rule3 と同じ優先順位の位置に追加し、
                haystack (category+note) を見る。ルール3の既存キーワード一覧・挙動は
                一切変更しない。

    Two implementation options for the 2026-09-08 ADR decision
    (バッテリー消耗 -> maintenance):
      Option A: make rule3 (maintenance) read category+note together (like rule1),
                and add "バッテリー消耗" to its keyword list. Side effect: rule3's
                other keywords (調整, バージョンアップ, etc.) may now also match via
                notes, not just category.
      Option B: add a dedicated check for "バッテリー消耗" specifically, at the same
                priority slot as rule3 (i.e. after rule2's 点検 check, before the
                unmodified rule3). rule3's existing keyword list and behavior for
                every other keyword is left untouched.

    Both variants remove "バッテリー消耗" from rule1's _FAILURE_KEYWORDS list (per the
    ADR), and are compared against the CURRENT shipped classifier (baseline, where
    バッテリー消耗 is still in rule1, added 2026-09-07) row by row, using the same
    query classification_report.py uses.

実行方法 / How to run (same environment as classification_report.py):
    python battery_wear_option_ab_simulation.py --medical-facility-id 1
"""

import argparse
import re
from collections import Counter
from typing import Optional, Tuple

from sqlalchemy import create_engine, text

from streamedix_datacuration.core.failure_classifier import (
    classify_repair_explained as classify_baseline,
    FAILURE,
    INSPECTION,
    MAINTENANCE,
    NO_FAULT,
)

_QUERY = text(
    """
    SELECT repair_category, event_note, failure_reason, work_note, repair_result
    FROM cur.medical_device_repair_history
    WHERE medical_facility_id = :fid
    """
)

# --- shared building blocks, copied from failure_classifier.py so this script has
#     no dependency on internals that might not be exported ---

_NEGATION = r"(?:なし|無し|ではない|では無い|なかった|ありません|無く)"

# rule1 keywords with "バッテリー消耗" removed, per the 2026-09-08 ADR revision.
_FAILURE_KEYWORDS_NO_BATTERY = [
    "不具合", "故障", "破損", "落下", "異常", "不良",
    "エラー", "動作不良", "修理不能", "破壊", "断線", "漏れ",
    "回収", "リコール", "recall",
]
_FAILURE_RE_NO_BATTERY = re.compile(
    r"(?:%s)(?!%s)" % ("|".join(map(re.escape, _FAILURE_KEYWORDS_NO_BATTERY)), _NEGATION),
    re.IGNORECASE,
)

_INSPECTION_KEYWORD = "点検"

# rule3's ORIGINAL keyword list, unchanged (used as-is by Option B; Option A adds
# "バッテリー消耗" to its own copy of this list).
_MAINTENANCE_KEYWORDS_BASE = [
    "交換", "メンテ", "バージョンアップ", "ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ",
    "改修", "是正", "部品", "セル交換", "改良",
    "調整", "オーバーホール", "ｵｰﾊﾞｰﾎｰﾙ",
]
_MAINTENANCE_KEYWORDS_PLUS_BATTERY = _MAINTENANCE_KEYWORDS_BASE + ["バッテリー消耗"]

_NO_FAULT_CATEGORY_KEYWORDS = ["問題なし", "問題無し", "異常なし", "異常無し", "問題無", "異常無"]
_REPAIR_KEYWORDS = ["修理"]

_CANONICAL_TOKENS = {
    "repair": FAILURE,
    "inspection": INSPECTION,
    "maintenance": MAINTENANCE,
    "no_fault": NO_FAULT,
}


def _first_match(haystack: str, keywords) -> Optional[str]:
    for kw in keywords:
        if kw in haystack:
            return kw
    return None


def _build_haystack(
    repair_category: Optional[str],
    event_note: Optional[str],
    failure_reason: Optional[str],
    work_note: Optional[str],
    repair_result: Optional[str],
) -> Tuple[str, str, str]:
    category = (repair_category or "").strip()
    note_text = " ".join(
        str(n) for n in (event_note, failure_reason, work_note, repair_result) if n
    )
    haystack = f"{category} {note_text}"
    return category, note_text, haystack


def classify_option_a(
    repair_category: Optional[str],
    event_note: Optional[str] = None,
    failure_reason: Optional[str] = None,
    work_note: Optional[str] = None,
    repair_result: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Option A: rule3 reads category+note (haystack), "バッテリー消耗" added to its
    keyword list. Same priority order as the original (rule1 -> 1.5 -> 2 -> 3 -> ...).
    """
    category, note_text, haystack = _build_haystack(
        repair_category, event_note, failure_reason, work_note, repair_result
    )

    m = _FAILURE_RE_NO_BATTERY.search(haystack)
    if m:
        return FAILURE, "rule1_failure_signal", m.group(0)

    canon = _CANONICAL_TOKENS.get(category.lower())
    if canon is not None:
        return canon, "rule1.5_canonical_token", category.lower()

    if _INSPECTION_KEYWORD in category:
        return INSPECTION, "rule2_inspection_kw", _INSPECTION_KEYWORD

    kw = _first_match(haystack, _MAINTENANCE_KEYWORDS_PLUS_BATTERY)  # <- CHANGED vs baseline: haystack, not category
    if kw:
        return MAINTENANCE, "rule3_maintenance_kw(A: haystack)", kw

    kw = _first_match(category, _NO_FAULT_CATEGORY_KEYWORDS)
    if kw:
        return NO_FAULT, "rule4_no_fault_kw", kw

    kw = _first_match(category, _REPAIR_KEYWORDS)
    if kw:
        return FAILURE, "rule4.5_repair_kw", kw

    kw = _first_match(note_text, _NO_FAULT_CATEGORY_KEYWORDS)
    if kw:
        return NO_FAULT, "rule4.8_no_fault_note", kw

    return FAILURE, "rule5_default_failure", ""


def classify_option_b(
    repair_category: Optional[str],
    event_note: Optional[str] = None,
    failure_reason: Optional[str] = None,
    work_note: Optional[str] = None,
    repair_result: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Option B: a dedicated check for "バッテリー消耗" only (haystack), slotted at the
    SAME priority position as rule3 (after rule2's 点検 check) so rule2 still wins
    for genuinely 点検-categorized rows, same as Option A. rule3 itself (its keyword
    list, category-only check) is completely unchanged.
    """
    category, note_text, haystack = _build_haystack(
        repair_category, event_note, failure_reason, work_note, repair_result
    )

    m = _FAILURE_RE_NO_BATTERY.search(haystack)
    if m:
        return FAILURE, "rule1_failure_signal", m.group(0)

    canon = _CANONICAL_TOKENS.get(category.lower())
    if canon is not None:
        return canon, "rule1.5_canonical_token", category.lower()

    if _INSPECTION_KEYWORD in category:
        return INSPECTION, "rule2_inspection_kw", _INSPECTION_KEYWORD

    # --- dedicated override, バッテリー消耗 only, haystack-based ---
    if "バッテリー消耗" in haystack:
        return MAINTENANCE, "rule2.5_battery_wear_override(B)", "バッテリー消耗"

    kw = _first_match(category, _MAINTENANCE_KEYWORDS_BASE)  # <- UNCHANGED vs baseline: category only, original list
    if kw:
        return MAINTENANCE, "rule3_maintenance_kw", kw

    kw = _first_match(category, _NO_FAULT_CATEGORY_KEYWORDS)
    if kw:
        return NO_FAULT, "rule4_no_fault_kw", kw

    kw = _first_match(category, _REPAIR_KEYWORDS)
    if kw:
        return FAILURE, "rule4.5_repair_kw", kw

    kw = _first_match(note_text, _NO_FAULT_CATEGORY_KEYWORDS)
    if kw:
        return NO_FAULT, "rule4.8_no_fault_note", kw

    return FAILURE, "rule5_default_failure", ""


def main() -> int:
    p = argparse.ArgumentParser(description="バッテリー消耗 Option A/B インパクト比較")
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

    total = len(rows)
    battery_rows_total = 0

    transitions_a: Counter = Counter()
    transitions_b: Counter = Counter()
    battery_driven_a = 0
    battery_driven_b = 0
    collateral_a: Counter = Counter()  # keyword -> count, rows that changed under A but NOT via バッテリー消耗

    for (cat, ev, fr, wn, rr) in rows:
        _, _, haystack = _build_haystack(cat, ev, fr, wn, rr)
        is_battery_row = "バッテリー消耗" in haystack
        if is_battery_row:
            battery_rows_total += 1

        base_cls, _isf, _rule, _matched = classify_baseline(cat, ev, fr, wn, rr)
        a_cls, _a_rule, a_matched = classify_option_a(cat, ev, fr, wn, rr)
        b_cls, _b_rule, _b_matched = classify_option_b(cat, ev, fr, wn, rr)

        if a_cls != base_cls:
            transitions_a[(base_cls, a_cls)] += 1
            if is_battery_row:
                battery_driven_a += 1
            else:
                collateral_a[a_matched] += 1

        if b_cls != base_cls:
            transitions_b[(base_cls, b_cls)] += 1
            if is_battery_row:
                battery_driven_b += 1
            # Option B is structurally incapable of a collateral change: its rule3
            # keyword list/scope is untouched from baseline, and the dedicated
            # override only matches on "バッテリー消耗" itself.

    print(f"Total facility_id={args.medical_facility_id} rows: {total:,}")
    print(f"Rows whose category+notes contain 'バッテリー消耗': {battery_rows_total:,}")
    print()
    print("=== Option A: rule3 reads category+notes, keyword added ===")
    print(f"  Rows changed, driven by バッテリー消耗 itself: {battery_driven_a:,}")
    print(f"  Rows changed, collateral (other rule3 keywords now matching via notes): {sum(collateral_a.values()):,}")
    for kw, n in collateral_a.most_common():
        print(f"    {kw}: {n:,}")
    print(f"  All (before -> after) transitions: {dict(transitions_a)}")
    print()
    print("=== Option B: dedicated バッテリー消耗-only override ===")
    print(f"  Rows changed, driven by バッテリー消耗 itself: {battery_driven_b:,}")
    print(f"  Rows changed, collateral: 0 (by construction — rule3 untouched)")
    print(f"  All (before -> after) transitions: {dict(transitions_b)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())