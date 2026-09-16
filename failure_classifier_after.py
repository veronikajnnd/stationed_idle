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

分類 (英語 enum / 日本語ラベル):
    failure     / 故障         … 要修理の不具合。2026-08-25 改訂で「回収/リコール対応」も
                                  含む (★故障率の母数はこれのみ)
    inspection  / 点検         … 定期・日常等の点検行為
    maintenance / 軽微作業     … 異常を伴わない作業・対応 (交換・メンテ・改修・是正・
                                  バッテリー消耗による電池交換 等。「回収」は 2026-08-25
                                  改訂で failure へ移動、ここには含めない)
    no_fault    / 不問         … 調査したが問題・異常なし (No Fault Found)

判定フロー (優先順。上が優先):
    0. 施設別オーバーライド表に区分が完全一致 → その分類
    1. 不具合シグナル (区分 or ノートに「不具合/故障/破損/落下/異常/不良/エラー/修理不能/
       回収/リコール/recall」 等。ただし「異常なし」「故障ではない」 等の否定が直後に
       続くものは除外) → failure
    2. 区分に「点検」 を含む → inspection
    2.5. 個別例外キーワード (区分 or ノートに登録済みの語。現在は「バッテリー消耗」のみ) →
         登録された分類 (現在は maintenance)
    3. 区分に軽微作業キーワード (交換/メンテ/バージョンアップ/改修/是正/部品 等) → maintenance
    4. 区分が「問題なし」「異常なし」 系 → no_fault
    5. 既定 (判断不能。「修理」「メーカー依頼」 ブランク等) → failure  (過小報告を避ける safe default)

施設差: 区分の語彙は施設ごとに別物 (TJ 2026-06-16_010)。基本はキーワードルールで施設横断に
効くが、誤判定する施設固有語彙は facility_overrides ({区分文字列: classification}) で上書きする。
"""

import re
from typing import Dict, Optional, Tuple

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

# --- ルール1: 不具合シグナル ---
# 2026-09-07: 「回収」「リコール」「recall」を追加 (ADR-2026-06-16 2026-08-25 改訂対応)。
# メーカー起因で機器が使えないという点で実質的に故障と同じ、との判断。旧ルール3
# (軽微作業) から移動、rule3側の "回収" は削除済み (下記 _MAINTENANCE_KEYWORDS 参照)。
# 2026-09-15: 「バッテリー消耗」を削除、ルール2.5 (下記 _RULE2_5_EXCEPTIONS) へ移動。
# 2026-09-07 時点ではここに追加していたが、rule1 は「区分+ノートに不具合シグナルがあれば
# 無条件で failure」という強いルールのため、電池交換だけで完了した軽微な作業まで一律
# failure に寄せてしまっていた。詳細はこのファイル冒頭の改訂履歴、および
# ADR-2026-06-16 (2026-09-15 改訂) を参照。
_FAILURE_KEYWORDS = [
    "不具合", "故障", "破損", "落下", "異常", "不良",
    "エラー", "動作不良", "修理不能", "破壊", "断線", "漏れ",
    "回収", "リコール", "recall",
]
# 否定表現 (キーワード直後に続く場合は不具合シグナルとみなさない)
_NEGATION = r"(?:なし|無し|ではない|では無い|なかった|ありません|無く)"
# 不具合キーワードの直後に否定が来ない = 真の不具合シグナル
# 2026-09-07: re.IGNORECASE を追加。"recall" は英語表記のため "Recall"/"RECALL" 等の
# 大文字表記も拾う必要がある (他のキーワードは全て日本語のため影響なし)。
_FAILURE_RE = re.compile(
    r"(?:%s)(?!%s)" % ("|".join(map(re.escape, _FAILURE_KEYWORDS)), _NEGATION),
    re.IGNORECASE,
)

# --- ルール2: 点検 ---
_INSPECTION_KEYWORD = "点検"

# --- ルール2.5: 個別例外 (キーワード単位で category+note 判定を上書き) ---
# 新設 (2026-09-15、ADR-2026-06-16 2026-09-15 改訂 = Option B)。位置はルール2 (点検) の
# 後、ルール3 (軽微作業) の前。ルール1と同じく category+note (haystack) を見るが、
# キーワードは1語ずつ登録する。新しい語を追加する際は ADR-2026-06-16 の境界表に1行追加
# した上で、対象語の行だけが動くことを再分類の前後比較で確認する運用
# (ADR-2026-09-15 決定事項6)。辞書の保管場所は当面ここ (コード定数)。YAML化は別コミット
# (ADR-2026-09-15 決定事項7、共通辞書全体のYAML化とセットで実施)。
#
# 「バッテリー消耗」: 2026-09-07 にルール1へ追加していたが、ルール1の「区分+ノートに
# 不具合シグナルがあれば無条件で failure」という判定は、電池交換だけで完了した軽微な
# 作業まで一律 failure に寄せてしまっていた (task10 の実データ検証で発覚)。ここへ移動する
# ことで、「バッテリー消耗」以外の真の不具合語 (修理不能/破損/異常/故障/不具合 等、
# ルール1に残存) を伴う行は従来通りルール1が先に拾い、failure のまま変わらない
# (ADR-2026-06-16「境界 — 修理不能」、49行は failure のまま、との決定に対応)。
#
# Rule2.5: word-level exceptions overriding category+note classification. New in
# 2026-09-15 (ADR-2026-06-16's 2026-09-15 revision, Option B). Positioned after rule2
# (inspection), before rule3 (maintenance). Reads category+notes (haystack) like rule1,
# but keywords are registered one at a time; adding a new one requires a new line in the
# ADR's boundary table plus a before/after reclassification proving only that word's rows
# moved (ADR-2026-09-15 decision 6). Stored as a code constant for now; moving it to YAML
# is a separate commit (ADR-2026-09-15 decision 7, bundled with YAML-ifying the whole
# common dictionary).
#
# "バッテリー消耗" (battery wear): added to rule1 on 2026-09-07, but rule1's blanket
# "any failure signal in category+note -> failure" caught rows where the battery was simply
# swapped as routine maintenance, over-counting them as failures (found via task10's real
# data check). Moving it here means rows that ALSO carry a genuine failure word (修理不能/
# 破損/異常/故障/不具合, still in rule1) keep classifying as failure exactly as before
# (matches ADR-2026-06-16's "境界 — 修理不能" decision that all 49 such rows stay failure).
_RULE2_5_EXCEPTIONS = [
    {
        "word": "バッテリー消耗",
        "classification": MAINTENANCE,
        "added": "2026-09-15",
        "reason": "ADR-2026-06-16 2026-09-15 改訂 (Option B)",
    },
]

# --- ルール3: 軽微作業 (メンテナンス) ---
# 調整 / オーバーホール は「対応手段であって原因ではない」(TJ 2026-06-17_004)。
# 既定 (ルール5=failure) のままだと予防的なオーバーホール・調整まで故障に数えてしまうため
# ここに含めて非故障とし、真に不具合を伴うものはルール1 (ノートの不具合シグナル) が先に拾う。
# 2026-09-07: 「回収」をここから削除 (ADR-2026-06-16 2026-08-25 改訂対応)。是正作業単体
# (リコールの記述を伴わないもの) は機器そのものに問題があったとは限らないため maintenance
# のまま変更なし。「回収」「リコール対応の是正作業」等はルール1 (_FAILURE_KEYWORDS) が
# 先に拾って failure になる。
_MAINTENANCE_KEYWORDS = [
    "交換", "メンテ", "バージョンアップ", "ﾊﾞｰｼﾞｮﾝｱｯﾌﾟ",
    "改修", "是正", "部品", "セル交換", "改良",
    "調整", "オーバーホール", "ｵｰﾊﾞｰﾎｰﾙ",
]

# --- ルール4: 不問 (No Fault Found) ---
_NO_FAULT_CATEGORY_KEYWORDS = ["問題なし", "問題無し", "異常なし", "異常無し", "問題無", "異常無"]

# --- ルール4.5: 明示的な修理語 → 故障 (positive) ---
# メーカー修理 / CE修理 / 修理 等を positive に故障判定し、安全既定(rule5)を
# 「真に分類不能な区分(空/ー/未知の新語彙)」 だけに絞る。要レビュー精度を上げるため。
_REPAIR_KEYWORDS = ["修理"]

# 取込マッピングが出力する正規トークン (英語) の直接対応。
# 例: 順天堂 05_jundoido.yaml は repair_category を if_(repair_flag,'repair',
#     if_(inspection_flag,'inspection',null)) で算出するため 'repair'/'inspection' が入る。
# kbnrepair を直接マップする施設は日本語 (日常点検 等) → 後段のキーワードルールで処理。
_CANONICAL_TOKENS = {
    "repair": FAILURE,
    "inspection": INSPECTION,
    "maintenance": MAINTENANCE,
    "no_fault": NO_FAULT,
}


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
    """
    category = (repair_category or "").strip()

    # ルール0: 施設別オーバーライド (区分の完全一致)
    if facility_overrides:
        override = facility_overrides.get(category)
        if override in VALID_CLASSIFICATIONS:
            return override, RULE_FACILITY_OVERRIDE, category

    note_text = " ".join(
        str(n) for n in (event_note, failure_reason, work_note, repair_result) if n
    )
    haystack = f"{category} {note_text}"

    # ルール1: 不具合シグナル (否定除外) → 故障
    m = _FAILURE_RE.search(haystack)
    if m:
        return FAILURE, RULE_FAILURE_SIGNAL, m.group(0)

    # ルール1.5: 取込が出力する正規トークン (英語) の完全一致
    canon = _CANONICAL_TOKENS.get(category.lower())
    if canon is not None:
        return canon, RULE_CANONICAL_TOKEN, category.lower()

    # ルール2: 「点検」 を含む → 点検
    if _INSPECTION_KEYWORD in category:
        return INSPECTION, RULE_INSPECTION_KW, _INSPECTION_KEYWORD

    # ルール2.5: 個別例外キーワード (区分+ノート) → 登録された分類
    # rule1 と同じ haystack (区分+ノート) を見る。ここに来る時点で rule1 の
    # _FAILURE_KEYWORDS には一致していない (=真の不具合語を伴わない) ことが保証されている。
    # Rule2.5: word-level exceptions (category+note). By the time we reach here, rule1's
    # _FAILURE_KEYWORDS already did not match, so no genuine failure word co-occurs.
    for exc in _RULE2_5_EXCEPTIONS:
        if exc["word"] in haystack:
            return exc["classification"], RULE_EXCEPTION_KW, exc["word"]

    # ルール3: 軽微作業キーワード (区分) → 軽微作業
    kw = _first_match(category, _MAINTENANCE_KEYWORDS)
    if kw:
        return MAINTENANCE, RULE_MAINTENANCE_KW, kw

    # ルール4: 問題なし系 (区分) → 不問
    kw = _first_match(category, _NO_FAULT_CATEGORY_KEYWORDS)
    if kw:
        return NO_FAULT, RULE_NO_FAULT_KW, kw

    # ルール4.5: 区分に明示的な修理語 → 故障 (positive)
    kw = _first_match(category, _REPAIR_KEYWORDS)
    if kw:
        return FAILURE, RULE_REPAIR_KW, kw

    # ルール4.8: 区分に手がかり無 + ノートに「異常なし/問題なし」 → 不問
    #   (rule1 で否定付き不具合語は既に除外済。区分が空/ー 等で判定不能な行の最後の意味判定)
    kw = _first_match(note_text, _NO_FAULT_CATEGORY_KEYWORDS)
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