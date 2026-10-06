"""
entry 5 の月次貸出 fact (entry5_monthly_rental_count.sql) の形と、entry 9 が使う貸出時間 (rental_hours) を決めるテスト。
Tests for the shape of entry 5's monthly rental fact and for the rental hours (rental_hours) that entry 9 uses.

出典 / Sources (各 test の期待値の根拠にした文書 / the documents each expectation is based on):
    - monthly_fact_template.md (Miyazawa-san, 2026-09-09):
        §2  1 行 = 1 つの実体の 1 か月分。3 か月にまたがる記録は 3 行 (同じ識別子)
        §3  識別子 (元レコードの id) を持つ。COUNT(...) AS ..._count の列は置かない (数えるのは Superset)
        §4  回数 = 時間の情報の数 (貸出回数の fact と貸出時間の fact は同じ行を持つ)
        §5  worked example: 貸出 2026-03-31 -> 2026-04-02 は 2 行、3 月 24.0 h、4 月 48.0 h (返却日の日も数える)
        §6  機器のキーは medical_device_ledger_id、識別子は medical_device_rental_history_id。
            ADR-2026-09-09 の適用までは medical_device_ledger_id IS NOT NULL を明示する
    - poc_metric_definition.md: 未完了レコードのルール (2026-09-03、月末で切って含める、除外しない)、
      entry 9 / 19 / 33 (貸出時間は未完了の貸出も含めて月ごとに足す)。貸出は日付だけの記録で、
      2026-10-06 の修理の 09:00 / 17:00 ルールの対象外

何を試すか / What this tests:
    entry5_monthly_rental_count.sql の SELECT 本体 (load_fact_select で読む。コピーしない) を、合成した
    貸出行に対して実行し、月次 fact が上の文書どおりになるかを見る。テーブルは作らない: 一時テーブルに
    行を入れ、with_source() で fact の元テーブルをそれに差し替える。cur.* には触れない。
    貸出の日付列の型は実テーブルで確認していないので、timestamp と date の両方で同じ test を回す。

    Runs the SELECT body of entry5_monthly_rental_count.sql (read through load_fact_select, never copied)
    against synthetic rental rows and checks the monthly fact matches the documents above. No table is
    created: rows go into a temp table and with_source() swaps the fact's source table for it. cur.* is
    never touched. The type of the rental date columns was not confirmed on the real table, so every test
    runs with both timestamp and date columns.

実行方法 / How to run (stationed_idle のルートで。DB が必要):
    pytest test_entry5_monthly_rental_fact.py -v

    接続先は TEST_DATABASE_URL があればそれ、無ければ streamedix_common.config.get_database_url()。
    書き込むのはセッション内の一時テーブル (t_rent) だけで、最後に必ず rollback する。接続できない場合は、
    全 test を skip する。

    The DB URL is TEST_DATABASE_URL when set, otherwise streamedix_common.config.get_database_url().
    Only a session-local temp table (t_rent) is written, and it is always rolled back. All tests skip
    when no connection can be made.
"""

import os
import re
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


# ---- SQL ファイルから fact の SELECT 本体だけを取り出す helper (この test ファイル内に置く) ----
# ---- Helper that extracts only the fact's SELECT body from the SQL file (kept inside this test file) ----
# 安全 / Safety: 元の SQL は DROP TABLE と CREATE TABLE AS を実行する。ここで返すのは AS の後ろの
# SELECT だけで DROP も CREATE も含まない (読み取り専用)。ロジックは SQL ファイルの1か所だけを読む。
# The source SQL runs DROP TABLE and CREATE TABLE AS. Only the SELECT after AS is returned (no DROP,
# no CREATE: read-only). The logic is read from the one SQL file, never copied.
# fact テーブルを作る文の目印 / The statement that builds the fact table (anchor to find the SELECT body).
_CREATE_FACT = re.compile(r"CREATE\s+TABLE\s+cur\.monthly_rental_count\s+AS\s*", re.IGNORECASE)

# SELECT 本体が読む元テーブル (CTE 名に差し替える対象)
# The source table the SELECT body reads from (replaced by a CTE name).
_SOURCE_TABLE = "cur.medical_device_rental_history"


def load_fact_select(sql_path: str) -> str:
    """CREATE TABLE ... AS の後ろの SELECT 本体 (WITH ... または SELECT ...) を返す。

    Returns the SELECT body (WITH ... or SELECT ...) that follows CREATE TABLE ... AS."""
    sql_text = Path(sql_path).read_text(encoding="utf-8")
    m = _CREATE_FACT.search(sql_text)
    if m is None:
        raise ValueError(
            "CREATE TABLE cur.monthly_rental_count AS が見つからない / not found in " + str(sql_path)
        )

    # 行コメント (--) を落としてから最初の ';' で切る。コメント内の ';' で誤って切れないようにするため。
    # このステートメントの範囲には文字列リテラル内の '--' は無い。
    # Strip -- line comments, then cut at the first ';' so a ';' inside a comment cannot end the
    # statement early. There is no '--' inside a string literal within this statement.
    body = sql_text[m.end():]
    cleaned = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    statement = cleaned.split(";", 1)[0].strip()

    if not statement.upper().startswith(("WITH", "SELECT")):
        raise ValueError("SELECT 本体が WITH / SELECT で始まらない / body does not start with WITH or SELECT")
    if _SOURCE_TABLE not in statement:
        raise ValueError(_SOURCE_TABLE + " を読んでいない / source table not referenced")
    return statement


def with_source(fact_select: str, source_sql: str, hist_name: str = "src") -> str:
    """fact の SELECT が読む元テーブルを、任意の SELECT (source_sql) に差し替えて返す。

    source_sql を先頭の CTE (hist_name) として置き、元の SQL 内の cur.medical_device_rental_history
    をその CTE 名に置き換える。本体が WITH で始まるときはその CTE の前に足し、SELECT で始まるときは
    WITH を前置する。テーブルを作らず・書き換えずに「この行たちを元テーブルだと思って fact を計算する」ため。

    Replaces the table the fact SELECT reads with an arbitrary SELECT (source_sql): source_sql becomes a
    leading CTE (hist_name) and every cur.medical_device_rental_history reference in the original SQL is
    rewritten to that CTE name. When the body starts with WITH the CTE is added in front of its own CTEs;
    when it starts with SELECT a WITH is put in front. This computes the fact "as if these rows were the
    source table" without creating or modifying any table."""
    body = fact_select.replace(_SOURCE_TABLE, hist_name)
    if body.upper().startswith("WITH"):
        rest = body[len("WITH"):].lstrip()
        return f"WITH {hist_name} AS (\n{source_sql}\n),\n{rest}"
    return f"WITH {hist_name} AS (\n{source_sql}\n)\n{body}"


_SQL_NAME = "entry5_monthly_rental_count.sql"


def _find_sql() -> str:
    """この test ファイルと同じフォルダ (stationed_idle のルート) の SQL を返す。
    環境変数 ENTRY5_SQL_PATH があればそれを優先する。

    Return the SQL file in the same folder as this test file (the stationed_idle root).
    The env var ENTRY5_SQL_PATH takes priority when set."""
    override = os.environ.get("ENTRY5_SQL_PATH")
    if override:
        return override
    return str(Path(__file__).resolve().with_name(_SQL_NAME))


_SQL_PATH = _find_sql()

# 一時テーブルの列は、fact の SELECT が実際に読む列だけ。日付列の型は {col_type} (timestamp か date)
# Temp-table columns are only those the fact SELECT reads. The date columns have type {col_type}.
_CREATE_TEMP = """
CREATE TEMP TABLE t_rent (
    medical_device_rental_history_id bigint,
    medical_device_ledger_id         bigint,
    medical_facility_id              integer,
    recipient_department             text,
    client_device_number             text,
    calculated_rental_start_date     {col_type},
    calculated_return_date           {col_type},
    is_returned                      boolean
) ON COMMIT DROP
"""


@pytest.fixture(params=["timestamp", "date"])
def col_type(request):
    """貸出の日付列の型 (timestamp と date の両方で回す)。
    The type of the rental date columns (every test runs with both)."""
    return request.param


@pytest.fixture
def conn(col_type):
    """DB 接続を返す。一時テーブルを作り、test の最後に必ず rollback する。接続できなければ skip。

    Yields a DB connection with the temp table created; always rolled back at the end of the test.
    Skips when no connection can be made."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        try:
            from streamedix_common.config import get_database_url

            url = get_database_url()
        except Exception as exc:  # 設定が読めない / config unavailable
            pytest.skip(f"no database URL available: {exc}")
    engine = create_engine(url)
    try:
        c = engine.connect()
    except Exception as exc:  # 接続できない / cannot connect
        engine.dispose()
        pytest.skip(f"cannot connect to the database: {exc}")
    try:
        c.execute(text(_CREATE_TEMP.format(col_type=col_type)))
        yield c
    finally:
        c.rollback()
        c.close()
        engine.dispose()


def _insert(conn, rows):
    """合成した貸出行を一時テーブルに入れる。rows は dict のリスト。id は省略すると 1 から連番。
    ledger を省略すると 1、facility は 1、department は 'ICU'。

    Insert synthetic rental rows into the temp table. rows is a list of dicts. id defaults to 1, 2, ...;
    ledger defaults to 1, facility to 1, department to 'ICU'."""
    for i, r in enumerate(rows, start=1):
        conn.execute(
            text(
                """INSERT INTO t_rent VALUES
                   (:id, :ledger, :facility, :dept, :device, :start, :ret, :is_returned)"""
            ),
            {
                "id": r.get("id", i),
                "ledger": r["ledger"] if "ledger" in r else 1,
                "facility": r.get("facility", 1),
                "dept": r["dept"] if "dept" in r else "ICU",
                "device": r.get("device", "CV181"),
                "start": r.get("start"),
                "ret": r.get("ret"),
                "is_returned": r.get("is_returned", r.get("ret") is not None),
            },
        )


def _run(conn):
    """fact の SELECT を一時テーブルに対して実行し、(列名のリスト, 行の dict のリスト) を返す。

    Run the fact SELECT against the temp table and return (column names, list of row dicts)."""
    sql = with_source(load_fact_select(_SQL_PATH), "SELECT * FROM t_rent")
    # 実テーブルを読まないことの確認 (差し替えが効いていなければここで止める)
    # Make sure the real table is not read (stop here if the swap did not take effect).
    assert _SOURCE_TABLE not in sql.split("\n", 2)[2], "source table was not swapped out"
    result = conn.execute(text(sql))
    cols = list(result.keys())
    rows = []
    for rec in result.fetchall():
        row = dict(zip(cols, rec))
        if row.get("month_start") is not None:
            row["month"] = row["month_start"].strftime("%Y-%m")
        rows.append(row)
    return cols, rows


def _hours_by_id_month(conn):
    """{(識別子, 'YYYY-MM'): rental_hours}。rental_hours 列が無い (古い形) ときは値が None になり、
    test は「列が無い」ことを assert の失敗として示す。

    {(identifier, 'YYYY-MM'): rental_hours}. When the rental_hours column does not exist (the old shape)
    the value is None, so a test shows "no such column" as a failed assertion."""
    _, rows = _run(conn)
    out = {}
    for row in rows:
        key = (row.get("medical_device_rental_history_id"), row["month"])
        out[key] = None if row.get("rental_hours") is None else float(row["rental_hours"])
    return out


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d")


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# --- 1. 1 か月に収まる貸出は、その月の 1 行になる。返却日の日も数える ---
# --- 1. A rental inside one month is one row; the return day is counted too ---

def test_rental_inside_one_month_is_one_row_with_inclusive_days(conn):
    """出典: monthly_fact_template.md §5 (worked example は開始日も返却日も 1 日まるごと数える)。
    3/10 から 3/12 は 3 日 = 72 時間。行は識別子 (貸出の id) と機器のキー (ledger) を持つ。

    Source: monthly_fact_template.md section 5 (the worked example counts the start day and the return
    day in full). 3/10 to 3/12 is 3 days = 72 h. The row carries the identifier (the rental's id) and
    the device key (ledger)."""
    _insert(conn, [{"id": 90114, "ledger": 4821, "start": _d("2026-03-10"), "ret": _d("2026-03-12")}])
    _, rows = _run(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row.get("medical_device_ledger_id") == 4821
    assert row.get("medical_device_rental_history_id") == 90114
    assert row["month"] == "2026-03"
    assert float(row["rental_hours"]) == pytest.approx(72.0)


# --- 2. 月をまたぐ貸出は月ごとの行に分かれ、識別子は同じ。template の worked example そのもの ---
# --- 2. A month-spanning rental splits into one row per month with the same identifier (the template's example) ---

def test_template_worked_example_two_rows_with_the_same_identifier(conn):
    """出典: monthly_fact_template.md §5。CV181 の 2026-03-31 -> 2026-04-02 は 2 行。3 月 24.0 h、
    4 月 48.0 h。識別子は同じ。1 件の貸出の月ごとの合計 (72 h) は、貸出全体の長さ (3 日) に等しい。

    Source: monthly_fact_template.md section 5. CV181 2026-03-31 -> 2026-04-02 is 2 rows: March 24.0 h,
    April 48.0 h, same identifier. The sum over the months (72 h) equals the whole rental (3 days)."""
    _insert(conn, [{"id": 90114, "ledger": 4821, "start": _d("2026-03-31"), "ret": _d("2026-04-02")}])
    assert _hours_by_id_month(conn) == {
        (90114, "2026-03"): pytest.approx(24.0),
        (90114, "2026-04"): pytest.approx(48.0),
    }


def test_rental_spanning_three_months_has_a_full_middle_month(conn):
    """出典: monthly_fact_template.md §2 (3 か月にまたがる記録は 3 行)。3/30 -> 5/2: 3 月 48 h (3/30 と 3/31)、
    4 月 720 h (30 日まるごと)、5 月 48 h (5/1 と 5/2)。

    Source: monthly_fact_template.md section 2 (a record spanning three months is three rows).
    3/30 -> 5/2: March 48 h (3/30 and 3/31), April 720 h (the whole month), May 48 h (5/1 and 5/2)."""
    _insert(conn, [{"id": 7, "start": _d("2026-03-30"), "ret": _d("2026-05-02")}])
    assert _hours_by_id_month(conn) == {
        (7, "2026-03"): pytest.approx(48.0),
        (7, "2026-04"): pytest.approx(720.0),
        (7, "2026-05"): pytest.approx(48.0),
    }


def test_rental_across_a_year_end_splits_into_december_and_january(conn):
    """出典: monthly_fact_template.md §2。年をまたぐ貸出 (12/30 -> 1/2): 12 月 48 h、1 月 48 h。
    Source: monthly_fact_template.md section 2. A rental across the year end (12/30 -> 1/2): December
    48 h, January 48 h."""
    _insert(conn, [{"id": 8, "start": _d("2025-12-30"), "ret": _d("2026-01-02")}])
    assert _hours_by_id_month(conn) == {
        (8, "2025-12"): pytest.approx(48.0),
        (8, "2026-01"): pytest.approx(48.0),
    }


def test_same_day_rental_counts_the_whole_day(conn):
    """出典: monthly_fact_template.md §5 (返却日の日も数える)。3/10 に借りて 3/10 に返した貸出は 24 時間の 1 行
    (0 時間の行ではない)。貸出は日付だけの記録なので、修理の 09:00 / 17:00 ルールは使わない。

    Source: monthly_fact_template.md section 5 (the return day is counted). A rental out and back on
    3/10 is one 24 h row (not a 0 h row). Rentals are date-only records, so the repair 09:00 / 17:00
    rule is not used."""
    _insert(conn, [{"id": 9, "start": _d("2026-03-10"), "ret": _d("2026-03-10")}])
    assert _hours_by_id_month(conn) == {(9, "2026-03"): pytest.approx(24.0)}


def test_a_rental_ending_on_the_last_day_of_a_month_makes_no_row_in_the_next_month(conn):
    """出典: monthly_fact_template.md §2 / §4 (行はその月に存在したときだけ。0 時間の行を作らない)。
    3/30 -> 3/31 の貸出は 3 月だけ (48 h)。4 月に 0 時間の行ができてはいけない。

    Source: monthly_fact_template.md sections 2 and 4 (a row exists only if the entity existed in that
    month; no 0 h filler rows). A rental 3/30 -> 3/31 is March only (48 h), with no 0 h row in April."""
    _insert(conn, [{"id": 10, "start": _d("2026-03-30"), "ret": _d("2026-03-31")}])
    assert _hours_by_id_month(conn) == {(10, "2026-03"): pytest.approx(48.0)}


# --- 3. 時刻が入っている返却は、そのまま使う (timestamp の列だけ) ---
# --- 3. A return with a recorded time is used as is (timestamp columns only) ---

def test_recorded_return_time_is_used_as_is(conn, col_type):
    """出典: 日付だけの記録 (時刻が ちょうど 00:00:00) だけが「その日の終わりまで」と読まれる。時刻が入っている
    返却 (3/10 08:00 -> 3/10 12:00) は 4 時間。date 型の列は時刻を持てないのでこの test は timestamp だけ。

    Source: only a date-only record (time exactly 00:00:00) is read as "until the end of that day". A
    return with a recorded time (3/10 08:00 -> 3/10 12:00) is 4 h. A date column cannot hold a time, so
    this test runs for timestamp only."""
    if col_type != "timestamp":
        pytest.skip("a date column cannot hold a time of day")
    _insert(conn, [{"id": 11, "start": _dt("2026-03-10 08:00"), "ret": _dt("2026-03-10 12:00")}])
    assert _hours_by_id_month(conn) == {(11, "2026-03"): pytest.approx(4.0)}


# --- 4. まだ返却されていない貸出は、除外せず、月末 (または現在) で切って含める ---
# --- 4. A still-open rental is included, capped at month end (or now), never excluded ---

def test_still_open_rental_is_included_and_capped(conn):
    """出典: poc_metric_definition.md の「未完了レコードのルール」(2026-09-03) と entry 19 (返却日が
    ある貸出だけに絞る条件は外した)。先月 1 日から未返却の貸出は、先月は月末まで (日数 x 24 時間)、
    今月は「今」までで切られた行になる。同じ識別子。返却日がある貸出と違い、「返却日の日を数える」
    調整は入らない。

    Source: the still-open record rule (2026-09-03) and entry 19 (the gate to rentals with a return date
    was removed). A rental open since the 1st of last month yields last month up to month end (days x 24
    h) and a row for this month capped at now, with the same identifier. Unlike a rental with a return
    date, no "count the return day" adjustment is made."""
    this_month, last_month = conn.execute(
        text("SELECT date_trunc('month', now())::timestamp, "
             "(date_trunc('month', now()) - interval '1 month')::timestamp")
    ).one()
    _insert(conn, [{"id": 12, "start": last_month, "ret": None, "is_returned": False}])
    hours = _hours_by_id_month(conn)

    days_last_month = (this_month - last_month).days
    assert hours[(12, last_month.strftime("%Y-%m"))] == pytest.approx(days_last_month * 24.0)

    elapsed_hours = conn.execute(
        text("SELECT extract(epoch FROM (localtimestamp - date_trunc('month', now())::timestamp)) / 3600.0")
    ).scalar()
    assert 0 <= hours[(12, this_month.strftime("%Y-%m"))] <= float(elapsed_hours) + 0.01


# --- 5. 識別子を持つので、回数は COUNT(DISTINCT id) で数えられる。回数の列は置かない ---
# --- 5. The identifier makes COUNT(DISTINCT id) possible; there is no count column ---

def test_distinct_identifiers_count_the_rentals_and_there_is_no_count_column(conn):
    """出典: monthly_fact_template.md §3 (COUNT(*) AS rental_count は置かない。識別子を持たせ、Superset が
    COUNT(DISTINCT) で数える) と §5。3 件の貸出 (1 件は 2 か月にまたがる) の行は 4 行になるが、識別子の
    種類は 3。3 月だけを見ると 3 件、4 月だけを見ると 1 件。rental_count の列は無い。

    Source: monthly_fact_template.md section 3 (no COUNT(*) AS rental_count; carry the identifier and let
    Superset COUNT(DISTINCT)) and section 5. Three rentals (one spans two months) give 4 rows but 3
    distinct identifiers; March alone has 3 rentals, April alone 1. There is no rental_count column."""
    _insert(conn, [
        {"id": 1, "start": _d("2026-03-05"), "ret": _d("2026-03-06")},
        {"id": 2, "start": _d("2026-03-20"), "ret": _d("2026-03-22"), "ledger": 2},
        {"id": 3, "start": _d("2026-03-31"), "ret": _d("2026-04-02")},
    ])
    cols, rows = _run(conn)
    assert "rental_count" not in cols
    assert len(rows) == 4
    ids = lambda month: {r.get("medical_device_rental_history_id") for r in rows if r["month"] == month}
    assert len({r.get("medical_device_rental_history_id") for r in rows}) == 3
    assert len(ids("2026-03")) == 3 and len(ids("2026-04")) == 1


def test_every_rental_has_at_least_one_row_and_one_row_per_identifier_and_month(conn):
    """出典: monthly_fact_template.md §4 (回数 = 時間の情報の数) と §2 (1 行 = 1 つの実体の 1 か月分)。元の貸出の
    識別子は全部 fact にあり、(識別子, 月) は重複しない。同じ機器・同じ月の 2 件の貸出は 2 行 (足し合わせない)。

    Source: monthly_fact_template.md section 4 (count = number of time rows) and section 2 (one row per
    entity per month). Every source identifier is in the fact, and (identifier, month) never repeats. Two
    rentals of the same device in the same month are two rows (not added together)."""
    _insert(conn, [
        {"id": 1, "start": _d("2026-03-05"), "ret": _d("2026-03-06")},
        {"id": 2, "start": _d("2026-03-05"), "ret": _d("2026-03-06")},
        {"id": 3, "start": _d("2026-03-31"), "ret": _d("2026-04-02")},
    ])
    _, rows = _run(conn)
    keys = [(r.get("medical_device_rental_history_id"), r["month"]) for r in rows]
    assert len(keys) == len(set(keys)) == 4
    assert {k[0] for k in keys} == {1, 2, 3}


# --- 6. 施設と部署は行に付く。部署が NULL でも行は消えない ---
# --- 6. Facility and department are carried; a NULL department does not drop the row ---

def test_facility_and_department_are_carried_and_null_department_is_kept(conn):
    """出典: monthly_fact_template.md §3 (キーに 機器・施設・部署・月)、poc_metric_definition.md entry 5
    (recipient_department)。施設 1 / ICU と 施設 2 / 部署 NULL の貸出は、それぞれの値を持った行になる。

    Source: monthly_fact_template.md section 3 (keys: device, facility, department, month) and
    poc_metric_definition.md entry 5 (recipient_department). Rentals of facility 1 / ICU and facility 2 /
    NULL department become rows carrying their own values."""
    _insert(conn, [
        {"id": 1, "facility": 1, "dept": "ICU", "start": _d("2026-03-05"), "ret": _d("2026-03-06")},
        {"id": 2, "facility": 2, "dept": None, "ledger": 2, "start": _d("2026-03-05"), "ret": _d("2026-03-06")},
    ])
    _, rows = _run(conn)
    by_id = {r.get("medical_device_rental_history_id"): r for r in rows}
    assert (by_id[1]["medical_facility_id"], by_id[1]["recipient_department"]) == (1, "ICU")
    assert (by_id[2]["medical_facility_id"], by_id[2]["recipient_department"]) == (2, None)


# --- 7. fact に入らない貸出 ---
# --- 7. Rentals that do not enter the fact ---

def test_rental_not_linked_to_the_ledger_is_excluded(conn):
    """出典: monthly_fact_template.md §6 (ADR-2026-09-09 の適用までは medical_device_ledger_id IS NOT NULL を
    明示する)。台帳に紐付かない貸出は、どの機器の貸出時間でもない。ADR の適用後に列が NOT NULL になれば、
    この条件は SQL からも、この test からも外してよい。

    Source: monthly_fact_template.md section 6 (until ADR-2026-09-09 is applied, write
    medical_device_ledger_id IS NOT NULL explicitly). A rental not linked to the ledger is the rental time
    of no device. Once the ADR is applied and the column is NOT NULL, the condition and this test can go."""
    _insert(conn, [{"id": 1, "ledger": None, "start": _d("2026-03-05"), "ret": _d("2026-03-06")}])
    _, rows = _run(conn)
    assert rows == []


def test_rental_without_a_start_date_is_excluded(conn):
    """出典: SQL の前提 (calculated_rental_start_date IS NOT NULL)。月を決められない貸出は数えられない。
    Source: the SQL's own precondition; a rental with no start cannot be placed in any month."""
    _insert(conn, [{"id": 1, "start": None, "ret": _d("2026-03-06")}])
    _, rows = _run(conn)
    assert rows == []


def test_return_before_start_keeps_the_rental_with_zero_hours(conn):
    """出典: monthly_fact_template.md §4 (貸出は必ず行を持つ。記録が壊れていても識別子を落とさない)。返却日が
    開始日より前の貸出は、開始月に 0 時間の 1 行になる (負の時間にも、行が消えるのにもならない)。

    Source: monthly_fact_template.md section 4 (every rental has a row; a broken record must not lose its
    identifier). A rental whose return date is before its start becomes one 0 h row in the start month
    (neither negative hours nor a missing row)."""
    _insert(conn, [{"id": 13, "start": _d("2026-03-10"), "ret": _d("2026-02-20")}])
    assert _hours_by_id_month(conn) == {(13, "2026-03"): pytest.approx(0.0)}