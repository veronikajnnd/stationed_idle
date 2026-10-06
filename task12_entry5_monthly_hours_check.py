"""
task12 entry 9 (貸出余力の拘束時間) の貸出時間の項: entry5_monthly_rental_count.sql を新しい形 (貸出 x 月ごとに 1 行、
識別子と rental_hours を持つ、回数の列なし) にしたことの、実データでの確認 (読み取り専用)。

Real-data check (read-only) of the rental-hours term of task12 entry 9: entry5_monthly_rental_count.sql was
changed to the new shape (one row per rental per month, with the identifier and rental_hours, no count column).

何を見るか / What is checked:
    1. 検算 (monthly_fact_template.md §4): 開始日と ledger id を持つ貸出は、すべて 1 行以上ある。(貸出, 月) は重複しない。
    2. 旧い形との違い: 旧い形 (client_device_number、開始月だけ、回数の列) の行数・貸出数と、新しい形の行数・貸出数。
       NULL の ledger id の貸出が、新しい形では fact に入らない件数。
    3. 時間の決め方の確認: 返却済みの貸出について、新しい形の 1 件ごとの合計時間を、保存済みの rental_duration_hours と比べる。
       equal = 一致、one_day_more = 保存済みの列は返却日を数えない (この SQL は数える)、それ以外は件数と例を出す。
    4. 未完了の貸出 (返却日なし): 件数、そこから来る行数と時間。古い未完了の貸出は今月まで毎月 1 行を作る。
    5. 1 つの (機器, 月) の貸出時間が、その月の時間数を超えるセル (同じ機器の貸出が重なっている)。

    1. Cross-check (monthly_fact_template.md section 4): every rental with a start date and a ledger id has at least
       one row; (rental, month) never repeats.
    2. Difference from the old shape (client_device_number, start month only, a count column): rows and rentals of
       the old shape vs the new shape; how many rentals with a NULL ledger id no longer enter the fact.
    3. Hours convention: for returned rentals, the per-rental total of the new shape vs the stored
       rental_duration_hours. equal = same; one_day_more = the stored column does not count the return day (this
       SQL does); anything else is counted and sampled.
    4. Still-open rentals (no return date): how many, and how many rows and hours they make. A very old open
       rental makes one row per month up to now.
    5. Cells where one device's rental hours in a month exceed the hours of that month (overlapping rentals).

読み取り専用 / Read-only (重要 / important):
    entry5_monthly_rental_count.sql 自体は DROP TABLE と CREATE TABLE AS を実行するので「実行しない」。SELECT 本体だけを
    取り出し、普通の SELECT として (cur の元テーブルを直接読んで) 実行する。トランザクションは READ ONLY にしてあり、
    テーブルの作成・書き込み・DROP はできない。fact のロジックは SQL ファイルの 1 か所だけを読む (コピーしない)。
    旧い形の行数を数える SQL (2 番) だけは、比較のために旧い形の定義を書いてある。

    The SQL file itself runs DROP TABLE and CREATE TABLE AS, so it is NOT executed. Only its SELECT body is
    extracted and run as a plain SELECT (reading the real source table directly). The transaction is READ ONLY, so
    nothing can be created, written or dropped. The fact logic is read from the single SQL file, never copied. Only
    the query that counts the old shape's rows (item 2) states the old definition, for the comparison.

実行方法 / How to run (stationed_idle のルートで):
    python task12_entry5_rental_hours_check.py
    python task12_entry5_rental_hours_check.py --db-url postgresql+psycopg2://...
"""

import argparse
import re
from pathlib import Path

from sqlalchemy import create_engine, text

# fact テーブルを作る文の目印 / The statement that builds the fact table (anchor to find the SELECT body).
_CREATE_FACT = re.compile(r"CREATE\s+TABLE\s+cur\.monthly_rental_count\s+AS\s*", re.IGNORECASE)


def load_fact_select(sql_path: str) -> str:
    """CREATE TABLE ... AS の後ろの SELECT 本体を返す (DROP も CREATE も含まない)。

    Returns the SELECT body after CREATE TABLE ... AS (no DROP, no CREATE)."""
    sql_text = Path(sql_path).read_text(encoding="utf-8")
    m = _CREATE_FACT.search(sql_text)
    if m is None:
        raise ValueError("CREATE TABLE cur.monthly_rental_count AS が見つからない / not found in " + str(sql_path))
    body = sql_text[m.end():]
    cleaned = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    statement = cleaned.split(";", 1)[0].strip()
    if not statement.upper().startswith(("WITH", "SELECT")):
        raise ValueError("SELECT 本体が WITH / SELECT で始まらない / body does not start with WITH or SELECT")
    return statement


def main() -> int:
    p = argparse.ArgumentParser(description="task12 entry 9: 貸出時間の fact の実データ確認 (読み取り専用)")
    p.add_argument("--db-url", default=None)
    p.add_argument("--sql", default="entry5_monthly_rental_count.sql",
                   help="fact の SELECT を読む SQL ファイル (実行はしない。SELECT 本体だけ使う)")
    args = p.parse_args()

    fact_select = load_fact_select(args.sql)
    if args.db_url:
        db_url = args.db_url
    else:
        from streamedix_common.config import get_database_url

        db_url = get_database_url()

    engine = create_engine(db_url)
    with engine.connect() as conn:
        # READ ONLY: 書き込みはできない / nothing can be written
        conn.execute(text("SET TRANSACTION READ ONLY"))
        fact = f"WITH fact AS (\n{fact_select}\n)"

        # --- 1. 検算 / cross-check ---
        src = conn.execute(text("""
            SELECT
                count(*) AS all_rows,
                count(*) FILTER (WHERE calculated_rental_start_date IS NULL) AS null_start,
                count(*) FILTER (WHERE medical_device_ledger_id IS NULL) AS null_ledger,
                count(*) FILTER (WHERE medical_device_ledger_id IS NULL
                                 AND calculated_rental_start_date IS NOT NULL) AS null_ledger_with_start,
                count(*) FILTER (WHERE calculated_rental_start_date IS NOT NULL
                                 AND medical_device_ledger_id IS NOT NULL) AS in_scope,
                count(*) FILTER (WHERE calculated_return_date IS NULL
                                 AND calculated_rental_start_date IS NOT NULL
                                 AND medical_device_ledger_id IS NOT NULL) AS open_in_scope
            FROM cur.medical_device_rental_history
        """)).one()
        fct = conn.execute(text(fact + """
            SELECT count(*) AS fact_rows,
                   count(DISTINCT medical_device_rental_history_id) AS fact_rentals,
                   coalesce(sum(rental_hours), 0) AS total_hours,
                   count(*) FILTER (WHERE rental_hours = 0) AS zero_hour_rows,
                   count(*) FILTER (WHERE recipient_department IS NULL) AS null_department_rows
            FROM fact
        """)).one()
        dup = conn.execute(text(fact + """
            SELECT count(*) FROM (
                SELECT medical_device_rental_history_id, month_start FROM fact
                GROUP BY 1, 2 HAVING count(*) > 1
            ) d
        """)).scalar()
        multi = conn.execute(text(fact + """
            SELECT count(*) FILTER (WHERE n > 1), coalesce(max(n), 0)
            FROM (SELECT count(*) AS n FROM fact GROUP BY medical_device_rental_history_id) t
        """)).one()
        print("=== 1. 検算 / cross-check (monthly_fact_template.md section 4) ===")
        print(f"  source rentals: {src.all_rows:,}  (NULL start date: {src.null_start:,}, NULL ledger id: {src.null_ledger:,})")
        print(f"  source rentals in scope (start date and ledger id present): {src.in_scope:,}")
        print(f"  fact: {fct.fact_rows:,} rows / {fct.fact_rentals:,} distinct rentals / {float(fct.total_hours):,.2f} h")
        print(f"  distinct rentals in the fact = rentals in scope: {fct.fact_rentals == src.in_scope}  (expect True)")
        print(f"  (rental, month) pairs that repeat: {dup:,}  (expect 0)")
        print(f"  rentals with more than one row: {multi[0]:,}; the most months for one rental: {multi[1]:,}")
        print(f"  rows with 0 hours: {fct.zero_hour_rows:,}; rows with a NULL department: {fct.null_department_rows:,}")
        print()

        # --- 2. 旧い形との違い / difference from the old shape ---
        old = conn.execute(text("""
            SELECT count(*) AS old_rows, coalesce(sum(n), 0) AS old_rentals
            FROM (
                SELECT count(*) AS n
                FROM cur.medical_device_rental_history r
                WHERE r.calculated_rental_start_date IS NOT NULL
                GROUP BY r.client_device_number, r.medical_facility_id, r.recipient_department,
                         date_trunc('month', r.calculated_rental_start_date)
            ) g
        """)).one()
        print("=== 2. 旧い形との違い / difference from the old shape ===")
        print(f"  old shape (device number x facility x department x start month, a count column): "
              f"{old.old_rows:,} rows / {int(old.old_rentals):,} rentals counted")
        print(f"  new shape: {fct.fact_rows:,} rows / {fct.fact_rentals:,} distinct rentals")
        left = int(old.old_rentals) - fct.fact_rentals
        print(f"  rentals in the old count but not in the new fact: {left:,}  "
              f"(rentals with a start date and a NULL ledger id: {src.null_ledger_with_start:,}; "
              f"expect the two numbers to be equal)")
        print()

        # --- 3. 時間の決め方 / hours convention ---
        conv = conn.execute(text(fact + """
            , f AS (
                SELECT medical_device_rental_history_id AS id, sum(rental_hours) AS fact_hours
                FROM fact GROUP BY 1
            )
            SELECT
                count(*) AS returned_rentals,
                count(*) FILTER (WHERE r.rental_duration_hours IS NULL) AS stored_null,
                count(*) FILTER (WHERE abs(f.fact_hours - r.rental_duration_hours) < 0.02) AS equal,
                count(*) FILTER (WHERE abs(f.fact_hours - 24 - r.rental_duration_hours) < 0.02) AS one_day_more,
                count(*) FILTER (WHERE r.rental_duration_hours IS NOT NULL
                                 AND abs(f.fact_hours - r.rental_duration_hours) >= 0.02
                                 AND abs(f.fact_hours - 24 - r.rental_duration_hours) >= 0.02) AS other
            FROM cur.medical_device_rental_history r
            JOIN f ON f.id = r.medical_device_rental_history_id
            WHERE r.calculated_return_date IS NOT NULL
        """)).one()
        print("=== 3. 時間の決め方 / hours convention: fact total per rental vs stored rental_duration_hours ===")
        print(f"  returned rentals in the fact: {conv.returned_rentals:,}")
        print(f"  equal: {conv.equal:,}; one_day_more (stored column does not count the return day): "
              f"{conv.one_day_more:,}; stored NULL: {conv.stored_null:,}; other: {conv.other:,}")
        if conv.other:
            sample = conn.execute(text(fact + """
                , f AS (
                    SELECT medical_device_rental_history_id AS id, sum(rental_hours) AS fact_hours
                    FROM fact GROUP BY 1
                )
                SELECT r.medical_device_rental_history_id, r.calculated_rental_start_date,
                       r.calculated_return_date, r.rental_duration_hours, f.fact_hours
                FROM cur.medical_device_rental_history r
                JOIN f ON f.id = r.medical_device_rental_history_id
                WHERE r.calculated_return_date IS NOT NULL
                  AND r.rental_duration_hours IS NOT NULL
                  AND abs(f.fact_hours - r.rental_duration_hours) >= 0.02
                  AND abs(f.fact_hours - 24 - r.rental_duration_hours) >= 0.02
                ORDER BY abs(f.fact_hours - r.rental_duration_hours) DESC
                LIMIT 8
            """)).fetchall()
            print("  largest differences (id, start, return, stored hours, fact hours):")
            for row in sample:
                print(f"    {row[0]}  {row[1]} -> {row[2]}  stored={row[3]}  fact={float(row[4]):.2f}")
        print()

        # --- 4. 未完了の貸出 / still-open rentals ---
        opn = conn.execute(text(fact + """
            SELECT count(DISTINCT f.medical_device_rental_history_id) AS open_rentals,
                   count(*) AS open_rows, coalesce(sum(f.rental_hours), 0) AS open_hours
            FROM fact f
            JOIN cur.medical_device_rental_history r
              ON r.medical_device_rental_history_id = f.medical_device_rental_history_id
            WHERE r.calculated_return_date IS NULL
        """)).one()
        by_year = conn.execute(text("""
            SELECT extract(year FROM calculated_rental_start_date)::int AS y, count(*)
            FROM cur.medical_device_rental_history
            WHERE calculated_return_date IS NULL AND calculated_rental_start_date IS NOT NULL
              AND medical_device_ledger_id IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)).fetchall()
        print("=== 4. 未完了の貸出 / still-open rentals (no return date) ===")
        print(f"  open rentals: {opn.open_rentals:,}; rows from them: {opn.open_rows:,} "
              f"({opn.open_rows / fct.fact_rows:.1%} of the fact rows if the fact has rows); "
              f"hours from them: {float(opn.open_hours):,.2f} "
              f"({(float(opn.open_hours) / float(fct.total_hours)) if fct.total_hours else 0:.1%} of the hours)")
        print("  open rentals by start year: " + ", ".join(f"{y}: {n:,}" for y, n in by_year))
        print()

        # --- 5. その月の時間数を超えるセル / cells above the hours of the month ---
        over = conn.execute(text(fact + """
            SELECT medical_device_ledger_id, month_start, sum(rental_hours) AS h,
                   count(*) AS n_rentals
            FROM fact
            GROUP BY 1, 2
            HAVING sum(rental_hours) > extract(epoch FROM
                   ((month_start::timestamp + interval '1 month') - month_start::timestamp)) / 3600.0 + 0.01
            ORDER BY h DESC
        """)).fetchall()
        print("=== 5. (device, month) cells whose rental hours exceed the hours of the month (overlapping rentals) ===")
        print(f"  cells: {len(over):,}")
        for row in over[:5]:
            print(f"    ledger={row[0]} month={row[1]}  {float(row[2]):,.2f} h from {row[3]} rentals")
        print()

    return 0 if (fct.fact_rentals == src.in_scope and dup == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())