"""
task12 entry 10 (*平均リカバリ時間（保守履歴）) の再分類 前後比較。セル交換 commit3 (rule2.5) が、entry 10 の
「分子の時間」と「分母の元になる行 (修理があった機器 x 月)」をどう動かすかを、実データで確かめる。

Reclassification before/after for task12 entry 10 (mean recovery time, maintenance history). Shows how
commit3 (セル交換 -> rule2.5) moves entry 10's numerator hours and the rows its denominator is counted
from (devices with a repair x month), on real data.

何を比べるか / What is compared:
    entry 10 の定義 (poc_metric_definition.md、2026-09-03 の決定): 平均 = 回復時間の合計 ÷ 修理があった
    機器の台数。オンプレが出すのは「機器 x 月の、修理があったか / 何時間止まったか」まで。割り算と、分母
    (*機器台数（保守対象）) を数えるのは Superset の仕事。entry 10 に専用の SQL は無く、分子も分母の元の行も
    entry4_monthly_failure_downtime.sql の fact (is_failure を直接読む) から来る。なので比較は次のとおり:
        BEFORE = 今の is_failure (保存されている値) で fact を計算
        AFTER  = セル交換を含む行だけ、after 辞書 (commit3 後) の分類で is_failure を置き換えて計算
    entry 9 と違うのは分母: 行が fact から消えると (0 時間の行でも)、その月の「修理があった機器」が減る。
    時間が動かなくても、分母は動きうる。

    Entry 10's definition (poc_metric_definition.md, decision of 2026-09-03): mean = total recovery hours
    divided by the number of serviced devices. On-premise supplies "device x month: was there a repair /
    how many hours stopped" and stops there. The division and the denominator (*機器台数（保守対象）) are
    Superset's. Entry 10 has no SQL of its own: the numerator and the rows the denominator comes from both
    come from the fact of entry4_monthly_failure_downtime.sql (which reads is_failure directly). So:
        BEFORE = fact computed with the stored is_failure as it is now
        AFTER  = same, but with is_failure replaced by the after-dictionary (post-commit3)
                 classification for the rows that contain セル交換
    What differs from entry 9 is the denominator: when a row leaves the fact (even a 0 h row), that
    month's "serviced devices" shrinks. The denominator can move even when no hours move.

「平均」は参考計算 / The mean is a reference calculation:
    Superset が実際にどう計算するかは未確認。この script の参考計算は「分子 = 期間内の fact の時間の合計、
    分母 = 期間内に fact の行を持つ機器の数 (重複なし)」と仮定する。オンプレの出力ではない (オンプレは
    割り算をしない)。前後でどれだけ動くかを見るための目安。
    How Superset actually calculates is not verified. The reference calculation here assumes "numerator =
    sum of fact hours in the period, denominator = number of distinct devices with a fact row in the
    period". It is not an on-premise output (on-premise does not divide); it only shows how far the
    number moves.

読み取り専用 / Read-only (重要 / important):
    entry4_monthly_failure_downtime.sql 自体は DROP TABLE と CREATE TABLE AS を実行するので「実行しない」。
    task12_entry9_before_after.py の load_fact_select() で SELECT 本体だけを取り出し、with_source() で
    元テーブルを SELECT に差し替えて、普通の SELECT として実行する。テーブルの作成・書き込み・DROP は
    一切ない。fact のロジックは SQL ファイルの1か所だけを読む (コピーしない)。

    The SQL file itself runs DROP TABLE and CREATE TABLE AS, so it is NOT executed. Only its SELECT body is
    extracted (load_fact_select from task12_entry9_before_after.py) and run as a plain SELECT with the
    source table swapped out. No table is created, written, or dropped. The fact logic is read from the
    single SQL file, never copied.

効果の分け方 / How the effects are separated (3 つの変更を 1 つずつ / the three changes one by one):
    1. 再分類 (commit3): 動いた行ごと、月ごと。00:00 ルールあり / なしの両方を出す
    2. 00:00 ルール (2026-10-06): 全テーブルで、ルールあり / なしの fact を比べる
    3. IS NOT NULL (ledger_id が NULL の行の除外): 全テーブルで、条件あり / なしの fact を比べる
    「ルールなし」は SQL の2つの時刻 (params CTE) を 00:00:00 にして作る。SQL の別コピーは持たない。

実行方法 / How to run (stationed_idle のルートで。task12_entry9_before_after.py と同じ場所):
    必要なもの / required: task12_entry9_before_after.py, task13_commit3_before_after.py,
        task13_commit3_cell_exchange_audit.py, entry4_monthly_failure_downtime.sql,
        keywords_before.yaml, keywords_after.yaml, streamedix_datacuration/

    python task12_entry10_before_after.py
    python task12_entry10_before_after.py --csv entry10_rows.csv
"""

import argparse
import csv as csv_module
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import create_engine, text

from streamedix_datacuration.core.keyword_dictionary import load_dictionary
from task12_entry9_before_after import (
    _LEDGER_FILTER,
    _ROWS_SQL,
    _Cell,
    _rule_times,
    _run_fact,
    _source_sql,
    _without_rule,
    load_fact_select,
)
from task13_commit3_before_after import FAILURE, _classify
from task13_commit3_cell_exchange_audit import _contains_cell_exchange


def _cell_key(c: _Cell):
    """セルの並べ替え用キー (ledger が None のものを先頭に)。
    Sort key for a cell (ledger None first)."""
    return (-1 if c[0] is None else c[0], c[1])


def _stats(fact: Dict[_Cell, float], months: Optional[Set[str]] = None) -> Tuple[int, int, float]:
    """fact (または指定した月だけ) の (行数, 機器数 (重複なし、ledger が NULL は数えない), 時間の合計) を返す。
    行数 = 「機器 x 月」の数。機器数 = 期間内に行を持つ機器の数 = 参考計算の分母。

    Return (rows, distinct devices (NULL ledger not counted), total hours) for the fact, or only for the
    given months. rows = number of device x month rows; devices = devices with a row in the period =
    the denominator of the reference calculation."""
    cells = [(c, h) for c, h in fact.items() if months is None or c[1] in months]
    return (
        len(cells),
        len({c[0] for c, _ in cells if c[0] is not None}),
        sum(h for _, h in cells),
    )


def _mean(hours: float, devices: int) -> str:
    """参考の平均 (時間 ÷ 機器数) の表示。機器が 0 なら n/a。
    Display of the reference mean (hours / devices); n/a when there are no devices."""
    return f"{hours / devices:,.2f}" if devices else "n/a"


def main() -> int:
    p = argparse.ArgumentParser(description="task12 entry 10: セル交換 (commit3) の前後比較 (読み取り専用)")
    p.add_argument("--db-url", default=None)
    p.add_argument("--before-yaml", default="keywords_before.yaml")
    p.add_argument("--after-yaml", default="keywords_after.yaml")
    p.add_argument("--sql", default="entry4_monthly_failure_downtime.sql",
                   help="fact の SELECT を読む SQL ファイル (実行はしない。SELECT 本体だけ使う)")
    p.add_argument("--csv", default=None, help="セル交換を含む全行をCSVに書き出す場合のパス")
    args = p.parse_args()

    load_dictionary.cache_clear()
    before_dict = load_dictionary(args.before_yaml)
    load_dictionary.cache_clear()
    after_dict = load_dictionary(args.after_yaml)
    load_dictionary.cache_clear()

    fact_select = load_fact_select(args.sql)
    rule_times = _rule_times(fact_select)
    has_rule = rule_times is not None
    no_rule_select = _without_rule(fact_select) if has_rule else fact_select
    has_ledger_filter = _LEDGER_FILTER in fact_select
    legacy_select = fact_select.replace(_LEDGER_FILTER, "AND true") if has_ledger_filter else fact_select

    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        fetched = conn.execute(_ROWS_SQL, {"full": "%セル交換%", "half": "%ｾﾙ交換%"}).fetchall()

        # --- 1. セル交換を含む全行を集め、before/after の分類を出す (動いた行も動かない行も) ---
        # --- 1. Collect every row containing セル交換 and classify it before/after ---
        rows: List[dict] = []
        for (hid, ledger, trouble, completion, is_completed, stored_is_failure, stored_hours,
             cat, ev, fr, wn, rr) in fetched:
            if not _contains_cell_exchange(cat, ev, fr, wn, rr):
                continue
            b_cls, b_rule, _ = _classify(before_dict, cat, ev, fr, wn, rr)
            a_cls, a_rule, a_matched = _classify(after_dict, cat, ev, fr, wn, rr)
            rows.append({
                "id": hid, "ledger": ledger, "trouble": trouble, "completion": completion,
                "is_completed": is_completed, "stored_is_failure": bool(stored_is_failure),
                "stored_hours": stored_hours, "category": cat,
                "before_cls": b_cls, "before_rule": b_rule,
                "after_cls": a_cls, "after_rule": a_rule, "after_matched": a_matched,
                "before_is_failure": b_cls == FAILURE, "after_is_failure": a_cls == FAILURE,
            })
        print(f"Rows containing セル交換 (NFKC): {len(rows):,}  (expect 26, TJ 2026-09-15_002 / task13 audit)")
        print()

        # --- 2. 前提の確認: 保存された is_failure が before 辞書の分類と一致しているか ---
        # --- 2. Precondition: does the stored is_failure equal the before-dictionary result? ---
        mismatched = [r for r in rows if r["stored_is_failure"] != r["before_is_failure"]]
        print("=== 前提: 保存された is_failure vs before 辞書の分類 / precondition: stored vs before dict ===")
        print(f"  mismatches: {len(mismatched)} / {len(rows)}  (expect 0)")
        for r in mismatched:
            print(f"    id={r['id']} stored_is_failure={r['stored_is_failure']} "
                  f"before_dict_is_failure={r['before_is_failure']}")
        print()

        left_failure = [r for r in rows if r["stored_is_failure"] and not r["after_is_failure"]]
        joined_failure = [r for r in rows if (not r["stored_is_failure"]) and r["after_is_failure"]]

        # --- 3. fact を BEFORE / AFTER で計算する (対象は、セル交換の行を持つ機器だけ)。
        #        ルールあり (今の SQL) と、ルールなしの両方 ---
        # --- 3. Compute the fact BEFORE / AFTER for the devices that have a セル交換 row,
        #        with the rule (current SQL) and without it ---
        ledgers = sorted({r["ledger"] for r in rows if r["ledger"] is not None})
        scope = ("h.medical_device_ledger_id IN (" + ",".join(str(int(x)) for x in ledgers) + ")"
                 if ledgers else "false")
        override_when = " ".join(
            f"WHEN {int(r['id'])} THEN {'true' if r['after_is_failure'] else 'false'}" for r in rows
        )
        after_expr = (
            f"CASE h.medical_device_repair_history_id {override_when} ELSE h.is_failure END"
            if rows else "h.is_failure"
        )
        before_fact = _run_fact(conn, fact_select, _source_sql(scope, "h.is_failure"))
        after_fact = _run_fact(conn, fact_select, _source_sql(scope, after_expr))
        before_nr = _run_fact(conn, no_rule_select, _source_sql(scope, "h.is_failure"))
        after_nr = _run_fact(conn, no_rule_select, _source_sql(scope, after_expr))

        # --- 4. 動いた行ごとの寄与: その行だけを元テーブルにして同じ SQL を回す ---
        # --- 4. Per-row contribution: run the same SQL with only that row as the source ---
        contribution: Dict[int, Dict[_Cell, float]] = {}
        contribution_nr: Dict[int, Dict[_Cell, float]] = {}
        for r in left_failure + joined_failure:
            only_this = f"h.medical_device_repair_history_id = {int(r['id'])}"
            contribution[r["id"]] = _run_fact(conn, fact_select, _source_sql(only_this, "true"))
            contribution_nr[r["id"]] = _run_fact(conn, no_rule_select, _source_sql(only_this, "true"))

        removed: Dict[_Cell, float] = defaultdict(float)
        added: Dict[_Cell, float] = defaultdict(float)
        touching: Dict[_Cell, List[int]] = defaultdict(list)
        for r in left_failure:
            for cell, h in contribution[r["id"]].items():
                removed[cell] += h
                touching[cell].append(r["id"])
        for r in joined_failure:
            for cell, h in contribution[r["id"]].items():
                added[cell] += h
                touching[cell].append(r["id"])

        # --- 5. 不変条件: 全セルで AFTER = BEFORE - (外れた行の分) + (加わった行の分) ---
        # --- 5. Invariant: in every cell AFTER = BEFORE - (rows that left) + (rows that joined) ---
        all_cells = sorted(set(before_fact) | set(after_fact) | set(removed) | set(added), key=_cell_key)
        unexplained: List[_Cell] = []
        changed_cells: List[_Cell] = []
        for cell in all_cells:
            b = before_fact.get(cell, 0.0)
            a = after_fact.get(cell, 0.0)
            expected = b - removed.get(cell, 0.0) + added.get(cell, 0.0)
            tol = 0.02 + 0.01 * (len(touching.get(cell, [])) or 1)
            if abs(a - b) > 0.0051:
                changed_cells.append(cell)
            if abs(a - expected) > tol:
                unexplained.append(cell)
        vanished = sorted((c for c in before_fact if c not in after_fact), key=_cell_key)
        vanished_nr = sorted((c for c in before_nr if c not in after_nr), key=_cell_key)
        appeared = [c for c in after_fact if c not in before_fact]
        # 消えたセルは、外れた行が触れたセルのはず (触れていないセルが消えたら説明できない)
        # A vanished cell must be one that a row leaving failure touched (otherwise it is unexplained)
        vanished_untouched = [c for c in vanished if c not in touching]

        # --- 6. fact 全体 (3つの版) ---
        #   whole_new = 今の SQL、whole_nr = 00:00 ルールなし、whole_old = IS NOT NULL なし
        # --- 6. The whole fact in three versions ---
        #   whole_new = current SQL, whole_nr = without the 00:00 rule, whole_old = without IS NOT NULL
        whole_new = _run_fact(conn, fact_select, _source_sql("true", "h.is_failure"))
        whole_nr = _run_fact(conn, no_rule_select, _source_sql("true", "h.is_failure")) if has_rule else whole_new
        whole_old = _run_fact(conn, legacy_select, _source_sql("true", "h.is_failure"))

        # 前提: 絞った計算 (before_fact) は、全体の計算のうち同じ機器のセルと同じになっているはず
        # Precondition: the scoped computation equals the same devices' cells of the whole computation
        scoped_whole = {c: h for c, h in whole_new.items() if c[0] in set(ledgers)}
        scope_mismatch = [c for c in set(scoped_whole) | set(before_fact)
                          if abs(scoped_whole.get(c, 0.0) - before_fact.get(c, 0.0)) > 0.011
                          or (c in scoped_whole) != (c in before_fact)]

        # AFTER の fact 全体 = 全体の BEFORE のうち、絞った機器のセルを AFTER のセルに置き換えたもの
        # The whole AFTER fact = the whole BEFORE fact with the scoped devices' cells replaced by AFTER's
        whole_after = {c: h for c, h in whole_new.items() if c not in before_fact}
        whole_after.update(after_fact)
        whole_after_nr = {c: h for c, h in whole_nr.items() if c not in before_nr}
        whole_after_nr.update(after_nr)

        # --- 7. 00:00 ルールの効果 (全テーブル) ---
        # --- 7. Effect of the 00:00 rule (whole table) ---
        rule_cells_new_only = [c for c in whole_new if c not in whole_nr]
        rule_cells_old_only = [c for c in whole_nr if c not in whole_new]
        rule_changed = [c for c in set(whole_new) & set(whole_nr) if abs(whole_new[c] - whole_nr[c]) > 0.011]

        # --- 8. IS NOT NULL の効果 (全テーブル) ---
        # --- 8. Effect of IS NOT NULL (whole table) ---
        dropped = sorted((c for c in whole_old if c not in whole_new), key=_cell_key)
        dropped_non_null = [c for c in dropped if c[0] is not None]
        changed_other = [c for c in whole_new if abs(whole_new[c] - whole_old.get(c, 0.0)) > 0.011]

        # --- 9. 修理の終わりが始まりより前の完了済み修理 (ルール適用後)。fact の行ができるか ---
        #   開始と終了が別の月にまたがって「終了が前の月」だと、月の展開 (generate_series) が空になり、
        #   行が1つもできない。同じ月なら 0 時間の行ができる。
        # --- 9. Completed repairs whose end is before the start (after the rule). Do they make a row? ---
        #   When the end is in an EARLIER month than the start, the month expansion (generate_series) is
        #   empty and no row at all is made; within the same month a 0 h row is made.
        fs, fe = rule_times if has_rule else ("00:00:00", "00:00:00")
        inverted = conn.execute(text(f"""
            SELECT id, ledger, raw_start, raw_end, adj_start, adj_end,
                   date_trunc('month', adj_end) < date_trunc('month', adj_start) AS no_row_made
            FROM (
                SELECT
                    r.medical_device_repair_history_id AS id,
                    r.medical_device_ledger_id AS ledger,
                    r.calculated_trouble_date AS raw_start,
                    r.calculated_completion_date AS raw_end,
                    CASE WHEN r.calculated_trouble_date::time = TIME '00:00:00'
                         THEN r.calculated_trouble_date::date + TIME '{fs}'
                         ELSE r.calculated_trouble_date END AS adj_start,
                    CASE WHEN r.calculated_completion_date::time = TIME '00:00:00'
                         THEN r.calculated_completion_date::date + TIME '{fe}'
                         ELSE r.calculated_completion_date END AS adj_end
                FROM cur.medical_device_repair_history r
                WHERE r.is_failure = true
                  AND r.calculated_trouble_date IS NOT NULL
                  AND r.medical_device_ledger_id IS NOT NULL
                  AND r.is_completed
                  AND r.calculated_completion_date IS NOT NULL
            ) t
            WHERE adj_end < adj_start
            ORDER BY id
        """)).fetchall()

        # --- 10. 行の時間が、その月の時間数を超えているセル (同じ機器の修理が重なっている) ---
        # --- 10. Cells whose hours exceed the hours of the month (overlapping repairs of one device) ---
        def _month_hours(ym: str) -> float:
            y, m = int(ym[:4]), int(ym[5:7])
            nxt = (y + 1, 1) if m == 12 else (y, m + 1)
            return (date(*nxt, 1) - date(y, m, 1)).days * 24.0

        over_month = sorted(
            ((c, h) for c, h in whole_new.items() if h > _month_hours(c[1]) + 0.011),
            key=lambda ch: -ch[1],
        )

    # ================= 出力 / Output =================
    print("=== セル交換を含む全行 (動いた行 / 動かない行) / all rows containing セル交換 ===")
    by_after_rule: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_after_rule[r["after_rule"]].append(r)
    for rule, group in sorted(by_after_rule.items(), key=lambda kv: -len(kv[1])):
        print(f"--- after_rule = {rule} ({len(group)} rows) ---")
        for r in group:
            moved = r in left_failure or r in joined_failure
            in_fact_before = (r["stored_is_failure"] and r["trouble"] is not None
                              and r["ledger"] is not None)
            note = []
            if moved:
                hours = sum(contribution[r["id"]].values())
                hours_nr = sum(contribution_nr[r["id"]].values())
                my_cells = sorted(contribution[r["id"]], key=_cell_key)
                gone = [c for c in my_cells if c in vanished]
                verb = "removes" if r in left_failure else "adds"
                note.append(f"MOVED: {verb} {hours:.2f}h in {len(my_cells)} device-month(s) "
                            f"(without the 00:00 rule: {hours_nr:.2f}h); "
                            f"device-month rows that disappear: {len(gone)}")
                if not my_cells:
                    note.append("NOT VISIBLE in fact (no month produced: trouble_date or ledger_id is NULL)")
            else:
                note.append("unmoved: " + ("in fact, stays" if in_fact_before else "not in fact, stays out"))
            if r["ledger"] is None:
                note.append("ledger_id=NULL")
            if r["trouble"] is None:
                note.append("trouble_date=NULL")
            if r["stored_is_failure"] and not r["is_completed"]:
                note.append("still-open")
            print(f"  id={r['id']} ledger={r['ledger']} category={r['category']!r} "
                  f"{r['before_cls']}->{r['after_cls']} [{'; '.join(note)}]")
            print(f"      trouble={r['trouble']} completion={r['completion']} "
                  f"stored calculated_downtime_hours={r['stored_hours']}")
        print()

    removed_total = sum(removed.values())
    removed_total_nr = sum(sum(contribution_nr[r["id"]].values()) for r in left_failure)
    print("=== 1. 再分類の効果 / effect of the reclassification (commit3) ===")
    print(f"  rows that left failure : {len(left_failure)}   rows that joined failure: {len(joined_failure)} (expect 0)")
    print("  分子 / numerator (hours):")
    print(f"    total hours removed: {removed_total:,.2f}  (without the 00:00 rule: {removed_total_nr:,.2f})")
    print(f"    (device, month) cells whose hours changed: {len(changed_cells)}")
    print("  分母の元の行 / rows the denominator is counted from ((device, month) rows):")
    zero_vanished = [c for c in vanished if before_fact[c] == 0.0]
    print(f"    rows that exist BEFORE but not AFTER: {len(vanished)} (of which 0.00 h rows: {len(zero_vanished)}); "
          f"rows that appear AFTER only: {len(appeared)} (expect 0)")
    print(f"    without the 00:00 rule, rows that disappear: {len(vanished_nr)}")
    for cell in vanished:
        print(f"      disappears: {str(cell):<24} before={before_fact[cell]:>7.2f} h  rows={touching.get(cell, [])}")
    print()

    print("  cell (ledger, month) | before -> after | removed by rows")
    for cell in changed_cells:
        b = before_fact.get(cell, 0.0)
        a = after_fact.get(cell, 0.0)
        gone = "  (row disappears)" if cell not in after_fact else ""
        print(f"    {str(cell):<24} {b:>10.2f} -> {a:>10.2f}   rows={touching.get(cell, [])}{gone}")
    print()

    # 影響を受けた月 (行が消えた月 + 時間が変わった月) ごとの参考計算
    # Reference calculation for each affected month (rows disappeared or hours changed)
    affected_months = sorted({c[1] for c in vanished} | {c[1] for c in changed_cells})
    print("  参考計算 / reference calculation per affected month (mean = hours / devices serviced that month):")
    print("    month    devices before->after | hours before->after | mean before->after"
          " | mean without 00:00 rule before->after")
    for ym in affected_months:
        db, hb = _stats(whole_new, {ym})[1], _stats(whole_new, {ym})[2]
        da, ha = _stats(whole_after, {ym})[1], _stats(whole_after, {ym})[2]
        dbn, hbn = _stats(whole_nr, {ym})[1], _stats(whole_nr, {ym})[2]
        dan, han = _stats(whole_after_nr, {ym})[1], _stats(whole_after_nr, {ym})[2]
        print(f"    {ym}  {db:>5} -> {da:<5} | {hb:>12,.2f} -> {ha:<12,.2f} | {_mean(hb, db):>8} -> {_mean(ha, da):<8}"
              f" | {_mean(hbn, dbn):>8} -> {_mean(han, dan)}")
    print()

    # 期間が1か月より長いとき: 同じ年の12か月をまとめて、機器の数 (重複なし) がどう動くか
    # For periods longer than a month: all 12 months of a year together; how the distinct device count moves
    years = sorted({ym[:4] for ym in affected_months})
    print("  参考計算 / reference calculation per affected calendar year (distinct devices in the year):")
    print("    year  devices before->after | hours before->after | mean before->after")
    for y in years:
        ms = {f"{y}-{m:02d}" for m in range(1, 13)}
        _, db, hb = _stats(whole_new, ms)
        _, da, ha = _stats(whole_after, ms)
        print(f"    {y}  {db:>5} -> {da:<5} | {hb:>14,.2f} -> {ha:<14,.2f} | {_mean(hb, db):>8} -> {_mean(ha, da)}")
    print()

    print("=== pass condition: every changed or disappearing row is explained by the rows that left failure ===")
    print(f"  cells where AFTER != BEFORE - (rows that left failure): {len(unexplained)}  (expect 0)")
    for cell in unexplained:
        print(f"    UNEXPLAINED {cell}: before={before_fact.get(cell, 0.0):.2f} "
              f"after={after_fact.get(cell, 0.0):.2f} removed={removed.get(cell, 0.0):.2f}")
    print(f"  rows that disappear without a moved row touching them: {len(vanished_untouched)}  (expect 0)")
    print(f"  scoped computation vs the same devices in the whole computation: {len(scope_mismatch)} differing  (expect 0)")
    print()

    print("=== 2. 00:00 ルールの効果 (全テーブル) / effect of the 00:00 rule, whole table ===")
    if not has_rule:
        print("  the SQL file does not contain the date-only rule, so before = after here")
    else:
        rn, dn, hn = _stats(whole_nr)
        ru, du, hu = _stats(whole_new)
        print(f"  without the rule: {rn:,} device-month rows / {dn:,} devices / {hn:,.2f} h")
        print(f"  with the rule   : {ru:,} device-month rows / {du:,} devices / {hu:,.2f} h")
        print(f"  分子 / numerator: hours changed in {len(rule_changed):,} rows, total {hu - hn:+,.2f} h")
        print(f"  分母の元の行 / denominator rows: only with the rule {len(rule_cells_new_only):,}, "
              f"only without it {len(rule_cells_old_only):,}  (0 and 0 means the denominator is not moved by the rule)")
    print()

    print("=== 3. IS NOT NULL の効果 (全テーブル) / effect of IS NOT NULL, whole table ===")
    if not has_ledger_filter:
        print("  the SQL file does not contain the IS NOT NULL condition, so before = after here")
    ro, do, ho = _stats(whole_old)
    rw, dw, hw = _stats(whole_new)
    null_rows = [c for c in whole_old if c[0] is None]
    print(f"  without the condition: {len(whole_old):,} device-month rows ({len(null_rows):,} with NULL ledger) "
          f"/ {do:,} devices / {ho:,.2f} h")
    print(f"  with the condition   : {len(whole_new):,} device-month rows / {dw:,} devices / {hw:,.2f} h")
    print(f"  分子 / numerator: {sum(whole_old[c] for c in dropped):,.2f} h dropped"
          + (f" ({sum(whole_old[c] for c in dropped) / ho:.1%} of hours before)" if ho else ""))
    print(f"  分母 / denominator: {len(dropped):,} rows dropped. If the denominator counts DISTINCT devices, NULL is "
          f"not a device and the count is unchanged ({do:,} -> {dw:,}); if it counts rows, it falls by {len(dropped):,}")
    print(f"  dropped rows that are NOT NULL-ledger: {len(dropped_non_null)}  (expect 0); "
          f"remaining rows whose hours changed: {len(changed_other)}  (expect 0)")
    print()

    print("=== 追加の観察 / side observations ===")
    print(f"  completed failure repairs whose end is before the start after the rule: {len(inverted)}")
    for (rid, ledger, raw_s, raw_e, adj_s, adj_e, no_row) in inverted:
        print(f"    id={rid} ledger={ledger} raw {raw_s} -> {raw_e}  after rule {adj_s} -> {adj_e}  "
              f"{'NO fact row is made (end is in an earlier month)' if no_row else 'a 0 h row is made (same month)'}")
    print(f"  (device, month) rows whose hours exceed the hours of that month (overlapping repairs): {len(over_month):,}")
    for (cell, h) in over_month[:5]:
        print(f"    {str(cell):<24} {h:,.2f} h  (month has {_month_hours(cell[1]):,.0f} h)")
    print(f"  rows containing セル交換 with ledger_id NULL: {sum(1 for r in rows if r['ledger'] is None)} "
          f"(outside the fact whatever their classification)")
    print()

    if args.csv:
        fieldnames = ["id", "ledger", "category", "trouble", "completion", "is_completed",
                      "stored_is_failure", "before_cls", "before_rule", "after_cls", "after_rule",
                      "after_matched", "before_is_failure", "after_is_failure"]
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv_module.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")

    return 1 if (unexplained or joined_failure or vanished_untouched or scope_mismatch
                 or dropped_non_null or changed_other) else 0


if __name__ == "__main__":
    raise SystemExit(main())