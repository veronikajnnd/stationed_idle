"""
core.failure_classifier の単体テスト (ADR-2026-06-16)

実データ (PoC 4医療機関, TJ 2026-06-16_010) で観測した区分・ノートを基にケース化。
"""

import pytest

from streamedix_datacuration.core.failure_classifier import (
    classify_repair,
    FAILURE,
    INSPECTION,
    MAINTENANCE,
    NO_FAULT,
)


@pytest.mark.parametrize(
    "category, expected",
    [
        # --- 点検 (rule2) ---
        ("日常点検", INSPECTION),
        ("定期点検", INSPECTION),
        ("メーカー点検", INSPECTION),
        ("使用中点検", INSPECTION),
        ("修理後点検", INSPECTION),
        ("納入時点検", INSPECTION),  # ADR: 点検扱い
        # --- 不具合点検は故障 (rule1 が rule2 に優先) ---
        ("不具合点検", FAILURE),
        # --- 故障 (修理系・既定) ---
        ("修理", FAILURE),
        ("メーカー修理", FAILURE),
        ("院内修理", FAILURE),
        ("メーカー依頼", FAILURE),
        ("修理不能", FAILURE),  # 不具合キーワード
        ("完了-院内修理", FAILURE),
        ("受付中-", FAILURE),
        # --- 回収/リコール → 故障 (2026-09-07: ADR-2026-06-16 の 2026-08-25 改訂対応。
        #     旧ルールでは軽微作業(rule3)扱いだったが、メーカー起因で機器が使えないため
        #     rule1(不具合シグナル)へ移動) ---
        ("回収", FAILURE),
        ("リコール対応", FAILURE),
        ("Recall", FAILURE),  # re.IGNORECASE で大文字表記 (recall は英語) も拾う
        ("リコール対応の是正作業", FAILURE),  # rule1 が rule3 に優先 (ADR 境界表: 両方
        #                                     含む場合は故障へ寄せる)
        # --- ブランク/None → 既定 = 故障 ---
        ("", FAILURE),
        (None, FAILURE),
        ("   ", FAILURE),
        # --- 軽微作業 (rule3) ---
        ("交換", MAINTENANCE),
        ("部品交換", MAINTENANCE),
        ("O2セル交換", MAINTENANCE),
        ("無償交換", MAINTENANCE),
        ("バージョンアップ", MAINTENANCE),
        ("改修", MAINTENANCE),
        # 「是正作業」単体 (リコールの記述を伴わないもの) は maintenance のまま変更なし。
        # 機器そのものに問題があったとは限らないため (ADR 境界表)。回収を伴う場合は上の
        # リコール対応の是正作業ケースの通り故障になる。
        ("是正作業", MAINTENANCE),
        ("院内メンテ(費用無)", MAINTENANCE),
        ("院内メンテ(費用有)", MAINTENANCE),
        # --- バッテリー消耗 (rule2.5、新設): 2026-09-15 改訂 (ADR-2026-06-16 Option B) で
        #     rule1 から移動。単独 (他に真の不具合語が無い) なら軽微作業 (電池交換) として
        #     maintenance。真の不具合語を伴う場合や区分に点検を含む場合の分岐は
        #     test_battery_wear_* 側の専用テストを参照。
        ("バッテリー消耗", MAINTENANCE),
        # --- 不問 (rule4) ---
        ("問題なし", NO_FAULT),
        ("異常なし", NO_FAULT),
    ],
)
def test_classify_by_category(category, expected):
    classification, is_failure = classify_repair(category)
    assert classification == expected
    assert is_failure == (expected == FAILURE)


def test_note_overrides_category_to_failure():
    # 愛知「問題なし」 だがノートに「破損」 → 故障 (rule1 がノートを見る)
    assert classify_repair("問題なし", event_note="破損")[0] == FAILURE
    # 「定期点検」 でもノートに明示的な不具合語 → 故障
    assert classify_repair("定期点検", failure_reason="故障のため")[0] == FAILURE


def test_recall_in_note_overrides_maintenance_category():
    # 区分は「是正作業」(maintenance) でも、ノートに「回収」「リコール」があれば
    # 故障 (rule1 がノートを見る)。2026-08-25 ADR 改訂対応。
    assert classify_repair("是正作業", event_note="回収対応のため")[0] == FAILURE
    assert classify_repair("是正作業", work_note="リコール対象機種")[0] == FAILURE


def test_battery_wear_alone_is_maintenance():
    """
    バッテリー消耗 (rule2.5): 2026-09-15 改訂 (ADR-2026-06-16 Option B)。単独 (他に真の
    不具合語が無い) なら軽微作業 (電池交換) として maintenance。rule1 の「不具合シグナルが
    あれば無条件で failure」という強い判定から、専用の rule2.5 へ移動した。

    Battery wear alone (rule2.5): moved out of rule1 in the 2026-09-15 revision
    (ADR-2026-06-16 Option B). With no other genuine failure word present, it now
    classifies as MAINTENANCE (a routine battery swap), instead of unconditionally
    FAILURE under the old rule1 match.
    """
    assert classify_repair(None, failure_reason="バッテリー消耗", event_note="CE修理")[0] == MAINTENANCE
    assert classify_repair(None, event_note="バッテリー消耗")[0] == MAINTENANCE
    assert classify_repair("バッテリー消耗")[0] == MAINTENANCE


def test_battery_wear_with_genuine_failure_word_stays_failure():
    """
    バッテリー消耗 + 真の不具合語 (修理不能/破損 等) が同じ行にある場合は rule1 が
    rule2.5 より先に発火し、failure のまま変わらない。ADR-2026-06-16「境界 — 修理不能」
    (49行は failure のまま、との決定) に対応するケース。

    When バッテリー消耗 co-occurs with a genuine failure word (修理不能, 破損, etc.) in
    the same row, rule1 still fires before rule2.5 and the row stays FAILURE. This matches
    ADR-2026-06-16's "境界 — 修理不能" decision that all 49 such rows remain failure.
    """
    assert classify_repair(None, failure_reason="バッテリー消耗", event_note="修理不能")[0] == FAILURE
    assert classify_repair(None, event_note="バッテリー消耗のため破損")[0] == FAILURE


def test_battery_wear_with_inspection_category_stays_inspection():
    """
    区分に「点検」があれば rule2 が rule2.5 より先に発火し、inspection になる。
    ADR-2026-06-16 の再分類比較で見つかった10行(区分は点検系、ノートにバッテリー消耗)に
    対応するケース。rule2.5 をルール2の後ろに置くことで、この10行は特別扱いせずに
    既存のルール2だけで正しく処理される。

    When the category contains 点検, rule2 fires before rule2.5, classifying as
    INSPECTION. Matches the 10 rows found in ADR-2026-06-16's reclassification comparison
    (inspection-category rows whose notes mention バッテリー消耗); placing rule2.5 after
    rule2 means these resolve correctly through the existing rule2, with no special-casing.
    """
    assert classify_repair("定期点検", event_note="バッテリー消耗")[0] == INSPECTION
    assert classify_repair("メーカー点検", failure_reason="バッテリー消耗")[0] == INSPECTION


def test_battery_wear_does_not_affect_unrelated_rows():
    """
    rule2.5 は登録された語(現在はバッテリー消耗のみ)にしか反応しない。既存の rule3/rule5
    の挙動が rule2.5 追加によって変わっていないことの回帰確認。

    Regression check: rule2.5 only fires on its registered word (currently バッテリー消耗
    alone), so unrelated rows keep their pre-existing rule3/rule5 behavior unchanged.
    """
    assert classify_repair("交換")[0] == MAINTENANCE
    assert classify_repair("修理")[0] == FAILURE
    assert classify_repair("")[0] == FAILURE


def test_negation_not_treated_as_failure():
    # 否定表現は不具合シグナルにしない
    assert classify_repair("点検", event_note="異常なし")[0] == INSPECTION
    assert classify_repair("問題なし", event_note="異常なし")[0] == NO_FAULT
    # 「故障ではない」 を含むノートだけなら故障にしない (点検が優先語として残る)
    assert classify_repair("定期点検", work_note="故障ではない")[0] == INSPECTION


def test_open_vocabulary_residual_stays_inspection():
    # ルールの限界: キーワードに無い言い回しの故障は拾えず点検のまま
    # (ADR: この残差はパイプライン外の定期QAで拾い→ルールへ還元)
    assert classify_repair("定期点検", event_note="電源が入らない")[0] == INSPECTION


def test_facility_override():
    ov = {"特殊作業": MAINTENANCE}
    assert classify_repair("特殊作業", facility_overrides=ov) == (MAINTENANCE, False)
    # オーバーライドはキーワードルールより優先
    assert classify_repair("定期点検", facility_overrides={"定期点検": FAILURE}) == (
        FAILURE,
        True,
    )


@pytest.mark.parametrize(
    "category, expected",
    [
        # 取込マッピング (順天堂 if_) が出力する英語トークン
        ("repair", FAILURE),
        ("inspection", INSPECTION),
        ("maintenance", MAINTENANCE),
        ("no_fault", NO_FAULT),
        ("REPAIR", FAILURE),  # 大小無視
    ],
)
def test_canonical_english_tokens(category, expected):
    assert classify_repair(category)[0] == expected


def test_note_failure_overrides_inspection_token():
    # repair_category='inspection' でもノートに不具合 → 故障 (rule1 が優先)
    assert classify_repair("inspection", event_note="破損")[0] == FAILURE


def test_is_failure_consistency():
    for cat in ["修理", "定期点検", "交換", "問題なし", "", "バッテリー消耗"]:
        cls, is_failure = classify_repair(cat)
        assert is_failure == (cls == FAILURE)