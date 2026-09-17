"""
task11 commit2 の再分類 before/after 比較 (共通辞書のYAML化 + NFKC正規化)
Reclassification before/after comparison for task11 commit2 (YAML-ification of the
common dictionary + NFKC normalization).

背景 / Background:
    ADR-2026-06-16 決定事項7の実装確認として、パッチ適用前 (commit2_failure_classifier_before.py =
    commit1で今実際に本番に入っている版、キーワードはコード定数のまま) とパッチ適用後
    (commit2_failure_classifier_after.py = commit2の下書き、keyword_dictionary.py 経由でYAMLから
    ロード + NFKC正規化) の分類結果を、実データ全行に対して突き合わせる。

    ファイル名に commit2_ を付けているのは、task11 commit1 の investigation で使った
    failure_classifier_before.py / failure_classifier_after.py と同じ stationed_idle
    ディレクトリに置いても名前が衝突しないようにするため。

    このコミットは「rule2.5にセル交換を追加する」ような分類ルールの変更ではなく、辞書の
    保管場所を変える純粋なリファクタなので、期待値は「分類結果が1行も変わらないこと」。
    ただし NFKC正規化はキーワードとの幅違い一致 (半角カナ等) を吸収する副作用があるため、
    それだけが原因で変わる行が出る可能性はゼロではない。出た場合は
    (a) NFKC正規化で区分+ノートの文字列自体が変化した行 (=想定内、件数と内容をログに残す)
    (b) 正規化しても文字列が変化していないのに結果が変わった行 (=想定外、辞書の中身が
        ずれているバグの可能性が高く、コミット前に必ず原因を特定する)
    の2種類に分けて報告する。

    Verifies task11 commit2 (ADR-2026-06-16 decision 7) by running both the pre-patch
    classifier (commit2_failure_classifier_before.py = what commit1 actually shipped,
    keywords still as code constants) and the post-patch classifier
    (commit2_failure_classifier_after.py = the commit2 draft, loading from YAML via
    keyword_dictionary.py with NFKC normalization) over every row and diffing the results.

    The commit2_ filename prefix avoids colliding with commit1's investigation files
    (failure_classifier_before.py / failure_classifier_after.py) if both live in the same
    stationed_idle directory.

    This commit is a pure refactor of WHERE the dictionary lives, not a rule change (unlike
    セル交換, which will be its own commit3), so the expectation is zero rows change. NFKC
    normalization does have the side effect of absorbing width-variant matches (e.g.
    half-width katakana), so a non-zero diff isn't impossible; any row that differs is
    split into (a) rows where NFKC normalization actually changed the category+note text
    (expected, log the count and the row content) vs (b) rows where the text was unchanged
    by normalization but the result still differs (unexpected — almost certainly a
    dictionary-content bug, must be root-caused before this commit ships).

期待される結果 / Expected result:
    全行 before_classification == after_classification (完全一致)。
    Every row: before_classification == after_classification (exact match).

本番コードは変更しない / does NOT modify the shipped classifier:
    `commit2_failure_classifier_before.py` (現行の commit1 版そのまま) と
    `commit2_failure_classifier_after.py` (commit2 の下書き) を、それぞれの依存関係
    (after 側は streamedix_datacuration.core.keyword_dictionary +
    config/keywords.yaml) ごと同じ作業ディレクトリに置いて実行する。
    DBには一切書き込まない、読み取り専用の比較。

実行方法 / How to run (same venv/layout pattern as task11_before_after.py — run in a
scratch directory, NOT inside streamedix-datacuration, so this never touches that repo):
    このディレクトリ構成が必要 / this directory layout is required:
        ./commit2_failure_classifier_before.py
        ./commit2_failure_classifier_after.py
        ./streamedix_datacuration/__init__.py
        ./streamedix_datacuration/core/__init__.py
        ./streamedix_datacuration/core/keyword_dictionary.py
        ./config/keywords.yaml
        ./task11_commit2_before_after.py   (このファイル)

    python task11_commit2_before_after.py --medical-facility-id 1
    python task11_commit2_before_after.py --all-facilities
    python task11_commit2_before_after.py --all-facilities --csv drift_rows.csv
"""

import argparse
import csv as csv_module
from collections import Counter
from typing import List, Optional, Tuple

from sqlalchemy import create_engine, text

from commit2_failure_classifier_after import classify_repair_explained as classify_after
from commit2_failure_classifier_before import classify_repair_explained as classify_before
from streamedix_datacuration.core.keyword_dictionary import normalize

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


def _raw_haystack(cat, ev, fr, wn, rr) -> str:
    """classify_repair_explained に渡す前の、正規化していない区分+ノートの文字列。
    どちらの版 (before/after) も内部でこれと同じ組み立て方をするので、ここでの再現は
    「NFKC正規化がこの行の文字列を実際に変えたか」を判定する目的専用 (分類には使わない)。

    The raw (non-normalized) category+note string, built the same way both versions build
    it internally. Only used here to check whether NFKC normalization actually changes this
    row's text (not used for classification itself — both modules already do that)."""
    category = (cat or "").strip()
    note_text = " ".join(str(n) for n in (ev, fr, wn, rr) if n)
    return f"{category} {note_text}"


class DriftKind:
    NFKC_EXPLAINED = "nfkc_explained"      # 正規化で文字列が変わった行 = 想定内
    UNEXPLAINED = "unexplained"            # 正規化しても文字列が同じなのに結果が変わった = 要調査


def _classify_drift(cat, ev, fr, wn, rr) -> Optional[dict]:
    """1行分の before/after 判定を行い、差分があれば内訳付きの dict を返す (無ければ None)。

    Classifies one row through both before/after and, if the result differs, returns a dict
    describing the drift (None if the row is unchanged)."""
    b_cls, _b_isf, b_rule, b_matched = classify_before(cat, ev, fr, wn, rr)
    a_cls, _a_isf, a_rule, a_matched = classify_after(cat, ev, fr, wn, rr)

    if b_cls == a_cls:
        return None

    raw = _raw_haystack(cat, ev, fr, wn, rr)
    normalized = normalize(raw)
    kind = DriftKind.NFKC_EXPLAINED if normalized != raw else DriftKind.UNEXPLAINED

    return {
        "before_classification": b_cls,
        "before_rule": b_rule,
        "before_matched": b_matched,
        "after_classification": a_cls,
        "after_rule": a_rule,
        "after_matched": a_matched,
        "kind": kind,
        "raw_haystack": raw,
        "normalized_haystack": normalized,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="task11 commit2 (YAML化+NFKC) の再分類 before/after 比較")
    fid_group = p.add_mutually_exclusive_group(required=True)
    fid_group.add_argument("--medical-facility-id", type=int, help="対象施設ID (1施設のみ)")
    fid_group.add_argument("--all-facilities", action="store_true", help="全施設を対象にする (NFKCの影響は施設非依存のため推奨)")
    p.add_argument("--db-url", default=None)
    p.add_argument("--csv", default=None, help="差分があった行を全件CSVに書き出す場合のパス")
    args = p.parse_args()

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
    drift_rows: List[Tuple] = []
    nfkc_explained: List[dict] = []
    unexplained: List[dict] = []
    csv_rows: List[dict] = []

    for row in rows:
        (hist_id, device_number, client_device_number, cat, ev, fr, wn, rr) = row

        drift = _classify_drift(cat, ev, fr, wn, rr)
        b_cls_for_matrix = drift["before_classification"] if drift else None
        a_cls_for_matrix = drift["after_classification"] if drift else None
        if drift is None:
            # 一致した行の分類も遷移行列に含めるため、一致側は before だけ再計算せず
            # 既に classify_before/after を _classify_drift 内で呼んでいるので、ここでは
            # 変わらなかった行の分類を別途取得する (行列の対角線を埋めるため)。
            b_cls_for_matrix, _, _, _ = classify_before(cat, ev, fr, wn, rr)
            a_cls_for_matrix = b_cls_for_matrix

        transitions[(b_cls_for_matrix, a_cls_for_matrix)] += 1

        if drift is not None:
            record = {
                "medical_device_repair_history_id": hist_id,
                "device_number": device_number,
                "client_device_number": client_device_number,
                **drift,
            }
            drift_rows.append(record)
            csv_rows.append(record)
            if drift["kind"] == DriftKind.NFKC_EXPLAINED:
                nfkc_explained.append(record)
            else:
                unexplained.append(record)

    print("=== transition matrix (before_classification -> after_classification) ===")
    for (b, a), n in sorted(transitions.items(), key=lambda x: -x[1]):
        marker = "" if b == a else "  <-- DRIFT"
        print(f"  {b:>11} -> {a:<11} : {n:>7,}{marker}")
    print()

    print("=== task11 commit2 pass condition: zero drift ===")
    print(f"  rows with any classification change : {len(drift_rows):,}  (expected 0)")
    print(f"    ...explained by NFKC normalization (expected, log only) : {len(nfkc_explained):,}")
    print(f"    ...UNEXPLAINED (text unchanged by NFKC, result still moved) : {len(unexplained):,}  (expected 0 — must be root-caused before merging)")
    print()

    if nfkc_explained:
        print("=== NFKC-caused rows (per ADR: log count + row content) ===")
        for r in nfkc_explained:
            print(f"  id={r['medical_device_repair_history_id']} device={r['device_number']}")
            print(f"    raw       : {r['raw_haystack']!r}")
            print(f"    normalized: {r['normalized_haystack']!r}")
            print(f"    before: {r['before_classification']} ({r['before_rule']}, matched={r['before_matched']!r})")
            print(f"    after : {r['after_classification']} ({r['after_rule']}, matched={r['after_matched']!r})")
        print()

    if unexplained:
        print("=== !!! UNEXPLAINED DRIFT — investigate before merging commit2 !!! ===")
        for r in unexplained:
            print(f"  id={r['medical_device_repair_history_id']} device={r['device_number']}")
            print(f"    haystack (unchanged by NFKC): {r['raw_haystack']!r}")
            print(f"    before: {r['before_classification']} ({r['before_rule']}, matched={r['before_matched']!r})")
            print(f"    after : {r['after_classification']} ({r['after_rule']}, matched={r['after_matched']!r})")
        print()

    if not drift_rows:
        print("OK: 0 rows changed. failure_classifier_after.py is behavior-identical to failure_classifier_before.py on this data.")
        print()

    if args.csv:
        fieldnames = [
            "medical_device_repair_history_id", "device_number", "client_device_number",
            "kind", "before_classification", "before_rule", "before_matched",
            "after_classification", "after_rule", "after_matched",
            "raw_haystack", "normalized_haystack",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"Wrote {len(csv_rows)} drifted rows to {args.csv}")

    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())