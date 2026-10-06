"""
task12 entry 9 (貸出余力の合計拘束時間) の再分類 前後比較。セル交換 commit3 (rule2.5) が、entry 9 の
「回復時間の項」(= entry 4 の月次ダウンタイム fact) をどう動かすかを、実データで確かめる。

Reclassification before/after for task12 entry 9 (rental headroom committed time). Shows how
commit3 (セル交換 -> rule2.5) moves entry 9's recovery-time term (= entry 4's monthly downtime
fact) on real data.

何を比べるか / What is compared:
    entry 9 のうち、分類 (is_failure) に影響されるのは「ダウンタイムの項」だけ。貸出時間の項
    (稼働時間) は修理の分類を読まないので、構造上、変わらない (この script は貸出時間を見ない)。
    ダウンタイムの項は、entry4_monthly_failure_downtime.sql が cur.medical_device_repair_history.is_failure
    を直接読んで作る。なので比較は次のとおり:
        BEFORE = 今の is_failure (保存されている値) で fact を計算
        AFTER  = セル交換を含む行だけ、after 辞書 (commit3 後) の分類で is_failure を置き換えて計算
    Of entry 9, only the downtime term depends on classification (is_failure). The rental-hours
    term never reads repair classification, so it cannot change (this script does not look at it).
    The downtime term is built by entry4_monthly_failure_downtime.sql reading
    cur.medical_device_repair_history.is_failure directly, so:
        BEFORE = fact computed with the stored is_failure as it is now
        AFTER  = same, but with is_failure replaced by the after-dictionary (post-commit3)
                 classification for the rows that contain セル交換

読み取り専用 / Read-only (重要 / important):
    entry4_monthly_failure_downtime.sql 自体は DROP TABLE と CREATE TABLE AS を実行するので、
    このファイルは「実行しない」。load_fact_select() で SELECT 本体だけを取り出し、
    with_source() で元テーブルを SELECT に差し替えて、普通の SELECT として実行する。テーブルの
    作成・書き込み・DROP は一切ない。fact のロジックは SQL ファイルの1か所だけを読む (コピーしない)。

    The SQL file itself runs DROP TABLE and CREATE TABLE AS, so it is NOT executed. Only its
    SELECT body is extracted and run as a plain SELECT with the source table swapped out. No table
    is created, written, or dropped. The fact logic is read from the single SQL file, never copied.

効果の分け方 / How the effects are separated (3 つの変更を 1 つずつ分けて数える / three changes, counted one by one):
    1. 再分類 (commit3, セル交換): 動いた行ごとの寄与。00:00 ルールあり / なしの両方を出す
       Reclassification: per moved row, with and without the 00:00 rule
    2. 00:00 ルール (2026-10-06): 全テーブルで、ルールあり / なしの fact を比べる
       The 00:00 rule: the whole-table fact with vs without the rule
    3. IS NOT NULL (ledger_id が NULL の行の除外): 全テーブルで、条件あり / なしの fact を比べる
       IS NOT NULL (excluding rows with NULL ledger_id): the whole-table fact with vs without it
    「ルールなし」は、SQL の2つの時刻 (params CTE) を 00:00:00 にして作る。SQL の別コピーは持たない。
    "Without the rule" is built by setting the SQL's two times (params CTE) to 00:00:00. No second copy of the SQL.

不変条件 / Invariant checked (test_entry9_downtime_fact.py の test 7 と同じ考え方):
    すべての (機器, 月) について   AFTER = BEFORE - (failure から外れた行が、その月に持っていた分)
    「failure から外れた行が持っていた分」は、その行だけを元テーブルにして同じ SQL を回して得る。
    これが全セルで成り立てば、動いたセルはすべて、外れた行だけで説明できる (説明できない動きは 0)。

    For every (device, month): AFTER = BEFORE - (what the rows that left failure contributed).
    That contribution is obtained by running the same SQL with only that row as the source. If it
    holds in every cell, every moved cell is explained by the rows that left failure alone.

実行方法 / How to run (task13 の2つの script と同じディレクトリで。stationed_idle 側):
    必要なもの / required: task13_commit3_before_after.py, task13_commit3_cell_exchange_audit.py,
        entry4_monthly_failure_downtime.sql, keywords_before.yaml,
        keywords_after.yaml, streamedix_datacuration/ (keyword_dictionary.py)

    python task12_entry9_before_after.py
    python task12_entry9_before_after.py --csv entry9_rows.csv
"""

import argparse
import csv as csv_module
import re
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

from streamedix_datacuration.core.keyword_dictionary import load_dictionary
from task13_commit3_before_after import FAILURE, _classify
from task13_commit3_cell_exchange_audit import _contains_cell_exchange

# ---- fact の SELECT 本体を SQL ファイルから取り出す2つの関数 ----
# ---- Two functions that extract the fact's SELECT body from the SQL file ----
# test_entry9_downtime_fact.py の同名の関数と同じもの。この script は使い捨てなので、別ファイルにせず
# ここに置いた (SQL の fact ロジックそのものは、どちらも SQL ファイルの1か所だけを読む)。
# Same as the functions of the same name in test_entry9_downtime_fact.py. This script is throwaway, so
# they live here instead of a separate module (the fact logic itself is still read from the one SQL file).
# 元ファイルのテーブル名 (fact テーブルを作る文の目印)
# The statement that builds the fact table (used as the anchor to find the SELECT body).
_CREATE_FACT = re.compile(r"CREATE\s+TABLE\s+cur\.monthly_failure_downtime\s+AS\s*", re.IGNORECASE)

# SELECT 本体が読む元テーブル (CTE 名に差し替える対象)
# The source table the SELECT body reads from (replaced by a CTE name).
_SOURCE_TABLE = "cur.medical_device_repair_history"


def load_fact_select(sql_path: str) -> str:
    """CREATE TABLE ... AS の後ろの SELECT 本体 (WITH ... ORDER BY ...) を返す。

    Returns the SELECT body (WITH ... ORDER BY ...) that follows CREATE TABLE ... AS.
    """
    sql_text = Path(sql_path).read_text(encoding="utf-8")
    m = _CREATE_FACT.search(sql_text)
    if m is None:
        raise ValueError(
            "CREATE TABLE cur.monthly_failure_downtime AS が見つからない / not found in " + str(sql_path)
        )

    # 行コメント (--) を落としてから最初の ';' で切る。コメント内の ';' で誤って切れないようにするため。
    # このステートメントの範囲には文字列リテラル内の '--' は無い。
    # Strip -- line comments, then cut at the first ';' so a ';' inside a comment cannot end the
    # statement early. There is no '--' inside a string literal within this statement.
    body = sql_text[m.end():]
    cleaned = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    statement = cleaned.split(";", 1)[0].strip()

    if not statement.upper().startswith("WITH"):
        raise ValueError("SELECT 本体が WITH で始まらない / body does not start with WITH")
    if _SOURCE_TABLE not in statement:
        raise ValueError(_SOURCE_TABLE + " を読んでいない / source table not referenced")
    return statement


def with_source(fact_select: str, source_sql: str, hist_name: str = "hist") -> str:
    """fact の SELECT が読む元テーブルを、任意の SELECT (source_sql) に差し替えて返す。

    source_sql を先頭の CTE (hist_name) として置き、元の SQL 内の cur.medical_device_repair_history
    をその CTE 名に置き換える。これで、テーブルを一切作らず・書き換えず、「この行たちを元テーブルだと
    思って fact を計算する」ことができる (test では合成データ、before/after では分類の差し替え)。

    Replaces the table the fact SELECT reads with an arbitrary SELECT (source_sql): source_sql
    becomes a leading CTE (hist_name) and every cur.medical_device_repair_history reference in the
    original SQL is rewritten to that CTE name. This computes the fact "as if these rows were the
    source table" without creating or modifying any table (synthetic rows in tests, an overridden
    classification in before/after).
    """
    rest = fact_select[len("WITH"):].lstrip()
    rest = rest.replace(_SOURCE_TABLE, hist_name)
    return f"WITH {hist_name} AS (\n{source_sql}\n),\n{rest}"


# セル交換を含みうる行を、粗い ILIKE で広めに取る (全角/半角)。正確な判定は Python 側の
# _contains_cell_exchange (NFKC 正規化後の部分一致) でもう一度かける。task13 の audit と同じ二段構え。
# Fetch rows that may contain セル交換 with a loose ILIKE (both widths); the precise check is
# re-applied in Python (substring after NFKC). Same two-stage approach as the task13 audit.
_ROWS_SQL = text(
    """
    SELECT
        medical_device_repair_history_id,
        medical_device_ledger_id,
        calculated_trouble_date,
        calculated_completion_date,
        is_completed,
        is_failure,
        calculated_downtime_hours,
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

_Cell = Tuple[Optional[int], str]  # (medical_device_ledger_id, 'YYYY-MM')

# SQL に足した条件の文字列 (変更前後の比較で、これを外した SQL を作る)
# The condition added to the SQL (removed in text to build the pre-change SQL for the comparison).
_LEDGER_FILTER = "AND r.medical_device_ledger_id IS NOT NULL"

# 日付だけの修理時刻のルール (2026-10-06) の2つの時刻は、SQL の params CTE から読む。ここには書かない。
# The two times of the date-only repair time rule (2026-10-06) are read from the SQL's params CTE.
# They are not written here.
_START_TIME_RE = re.compile(r"TIME\s+'([0-9:]+)'\s+AS\s+date_only_start_time", re.IGNORECASE)
_END_TIME_RE = re.compile(r"TIME\s+'([0-9:]+)'\s+AS\s+date_only_end_time", re.IGNORECASE)


def _rule_times(fact_select: str) -> Optional[Tuple[str, str]]:
    """SQL の params CTE から (開始に使う時刻, 完了に使う時刻) を返す。ルールが無い SQL なら None。

    Return (start fill time, completion fill time) from the SQL's params CTE, or None when the SQL has
    no such rule."""
    m_start = _START_TIME_RE.search(fact_select)
    m_end = _END_TIME_RE.search(fact_select)
    if m_start and m_end:
        return m_start.group(1), m_end.group(1)
    return None


def _without_rule(fact_select: str) -> str:
    """ルールを無効にした SQL を返す。2つの時刻を 00:00:00 にすると、「00:00:00 を 00:00:00 に置きかえる」
    ことになり、置きかえが何も変えない (ルール導入前と同じ計算になる)。

    Return the SQL with the rule neutralized. Setting both times to 00:00:00 makes the replacement
    "replace 00:00:00 by 00:00:00", which changes nothing (the same calculation as before the rule)."""
    out = _START_TIME_RE.sub("TIME '00:00:00' AS date_only_start_time", fact_select)
    return _END_TIME_RE.sub("TIME '00:00:00' AS date_only_end_time", out)


def _source_sql(
    ids_filter: str, is_failure_expr: str
) -> str:
    """fact の元テーブルに差し替える SELECT を作る。ids_filter と is_failure_expr は、呼び出し側が
    整数 (DB から取った id) だけから組み立てた文字列で、ユーザー入力は含まない。

    Builds the SELECT that replaces the fact's source table. ids_filter and is_failure_expr are
    built by the caller from integers read from the DB only, never from user input."""
    return f"""
        SELECT
            h.medical_device_repair_history_id,
            h.medical_device_ledger_id,
            h.calculated_trouble_date,
            h.calculated_completion_date,
            h.is_completed,
            h.calculated_downtime_hours,
            {is_failure_expr} AS is_failure
        FROM cur.medical_device_repair_history h
        WHERE {ids_filter}
    """


def _run_fact(conn, fact_select: str, source_sql: str) -> Dict[_Cell, float]:
    """差し替えた元テーブルで fact の SELECT を実行し、{(機器, 月): 時間} を返す (読み取り専用)。

    Run the fact SELECT over a swapped source and return {(device, month): hours} (read-only)."""
    sql = with_source(fact_select, source_sql)
    out: Dict[_Cell, float] = {}
    for ledger, month_start, hours in conn.execute(text(sql)).fetchall():
        out[(ledger, month_start.strftime("%Y-%m"))] = float(hours)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="task12 entry 9: セル交換 (commit3) の前後比較 (読み取り専用)")
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

        # --- 2. 前提の確認: 保存されている is_failure が、before 辞書の分類と一致しているか ---
        # fact は保存されている is_failure を読む。それが before 辞書と食い違っていたら、
        # 「前」の定義が揺らぐ (例: すでに commit3 のコードで curate を再実行済み)。
        # --- 2. Precondition: does the stored is_failure equal the before-dictionary result? ---
        # The fact reads the stored is_failure; if it disagrees with the before dictionary, the
        # meaning of "before" is shaky (e.g. curate was already re-run with commit3's code).
        mismatched = [r for r in rows if r["stored_is_failure"] != r["before_is_failure"]]
        print("=== 前提: 保存された is_failure vs before 辞書の分類 / precondition: stored vs before dict ===")
        print(f"  mismatches: {len(mismatched)} / {len(rows)}  (expect 0)")
        for r in mismatched:
            print(f"    id={r['id']} stored_is_failure={r['stored_is_failure']} "
                  f"before_dict_is_failure={r['before_is_failure']}")
        print()

        # --- 3. 動いた行 (保存された is_failure から after で変わる行) を特定する ---
        # --- 3. Identify moved rows (stored is_failure differs from the after result) ---
        left_failure = [r for r in rows if r["stored_is_failure"] and not r["after_is_failure"]]
        joined_failure = [r for r in rows if (not r["stored_is_failure"]) and r["after_is_failure"]]

        # --- 4. fact を BEFORE / AFTER で計算する (対象は、セル交換の行を持つ機器だけ) ---
        # --- 4. Compute the fact BEFORE / AFTER (only for devices that have a セル交換 row) ---
        ledgers = sorted({r["ledger"] for r in rows if r["ledger"] is not None})
        has_null_ledger = any(r["ledger"] is None for r in rows)
        parts = []
        if ledgers:
            parts.append("h.medical_device_ledger_id IN (" + ",".join(str(int(x)) for x in ledgers) + ")")
        if has_null_ledger:
            parts.append("h.medical_device_ledger_id IS NULL")
        scope = "(" + " OR ".join(parts) + ")" if parts else "false"

        override_when = " ".join(
            f"WHEN {int(r['id'])} THEN {'true' if r['after_is_failure'] else 'false'}" for r in rows
        )
        after_expr = (
            f"CASE h.medical_device_repair_history_id {override_when} ELSE h.is_failure END"
            if rows else "h.is_failure"
        )
        before_fact = _run_fact(conn, fact_select, _source_sql(scope, "h.is_failure"))
        after_fact = _run_fact(conn, fact_select, _source_sql(scope, after_expr))
        # 実テーブルとの比較用: ルール導入前の計算での BEFORE (実テーブルは古い SQL で作られているため)
        # For the real-table comparison: BEFORE with the pre-rule calculation (the real table was built
        # with the older SQL)
        before_fact_nr = _run_fact(conn, no_rule_select, _source_sql(scope, "h.is_failure"))

        # --- 5. 動いた行ごとの寄与: その行だけを元テーブルにして同じ SQL を回す ---
        # --- 5. Per-row contribution: run the same SQL with only that row as the source ---
        contribution: Dict[int, Dict[_Cell, float]] = {}
        for r in left_failure + joined_failure:
            contribution[r["id"]] = _run_fact(
                conn, fact_select,
                _source_sql(f"h.medical_device_repair_history_id = {int(r['id'])}", "true"),
            )
        # 同じ行の寄与を、ルールなしの計算でも出す (再分類の効果とルールの効果を分けて見るため)
        # The same rows' contribution under the pre-rule calculation (to separate the effect of the
        # reclassification from the effect of the rule)
        contribution_nr: Dict[int, Dict[_Cell, float]] = {}
        for r in left_failure + joined_failure:
            contribution_nr[r["id"]] = _run_fact(
                conn, no_rule_select,
                _source_sql(f"h.medical_device_repair_history_id = {int(r['id'])}", "true"),
            )

        # --- 6. 全セルで 「AFTER = BEFORE - 外れた行の寄与 + 加わった行の寄与」 を確かめる ---
        # --- 6. Check AFTER = BEFORE - (rows that left) + (rows that joined) in every cell ---
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

        all_cells = sorted(
            set(before_fact) | set(after_fact) | set(removed) | set(added),
            key=lambda c: (-1 if c[0] is None else c[0], c[1]),
        )
        unexplained: List[_Cell] = []
        changed_cells: List[_Cell] = []
        for cell in all_cells:
            b = before_fact.get(cell, 0.0)
            a = after_fact.get(cell, 0.0)
            expected = b - removed.get(cell, 0.0) + added.get(cell, 0.0)
            n_rows = len(touching.get(cell, [])) or 1
            tol = 0.02 + 0.01 * n_rows  # 各値が小数第2位で丸められるぶんの許容 / rounding tolerance
            if abs(a - b) > 0.0051:
                changed_cells.append(cell)
            if abs(a - expected) > tol:
                unexplained.append(cell)

        # 時間は変わらなくても「セル (行) 自体が消える」ことがある (0.00h のセル)。時間の差だけを見ると
        # これを見落とすので、存在の差を別に数える。
        # A cell can vanish even when no hours change (a 0.00 h cell). Counting only hour differences
        # would miss that, so the difference in existence is counted separately.
        vanished = sorted(
            (c for c in before_fact if c not in after_fact),
            key=lambda c: (-1 if c[0] is None else c[0], c[1]),
        )
        appeared = [c for c in after_fact if c not in before_fact]

        # --- 7. 実際の cur.monthly_failure_downtime が、今 BEFORE と一致しているか (古くないか) ---
        # --- 7. Does the real cur.monthly_failure_downtime currently equal BEFORE (is it stale)? ---
        table_exists = conn.execute(text("SELECT to_regclass('cur.monthly_failure_downtime') IS NOT NULL")).scalar()
        stale: List[_Cell] = []
        stale_null: List[_Cell] = []
        stale_vs_new: List[_Cell] = []
        null_ledger_in_table: Optional[bool] = None
        real: Dict[_Cell, float] = {}
        if table_exists:
            rows_real = conn.execute(
                text(f"""SELECT medical_device_ledger_id, month_start, downtime_hours
                         FROM cur.monthly_failure_downtime h
                         WHERE {scope}""")
            ).fetchall()
            for ledger, month_start, hours in rows_real:
                real[(ledger, month_start.strftime("%Y-%m"))] = float(hours)
            for cell in set(real) | set(before_fact_nr):
                if abs(real.get(cell, 0.0) - before_fact_nr.get(cell, 0.0)) > 0.011:
                    stale.append(cell)
            # 参考: 実テーブルが今の SQL (ルールあり) で作られていた場合に差が出るセル数
            # For reference: cells that would differ if the table were built with the current SQL (rule on)
            stale_vs_new = [c for c in set(real) | set(before_fact)
                            if c[0] is not None and abs(real.get(c, 0.0) - before_fact.get(c, 0.0)) > 0.011]
            null_ledger_in_table = any(c[0] is None for c in real)
            # NULL ledger のセルは、IS NOT NULL を足した SQL で作り直すと消える。実テーブルが古い SQL で
            # 作られていれば残っているので、ここで差として出るのは想定内。別に数える。
            # NULL-ledger cells vanish when the table is rebuilt with the IS NOT NULL condition. If the
            # real table was built with the older SQL they remain, so that difference is expected and is
            # counted separately from genuine staleness.
            stale_null = [c for c in stale if c[0] is None]
            stale = [c for c in stale if c[0] is not None]

        # --- 8. SQL 変更そのものの効果: IS NOT NULL を足す前後で、fact 全体がどう変わるか ---
        # 足した条件だけを SQL 文字列から外して「変更前の SQL」を作り、同じ全行に対して両方を回す。
        # 変わるのは NULL ledger のセルだけのはず (それ以外のセルは1つも変わらない)。
        # --- 8. Effect of the SQL change itself: how the whole fact changes with vs without the
        # IS NOT NULL condition. The condition is removed from the SQL text to get the pre-change SQL,
        # and both are run over all rows. Only NULL-ledger cells should change (no other cell).
        null_rows_total = conn.execute(text(
            "SELECT count(*) FROM cur.medical_device_repair_history "
            "WHERE is_failure = true AND medical_device_ledger_id IS NULL "
            "AND calculated_trouble_date IS NOT NULL"
        )).scalar()
        has_ledger_filter = _LEDGER_FILTER in fact_select
        legacy_select = fact_select.replace(_LEDGER_FILTER, "AND true") if has_ledger_filter else fact_select
        whole_new = _run_fact(conn, fact_select, _source_sql("true", "h.is_failure"))
        whole_old = _run_fact(conn, legacy_select, _source_sql("true", "h.is_failure"))
        dropped = sorted((c for c in whole_old if c not in whole_new), key=lambda c: c[1])
        dropped_non_null = [c for c in dropped if c[0] is not None]
        changed_other = [c for c in whole_new
                         if abs(whole_new[c] - whole_old.get(c, 0.0)) > 0.011]
        dropped_hours = sum(whole_old[c] for c in dropped)
        old_hours_total = sum(whole_old.values())
        new_hours_total = sum(whole_new.values())
        dropped_months = [c[1] for c in dropped]

        # --- 9. 00:00 ルールそのものの効果: ルールあり / なしで、fact 全体がどう変わるか ---
        # 2つの時刻を 00:00:00 にして「ルールなし」の SQL を作り、同じ全行に対して両方を回す。
        # IS NOT NULL の条件はどちらにも入っているので、ルールだけの差になる。
        # --- 9. Effect of the 00:00 rule itself: how the whole fact changes with vs without the rule.
        # "Without the rule" is the SQL with both times set to 00:00:00; both are run over all rows.
        # The IS NOT NULL condition is in both, so the difference is the rule alone.
        whole_nr = _run_fact(conn, no_rule_select, _source_sql("true", "h.is_failure")) if has_rule else whole_new
        rule_changed = sorted(
            (c for c in set(whole_new) | set(whole_nr)
             if abs(whole_new.get(c, 0.0) - whole_nr.get(c, 0.0)) > 0.011),
            key=lambda c: (c[0], c[1]),
        )
        rule_hours_delta = sum(whole_new.values()) - sum(whole_nr.values())
        rule_cells_new_only = [c for c in whole_new if c not in whole_nr]
        rule_cells_old_only = [c for c in whole_nr if c not in whole_new]
        composition = None
        if has_rule:
            fs, fe = rule_times
            composition = conn.execute(text(f"""
                SELECT
                    count(*)                                                    AS rows_in_scope,
                    count(*) FILTER (WHERE s0 AND e0)                           AS both_date_only,
                    count(*) FILTER (WHERE s0 AND NOT e0)                       AS start_only,
                    count(*) FILTER (WHERE NOT s0 AND e0)                       AS end_only,
                    count(*) FILTER (WHERE has_end AND adj_end < adj_start)     AS end_before_start,
                    count(*) FILTER (WHERE has_end AND adj_end < adj_start
                                     AND NOT (raw_end < raw_start))             AS newly_end_before_start
                FROM (
                    SELECT
                        COALESCE(r.calculated_trouble_date::time = TIME '00:00:00', false)    AS s0,
                        COALESCE(r.calculated_completion_date::time = TIME '00:00:00', false) AS e0,
                        (r.is_completed AND r.calculated_completion_date IS NOT NULL)          AS has_end,
                        r.calculated_trouble_date                                              AS raw_start,
                        r.calculated_completion_date                                           AS raw_end,
                        CASE WHEN r.calculated_trouble_date::time = TIME '00:00:00'
                             THEN r.calculated_trouble_date::date + TIME '{fs}'
                             ELSE r.calculated_trouble_date END                                AS adj_start,
                        CASE WHEN r.calculated_completion_date::time = TIME '00:00:00'
                             THEN r.calculated_completion_date::date + TIME '{fe}'
                             ELSE r.calculated_completion_date END                             AS adj_end
                    FROM cur.medical_device_repair_history r
                    WHERE r.is_failure = true
                      AND r.calculated_trouble_date IS NOT NULL
                      AND r.medical_device_ledger_id IS NOT NULL
                ) t
            """)).one()

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
                months = len(contribution[r["id"]])
                hours_nr = sum(contribution_nr[r["id"]].values())
                note.append(f"MOVED: removes {hours:.2f}h over {months} month(s) (without the 00:00 rule: {hours_nr:.2f}h)"
                            if r in left_failure
                            else f"MOVED: adds {hours:.2f}h over {months} month(s) (without the 00:00 rule: {hours_nr:.2f}h)")
                if r["id"] in contribution and not contribution[r["id"]]:
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
    print("=== entry 9 ダウンタイムの項: 前後 / downtime term, before vs after ===")
    print(f"  rows that left failure : {len(left_failure)}   rows that joined failure: {len(joined_failure)} (expect 0)")
    invisible = [r for r in left_failure if not contribution[r['id']]]
    print(f"  of those leaving failure, NOT visible in the fact: {len(invisible)}")
    print(f"  (device, month) cells changed: {len(changed_cells)}")
    removed_total_nr = sum(sum(contribution_nr[r["id"]].values()) for r in left_failure)
    print(f"  total downtime hours removed : {removed_total:,.2f}  (without the 00:00 rule: {removed_total_nr:,.2f})")
    zero_vanished = [c for c in vanished if before_fact[c] == 0.0]
    print(f"  cells that exist BEFORE but not AFTER: {len(vanished)} "
          f"(of which 0.00 h cells: {len(zero_vanished)}); cells that appear AFTER only: {len(appeared)} (expect 0)")
    for cell in vanished:
        print(f"    vanishes: {str(cell):<28} before={before_fact[cell]:.2f} h, rows={touching.get(cell, [])}")
    print()
    if changed_cells:
        print("  cell (ledger, month) | before -> after | removed by rows")
        for cell in changed_cells:
            b = before_fact.get(cell, 0.0)
            a = after_fact.get(cell, 0.0)
            gone = "  (cell disappears)" if a == 0.0 and cell not in after_fact else ""
            print(f"    {str(cell):<28} {b:>10.2f} -> {a:>10.2f}   rows={touching.get(cell, [])}{gone}")
        print()

    print("=== pass condition: every changed cell is explained by the rows that left failure ===")
    print(f"  cells where AFTER != BEFORE - (rows that left failure): {len(unexplained)}  (expect 0)")
    for cell in unexplained:
        print(f"    UNEXPLAINED {cell}: before={before_fact.get(cell, 0.0):.2f} "
              f"after={after_fact.get(cell, 0.0):.2f} removed={removed.get(cell, 0.0):.2f}")
    print()

    print("=== 確認: 今の cur.monthly_failure_downtime は BEFORE と一致しているか / is the real table equal to BEFORE? ===")
    if not table_exists:
        print("  cur.monthly_failure_downtime does not exist (nothing to compare)")
    else:
        print(f"  cells differing from the table (excluding NULL ledger): {len(stale)}  (expect 0; non-zero means "
              f"the table was built before the latest curate run or is otherwise stale)")
        for cell in sorted(stale, key=lambda c: c[1] if c[0] is None else (c[0], c[1]))[:20]:
            print(f"    {str(cell):<28} real table={real.get(cell, 0.0):>10.2f} h   BEFORE (computed now, pre-rule calculation)={before_fact_nr.get(cell, 0.0):>10.2f} h")
        print(f"  NULL-ledger cells still in the real table: {len(stale_null)} "
              f"(expected until the table is rebuilt with the IS NOT NULL condition)")
        print(f"  cells that would differ if the table were built with the current SQL (rule on), "
              f"excluding NULL ledger: {len(stale_vs_new):,}  (expected: the cells touched by the 00:00 rule, see below)")
    print()

    print("=== SQL 変更の効果: IS NOT NULL の前後 (全テーブル) / effect of the SQL change, whole table ===")
    if not has_ledger_filter:
        print("  the SQL file does not contain the IS NOT NULL condition, so before = after here")
    print(f"  failure rows with ledger_id NULL (trouble_date set): {null_rows_total:,}")
    print(f"  fact without the condition: {len(whole_old):,} cells / {old_hours_total:,.2f} h")
    print(f"  fact with the condition   : {len(whole_new):,} cells / {new_hours_total:,.2f} h")
    print(f"  cells dropped by the condition: {len(dropped):,} / {dropped_hours:,.2f} h "
          + (f"({dropped_hours / old_hours_total:.1%} of hours before), months {min(dropped_months)} .. {max(dropped_months)}"
             if dropped and old_hours_total else ""))
    print(f"  dropped cells that are NOT NULL-ledger: {len(dropped_non_null)}  (expect 0)")
    print(f"  remaining cells whose hours changed   : {len(changed_other)}  (expect 0)")
    print()

    print("=== 00:00 ルールの効果 (全テーブル) / effect of the 00:00 rule, whole table ===")
    if not has_rule:
        print("  the SQL file does not contain the date-only rule, so before = after here")
    else:
        print(f"  times used: start 00:00 -> {rule_times[0]}, completion 00:00 -> {rule_times[1]}")
        print(f"  fact without the rule: {len(whole_nr):,} cells / {sum(whole_nr.values()):,.2f} h")
        print(f"  fact with the rule   : {len(whole_new):,} cells / {sum(whole_new.values()):,.2f} h")
        print(f"  cells whose hours changed: {len(rule_changed):,}; total hours change: {rule_hours_delta:+,.2f} h; "
              f"cells only with the rule: {len(rule_cells_new_only):,}; cells only without it: {len(rule_cells_old_only):,}")
        print(f"  failure repairs in the fact's scope: {composition.rows_in_scope:,}")
        print(f"    both sides date-only : {composition.both_date_only:,}")
        print(f"    start date-only only : {composition.start_only:,}")
        print(f"    end date-only only   : {composition.end_only:,}")
        print(f"  completed repairs whose end is before the start after the replacement (counted as 0 h): "
              f"{composition.end_before_start:,} (of which caused by the rule: {composition.newly_end_before_start:,})")
    print()

    print("=== 追加の観察 / side observations ===")
    print(f"  rows containing セル交換 with ledger_id NULL: {sum(1 for r in rows if r['ledger'] is None)} "
          f"(these are outside the fact now, whatever their classification)")
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

    return 1 if (unexplained or joined_failure or dropped_non_null or changed_other) else 0


if __name__ == "__main__":
    raise SystemExit(main())