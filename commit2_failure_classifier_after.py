"""
故障実績の分類モジュール (ADR-2026-06-16: 故障実績の分類ポリシー)

修理レコードの区分 (repair_category = 取込元 kbnrepair 由来) とノート
(event_note / failure_reason / work_note / repair_result) から、
repair_classification を **決定論的** に算出する。

正本はこのルール (監査可能・再現可能)。精度はパイプライン外の定期 QA で継続改善し、
見つかったパターンを本モジュールのキーワード/施設別オーバーライドに還元する方針
(ADR-2026-06-16「分類の継続改善」)。

2026-09-07 改訂 (ADR-2026-06-16 の 2026-08-25 追記に追従): 「回収/リコール対応」を
ルール3 (軽微作業) からルール1 (不具合シグナル) へ移動。メーカー起因で機器が使えない
という点で実質的に故障と同じ、と ADR で判断されたため (是正作業単体は maintenance のまま
変更なし)。旧ルールのままだった既存の repair_classification/is_failure は本改訂の
再分類実行 (Step 6) で追いつく。

2026-09-15 改訂 (ADR-2026-06-16 の 2026-09-15 改訂 = Option B に追従): 「バッテリー消耗」を
ルール1 (不具合シグナル) からルール2.5 (個別例外、新設) へ移動。ルール1は「区分+ノートに
不具合シグナルがあれば無条件で failure」という強いルールのため、電池交換だけで完了した
軽微な作業まで一律 failure に寄せてしまっていた (DATALOOP task10 の実データ検証で発覚、
task11 で対応)。ルール2.5 はルール2 (点検) の後・ルール3 (軽微作業) の前に位置し、
キーワードを1語ずつ登録する。「バッテリー消耗」以外の真の不具合語 (修理不能/破損/異常/
故障/不具合 等、ルール1に残存) を伴う行は従来通りルール1が先に拾い、failure のまま
変わらない (ADR-2026-06-16「境界 — 修理不能」)。

2026-09-17 改訂 (task11 commit2、ADR-2026-06-16 決定事項7): キーワード辞書 (ルール1の
不具合シグナル語・否定パターン、ルール1.5の正規トークン、ルール2の点検語、ルール2.5の
例外、ルール3の軽微作業語、ルール4の不問語、ルール4.5の修理語) を、このファイル内の
モジュール定数からロードするのではなく、config/failure_classification/keywords.yaml から
keyword_dictionary.load_dictionary() 経由でロードする形に変更。コードを触らずキーワードを
追加/変更できるようにするのが目的 (rule2.5 の例外追加を除く。あちらは今まで通り ADR の
決定事項6の手順で YAML の exceptions セクションに1行追加する)。

これに伴い、マッチング前に区分+ノートの本文と辞書側の全キーワードの両方に Unicode NFKC
正規化を適用するようになった (keyword_dictionary.normalize())。半角カナ表記
(例: ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ) は全角 (バージョンアップ) に正規化されて一致するため、旧実装で
辞書に別エントリとして重複登録していた幅違いのキーワードは YAML から削除した。
分類結果 (repair_classification) 自体への影響はゼロのはず (このリファクタ単体では
新しい語も削除された語もない、幅正規化で「一致するようになった」行が増える方向にしか
動かない) だが、これは task11 commit2 の before/after 再分類比較で別途証明する。
rule0 (施設別オーバーライド) の区分マッチングは今回意図的に非正規化のまま据え置いた
(facility_overrides はこの YAML 化の対象外であり、別テーブル由来のため、今回のADR決定の
範囲外の挙動変更を避けるため)。

分類 (英語 enum / 日本語ラベル):
    failure     / 故障         … 要修理の不具合。2026-08-25 改訂で「回収/リコール対応」も
                                  含む (★故障率の母数はこれのみ)
    inspection  / 点検         … 定期・日常等の点検行為
    maintenance / 軽微作業     … 異常を伴わない作業・対応 (交換・メンテ・改修・是正・
                                  バッテリー消耗による電池交換 等。「回収」は 2026-08-25
                                  改訂で failure へ移動、ここには含めない)
    no_fault    / 不問         … 調査したが問題・異常なし (No Fault Found)

判定フロー (優先順。上が優先):
    0. 施設別オーバーライド表に区分が完全一致 (非正規化の区分文字列で照合) → その分類
    1. 不具合シグナル (区分 or ノートに「不具合/故障/破損/落下/異常/不良/エラー/修理不能/
       回収/リコール/recall」 等。ただし「異常なし」「故障ではない」 等の否定が直後に
       続くものは除外) → failure
    1.5. 取込が出力する正規トークン (英語) の完全一致 → 対応する分類
    2. 区分に「点検」 を含む → inspection
    2.5. 個別例外キーワード (区分 or ノートに登録済みの語。現在は「バッテリー消耗」のみ) →
         登録された分類 (現在は maintenance)
    3. 区分に軽微作業キーワード (交換/メンテ/バージョンアップ/改修/是正/部品 等) → maintenance
    4. 区分が「問題なし」「異常なし」 系 → no_fault
    4.5. 区分に明示的な修理語 (「修理」等) → failure (positive)
    4.8. 区分に手がかり無 + ノートに「異常なし/問題なし」 → no_fault
    5. 既定 (判断不能。「修理」「メーカー依頼」 ブランク等) → failure  (過小報告を避ける safe default)

施設差: 区分の語彙は施設ごとに別物 (TJ 2026-06-16_010)。基本はキーワードルールで施設横断に
効くが、誤判定する施設固有語彙は facility_overrides ({区分文字列: classification}) で上書きする。
"""

from typing import Dict, Optional, Tuple

from streamedix_datacuration.core.keyword_dictionary import load_dictionary, normalize

# 分類 enum 値
FAILURE = "failure"
INSPECTION = "inspection"
MAINTENANCE = "maintenance"
NO_FAULT = "no_fault"

VALID_CLASSIFICATIONS = (FAILURE, INSPECTION, MAINTENANCE, NO_FAULT)

# 日本語ラベル (レポート/UI 表示用)
LABELS_JA: Dict[str, str] = {
    FAILURE: "故障",
    INSPECTION: "点検",
    MAINTENANCE: "軽微作業",
    NO_FAULT: "不問",
}

# キーワード辞書のロード (起動時に1回、検証込み)。config/failure_classification/keywords.yaml
# が壊れている場合はここで DictionaryValidationError が送出され、import 時点で fail-fast する
# (壊れた辞書のまま分類が動き続けることを防ぐ)。
#
# Load the keyword dictionary once at import time (validated). A malformed
# config/failure_classification/keywords.yaml raises DictionaryValidationError right here,
# so the module fails to import rather than classifying rows against a broken dictionary.
_DICT = load_dictionary()


def classify_repair(
    repair_category: Optional[str],
    event_note: Optional[str] = None,
    failure_reason: Optional[str] = None,
    work_note: Optional[str] = None,
    repair_result: Optional[str] = None,
    facility_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[str, bool]:
    """
    修理レコードを分類する。

    Args:
        repair_category: 区分 (取込元 kbnrepair 由来)。None/空も可。
        event_note / failure_reason / work_note / repair_result: ノート (free-text)。
        facility_overrides: 施設別オーバーライド ({区分文字列: classification})。任意。

    Returns:
        (classification, is_failure)
        classification は VALID_CLASSIFICATIONS のいずれか、is_failure は classification == failure。
    """
    classification, _rule, _matched = _classify_with_rule(
        repair_category, event_note, failure_reason, work_note, repair_result, facility_overrides
    )
    return classification, classification == FAILURE


# 発火ルールのラベル (QA 振分レポート用。どのルールで分類が決まったかを可視化)
RULE_FACILITY_OVERRIDE = "rule0_facility_override"
RULE_FAILURE_SIGNAL = "rule1_failure_signal"
RULE_CANONICAL_TOKEN = "rule1.5_canonical_token"
RULE_INSPECTION_KW = "rule2_inspection_kw"
RULE_EXCEPTION_KW = "rule2.5_exception_kw"  # 個別例外キーワード (登録された分類を返す)
RULE_MAINTENANCE_KW = "rule3_maintenance_kw"
RULE_NO_FAULT_KW = "rule4_no_fault_kw"
RULE_REPAIR_KW = "rule4.5_repair_kw"  # 区分に明示的な修理語 → 故障 (positive)
RULE_NO_FAULT_NOTE = "rule4.8_no_fault_note"  # 区分に手がかり無 + ノートに「異常なし」 → 不問
RULE_DEFAULT_FAILURE = "rule5_default_failure"  # ★分類不能で既定 failure (要レビュー)


def _first_match(text: str, keywords) -> Optional[str]:
    """text に含まれる最初のキーワードを返す (どの語で判定されたかの可視化用)。"""
    for kw in keywords:
        if kw in text:
            return kw
    return None


def _classify_with_rule(
    repair_category: Optional[str],
    event_note: Optional[str],
    failure_reason: Optional[str],
    work_note: Optional[str],
    repair_result: Optional[str],
    facility_overrides: Optional[Dict[str, str]],
) -> Tuple[str, str, str]:
    """分類・発火ルール・マッチした語を返す内部実装。

    Returns:
        (classification, rule, matched_token)
        matched_token = 判定の決め手になった語/正規表現マッチ (rule5 は "")。
        NFKC正規化後の文字列で照合するため、入力が半角カナ等だった場合 matched_token は
        正規化後 (全角) の表記で返る点に注意 (QA レポートの表示上の違いであり、
        classification 自体には影響しない)。
    """
    category = (repair_category or "").strip()

    # ルール0: 施設別オーバーライド (区分の完全一致)。
    # facility_overrides は別テーブル由来で、このYAML化 (task11 commit2) の対象外のため、
    # 意図的に非正規化の category で照合する (今回のADR決定の範囲外の挙動変更を避ける)。
    if facility_overrides:
        override = facility_overrides.get(category)
        if override in VALID_CLASSIFICATIONS:
            return override, RULE_FACILITY_OVERRIDE, category

    note_text = " ".join(
        str(n) for n in (event_note, failure_reason, work_note, repair_result) if n
    )

    # ここから先はキーワード/正規表現マッチングのみなので NFKC 正規化した文字列で照合する。
    # 辞書側のキーワードも keyword_dictionary.py のロード時に同じ normalize() を通っている
    # ため、双方とも正規化済みで一致する (例: 半角カナ ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ の行でも、辞書の全角
    # バージョンアップ 1エントリだけで拾える)。
    norm_category = normalize(category)
    norm_note_text = normalize(note_text)
    norm_haystack = f"{norm_category} {norm_note_text}"

    # ルール1: 不具合シグナル (否定除外) → 故障
    m = _DICT.failure_regex.search(norm_haystack)
    if m:
        return FAILURE, RULE_FAILURE_SIGNAL, m.group(0)

    # ルール1.5: 取込が出力する正規トークン (英語) の完全一致
    canon = _DICT.canonical_tokens.get(norm_category.lower())
    if canon is not None:
        return canon, RULE_CANONICAL_TOKEN, norm_category.lower()

    # ルール2: 「点検」 を含む → 点検
    if _DICT.inspection_keyword in norm_category:
        return INSPECTION, RULE_INSPECTION_KW, _DICT.inspection_keyword

    # ルール2.5: 個別例外キーワード (区分+ノート) → 登録された分類
    # rule1 と同じ haystack (区分+ノート) を見る。ここに来る時点で rule1 の
    # 不具合シグナル語には一致していない (=真の不具合語を伴わない) ことが保証されている。
    # Rule2.5: word-level exceptions (category+note). By the time we reach here, rule1's
    # failure-signal keywords already did not match, so no genuine failure word co-occurs.
    for exc in _DICT.exceptions:
        if exc.word in norm_haystack:
            return exc.classification, RULE_EXCEPTION_KW, exc.word

    # ルール3: 軽微作業キーワード (区分) → 軽微作業
    kw = _first_match(norm_category, _DICT.maintenance_keywords)
    if kw:
        return MAINTENANCE, RULE_MAINTENANCE_KW, kw

    # ルール4: 問題なし系 (区分) → 不問
    kw = _first_match(norm_category, _DICT.no_fault_category_keywords)
    if kw:
        return NO_FAULT, RULE_NO_FAULT_KW, kw

    # ルール4.5: 区分に明示的な修理語 → 故障 (positive)
    kw = _first_match(norm_category, _DICT.repair_keywords)
    if kw:
        return FAILURE, RULE_REPAIR_KW, kw

    # ルール4.8: 区分に手がかり無 + ノートに「異常なし/問題なし」 → 不問
    #   (rule1 で否定付き不具合語は既に除外済。区分が空/ー 等で判定不能な行の最後の意味判定)
    kw = _first_match(norm_note_text, _DICT.no_fault_category_keywords)
    if kw:
        return NO_FAULT, RULE_NO_FAULT_NOTE, kw

    # ルール5: 既定 (判断不能) → 故障 (safe default)
    return FAILURE, RULE_DEFAULT_FAILURE, ""


def classify_repair_explained(
    repair_category: Optional[str],
    event_note: Optional[str] = None,
    failure_reason: Optional[str] = None,
    work_note: Optional[str] = None,
    repair_result: Optional[str] = None,
    facility_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[str, bool, str, str]:
    """classify_repair と同じ判定 + 発火ルール + マッチした語を返す (QA 振分レポート用)。

    Returns:
        (classification, is_failure, rule, matched_token)
    """
    classification, rule, matched = _classify_with_rule(
        repair_category, event_note, failure_reason, work_note, repair_result, facility_overrides
    )
    return classification, classification == FAILURE, rule, matched