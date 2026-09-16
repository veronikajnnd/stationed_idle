"""
43件の修理不能行 — 機種（台帳連携）の集中度チェック
The 43 `修理不能`-driven rows — checking whether they concentrate in a small number
of device models, via a join to `cur.medical_device_ledger`.

背景 / Background:
    2026-09-14 の worklog で見つけた、バッテリー消耗かつ Option B でも failure のままの
    49 行のうち、43 行は `修理不能` というキーワードでマッチしている。Miyazawa-san の
    2026-09-15 レビュー（点4）、および更新後の ADR-2026-06-16 の「気になる点（今は変えない）」
    に記載された未解決事項として、この 43 行が少数の機種に集中しているのかを、台帳
    （cur.medical_device_ledger）と紐付けて確認するよう依頼された。ADR原文（引用）:
    「修理不能の43行が、特定の機種に集まっていないか、はまだ確かめていない。集まっていれば、
    その機種の故障率だけが大きく動く」。

    Of the 49 バッテリー消耗 rows that stay `failure` under Option B (found 2026-09-14),
    43 fire specifically on the `修理不能` keyword. This is not just an informal review
    comment: the updated ADR-2026-06-16 (2026-09-15 revision) records it verbatim as an
    open item under "気になる点（今は変えない）": "whether the 43 修理不能 rows concentrate
    in a specific device model has not yet been confirmed; if they do, that model's failure
    rate alone would move significantly." This script answers that open item by joining to
    the device ledger (`cur.medical_device_ledger`) via `medical_device_ledger_id`.

注意 / Caveat (未対応 / not yet addressed):
    このスクリプトは `バッテリー消耗` / `修理不能` を単純な部分文字列一致で判定しており、
    NFKC正規化は行っていない。ADR-2026-09-15（辞書・施設マッピング）の決定事項4は、
    本番分類器でのキーワード比較は必ずNFKC正規化してから行うと決めており、task12の
    調査でも一部のrule3キーワード（バージョンアップ/オーバーホール等）に全角/半角の
    表記ゆれが実データに存在することが確認済み。`バッテリー消耗`/`修理不能` 自体に
    半角形が存在するかはまだ未確認であり、存在する場合はこの「43件」が undercount
    になっている可能性がある。この件数はMiyazawa-sanへの結論に直結するため、結果を
    報告する前に半角カナ変種の有無を確認したほうが安全。

    This script matches `バッテリー消耗` / `修理不能` with plain substring checks, no NFKC
    normalization. ADR-2026-09-15's decision 4 requires the production classifier to
    normalize (NFKC) before comparing keywords, and task12's own investigation already
    found full-width/half-width duplicate forms for some rule3 keywords in real data.
    Whether `バッテリー消耗` / `修理不能` themselves have half-width variants in this
    dataset has not been checked; if they do, the "43 rows" count here could be an
    undercount. Since this number feeds directly into a conclusion for Miyazawa-san,
    worth a quick sanity check for half-width kana forms before reporting the result.

本番コードは変更しない / does NOT modify the shipped classifier:
    `battery.py` と同じディレクトリに置いて実行する。`_build_haystack` / `classify_option_b`
    をそのまま再利用し、47列(元のクエリ)ではなく機種情報を含む独自のクエリを使う。
    Place this file in the SAME directory as `battery.py`; it reuses `_build_haystack` /
    `classify_option_b` from there, but uses its own SQL query (not `battery.py`'s
    `_QUERY`) since it needs device/model columns that query does not select.

実行方法 / How to run (same venv as the other scripts):
    python battery43.py --medical-facility-id 1
"""

import argparse
from collections import Counter

from sqlalchemy import create_engine, text

from battery import _build_haystack, classify_option_b

# repair_history と ledger を medical_device_ledger_id で LEFT JOIN する。
# NULL になる場合は、その修理行がどの台帳エントリにも紐付いていないことを意味する。
# LEFT JOIN repair_history to the ledger via medical_device_ledger_id. A NULL ledger
# side means this repair row isn't linked to any ledger entry at all.
_QUERY = text(
    """
    SELECT
        r.repair_category, r.event_note, r.failure_reason, r.work_note, r.repair_result,
        r.medical_device_ledger_id,
        r.client_device_number AS repair_client_device_number,
        r.device_number AS repair_device_number,
        r.product_name AS repair_product_name,
        r.model_number AS repair_model_number,
        r.manufacturer_name AS repair_manufacturer_name,
        l.product_name AS ledger_product_name,
        l.model_number AS ledger_model_number,
        l.manufacturer_name AS ledger_manufacturer_name,
        l.device_category AS ledger_device_category
    FROM cur.medical_device_repair_history r
    LEFT JOIN cur.medical_device_ledger l
        ON l.medical_device_ledger_id = r.medical_device_ledger_id
    WHERE r.medical_facility_id = :fid
    """
)


def main() -> int:
    p = argparse.ArgumentParser(description="43件の修理不能行を機種別に集計")
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

    # 49件のうち「修理不能」でマッチした43件だけを対象にする
    # (バッテリー消耗が含まれ、Option Bでもfailureのままで、マッチ語が「修理不能」の行)。
    # Narrow down to the 43 of 49 rows specifically matched on 修理不能 (contains
    # バッテリー消耗, still 'failure' under Option B, matched keyword is 修理不能).
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

        _, _, haystack = _build_haystack(cat, ev, fr, wn, rr)
        if "バッテリー消耗" not in haystack:
            continue

        b_cls, b_rule, b_matched = classify_option_b(cat, ev, fr, wn, rr)
        if b_cls != "failure" or b_matched != "修理不能":
            continue

        # 台帳側の機種名があればそちらを正とし、紐付かない場合は修理履歴側の値にフォールバックする
        # Prefer the ledger's model/product name as canonical; fall back to the repair
        # row's own value when there is no ledger link.
        model_number = ledger_model_number or repair_model_number
        product_name = ledger_product_name or repair_product_name
        manufacturer_name = ledger_manufacturer_name or repair_manufacturer_name

        target_rows.append(
            {
                "repair_client_device_number": repair_client_device_number,
                "repair_device_number": repair_device_number,
                "medical_device_ledger_id": ledger_id,
                "ledger_matched": ledger_id is not None,
                "model_number": model_number,
                "product_name": product_name,
                "manufacturer_name": manufacturer_name,
                "ledger_device_category": ledger_device_category,
                "repair_result": rr,
            }
        )

    print(f"Total facility_id={args.medical_facility_id} rows scanned: {len(rows):,}")
    print(f"Rows matched on 修理不能 (target set): {len(target_rows)}")
    print()

    unmatched = [r for r in target_rows if not r["ledger_matched"]]
    print(f"Rows with NO ledger link at all (medical_device_ledger_id is NULL): {len(unmatched)}")
    print()

    print("=== count by model_number (canonical: ledger, fallback: repair row) ===")
    model_counts = Counter(r["model_number"] or "(none)" for r in target_rows)
    for model, n in model_counts.most_common():
        print(f"  {model}: {n}")
    print()

    print("=== count by product_name (canonical: ledger, fallback: repair row) ===")
    product_counts = Counter(r["product_name"] or "(none)" for r in target_rows)
    for product, n in product_counts.most_common():
        print(f"  {product}: {n}")
    print()

    print("=== count by manufacturer_name ===")
    mfr_counts = Counter(r["manufacturer_name"] or "(none)" for r in target_rows)
    for mfr, n in mfr_counts.most_common():
        print(f"  {mfr}: {n}")
    print()

    print("=== full row detail ===")
    for i, r in enumerate(target_rows, start=1):
        print(f"[{i}]")
        for k, v in r.items():
            print(f"  {k}: {v!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())