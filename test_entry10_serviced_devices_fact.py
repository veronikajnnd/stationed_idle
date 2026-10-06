"""
entry 10 (*平均リカバリ時間（保守履歴）) が entry 4 の月次ダウンタイム fact から読むもの (分子の時間 と
分母の「保守した機器の台数」) を守るテスト。
Guard tests for what entry 10 (mean recovery time, maintenance history) reads from entry 4's monthly
downtime fact: the numerator hours and the denominator "number of serviced devices".

出典 / Sources (各 test の期待値の根拠にした文書 / the documents each expectation is based on):
    - poc_metric_definition.md entry 10 (2026-09-03 の決定): オンプレは「機器 x 月ごとの、修理があったか
      (whether there was a repair)、何時間止まったか (how many hours stopped)」を出す。分母
      (*機器台数（保守対象）= 選んだ期間に修理があった機器) は Superset が数える。条件: ADR-2026-06-16 の
      failure だけを使い、点検の時間を混ぜない。分子の時間は entry 4 と同じ未完了レコードのルール。
    - poc_metric_definition.md 日付だけの修理時刻のルール (2026-10-06): entry 10 も対象
    - monthly_fact_template.md §2 (粒度 = 実体 x 月、1つの (実体, 月) は1行)、§6/§7 (ledger_id IS NOT NULL)

何を試すか / What this tests:
    entry 10 には、entry 4 の fact SQL とは別の SQL が無い (分子の時間も、分母を数える元の行も、同じ
    entry4_monthly_failure_downtime.sql)。この SQL の SELECT 本体 (load_fact_select で読む。コピーしない)
    を合成した修理行に対して実行し、次を確かめる:
        - 同じ機器・同じ月の修理は1行にまとまり、時間は足し算になる (分子 = 修理記録の停止時間の合計)
        - 修理があった (機器, 月) は、時間が 0 でも1行ある (機器が「修理された」と数えられるため)
        - 点検などの時間は、同じ (機器, 月) にあっても混ざらない。点検しかない月に行はできない
        - 3か月以上にまたがる修理は、触れた月すべてに行ができる
    (月ごとの機器台数は、その月の行数として読める。Superset が実際にどう数えるかは未確認。)
    entry 9 の test と同じく、テーブルは作らず、cur.* にも触れない (一時テーブルを使う)。

    There is no SQL of its own for entry 10 (the numerator hours and the rows the denominator is
    counted from both come from entry4_monthly_failure_downtime.sql). The SELECT body of that SQL is run
    against synthetic repair rows (read through load_fact_select, never copied) and checks: repairs of
    one device in one month collapse into one row with summed hours (numerator); a (device, month) with a
    repair has a row even at 0 h (so the device counts as serviced); non-failure time is never mixed in
    and a month with only an inspection gets no row; a repair spanning 3+ months gets a row in each
    month it touches. (Devices serviced in a month can be read as the number of rows of that month; how
    Superset actually counts them is not verified.) As in the entry 9 tests, no table is created and
    cur.* is never touched (a temp table is used).

実行方法 / How to run (stationed_idle のルートで。DB が必要):
    pytest test_entry10_serviced_devices_fact.py -v

    接続先は TEST_DATABASE_URL があればそれ、無ければ streamedix_common.config.get_database_url()。
    書き込むのはセッション内の一時テーブル (t_hist) だけで、最後に必ず rollback する。接続できない場合は、
    全 test を skip する。

    The DB URL is TEST_DATABASE_URL when set, otherwise streamedix_common.config.get_database_url().
    Only a session-local temp table (t_hist) is written, and it is always rolled back. All tests skip
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


_SQL_NAME = "entry4_monthly_failure_downtime.sql"


def _find_sql() -> str:
    """この test ファイルと同じフォルダ (stationed_idle のルート) の SQL を返す。
    環境変数 ENTRY4_SQL_PATH があればそれを優先する。

    Return the SQL file in the same folder as this test file (the stationed_idle root).
    The env var ENTRY4_SQL_PATH takes priority when set."""
    override = os.environ.get("ENTRY4_SQL_PATH")
    if override:
        return override
    return str(Path(__file__).resolve().with_name(_SQL_NAME))


_SQL_PATH = _find_sql()

# 一時テーブルの列は、fact の SELECT が実際に読む列だけ (元テーブルの全列は要らない)
# Temp-table columns are only those the fact SELECT actually reads.
_CREATE_TEMP = """
CREATE TEMP TABLE t_hist (
    medical_device_repair_history_id bigint,
    medical_device_ledger_id         bigint,
    calculated_trouble_date          timestamp,
    calculated_completion_date       timestamp,
    is_completed                     boolean,
    calculated_downtime_hours        numeric,
    is_failure                       boolean,
    repair_classification            text
) ON COMMIT DROP
"""


@pytest.fixture
def conn():
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
        c.execute(text(_CREATE_TEMP))
        yield c
    finally:
        c.rollback()
        c.close()
        engine.dispose()


def _insert(conn, rows):
    """合成した修理行を一時テーブルに入れる。rows は dict のリスト。

    Insert synthetic repair rows into the temp table. rows is a list of dicts."""
    for i, r in enumerate(rows, start=1):
        conn.execute(
            text(
                """INSERT INTO t_hist VALUES
                   (:id, :ledger, :trouble, :completion, :is_completed, NULL, :is_failure, :cls)"""
            ),
            {
                "id": r.get("id", i),
                "ledger": r.get("ledger"),
                "trouble": r.get("trouble"),
                "completion": r.get("completion"),
                "is_completed": r.get("is_completed", r.get("completion") is not None),
                "is_failure": r["is_failure"],
                "cls": r.get("cls", "failure" if r["is_failure"] else "maintenance"),
            },
        )


def _fact(conn):
    """fact の SELECT を一時テーブルに対して実行し、{(ledger, 'YYYY-MM'): 時間} を返す。

    Run the fact SELECT against the temp table and return {(ledger, 'YYYY-MM'): hours}."""
    sql = with_source(load_fact_select(_SQL_PATH), "SELECT * FROM t_hist")
    # 実テーブルを読まないことの確認 (差し替えが効いていなければここで止める)
    # Make sure the real table is not read (stop here if the swap did not take effect).
    assert _SOURCE_TABLE not in sql.split("\n", 2)[2], "source table was not swapped out"
    out = {}
    for ledger, month_start, hours in conn.execute(text(sql)).fetchall():
        out[(ledger, month_start.strftime("%Y-%m"))] = float(hours)
    return out


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")



# fact の SELECT を実行して、行そのもの (重複も含めて) をリストで返す。_fact は dict にするので、
# 同じ (機器, 月) が2行出ても気づけない。分母を数える test では、行の重複を見る必要がある。
# Run the fact SELECT and return the raw rows as a list (duplicates included). _fact builds a dict, so
# it would not notice the same (device, month) coming out twice; the denominator tests need to see that.
def _fact_rows(conn):
    sql = with_source(load_fact_select(_SQL_PATH), "SELECT * FROM t_hist")
    assert _SOURCE_TABLE not in sql.split("\n", 2)[2], "source table was not swapped out"
    return [
        (ledger, month_start.strftime("%Y-%m"), float(hours))
        for ledger, month_start, hours in conn.execute(text(sql)).fetchall()
    ]


# その月に修理があった機器の台数 = その月の行数。entry 10 の分母 (*機器台数（保守対象）) を fact から
# 読むときの一例。Superset が実際にどう数えるかは未確認 (poc_metric_definition.md は分母を Superset の仕事とする)。
# Devices serviced in a month = number of rows of that month. One way to read entry 10's denominator
# from the fact; how Superset actually counts is not verified (the document makes the denominator
# Superset's job).
def _serviced_devices(conn, month):
    return sum(1 for _, m, _ in _fact_rows(conn) if m == month)


# --- 1. 同じ機器・同じ月の修理は1行にまとまり、時間は足し算になる ---
# --- 1. Repairs of one device in one month collapse into one row, hours are summed ---

def test_two_failure_repairs_of_one_device_in_one_month_make_one_row_with_summed_hours(conn):
    """出典: poc_metric_definition.md entry 10 の分子 (Total downtime across repair records in scope) と
    monthly_fact_template.md §2 (1つの (実体, 月) は1行)。4 時間 + 10 時間 = 14 時間、行は1つ。

    Source: entry 10's numerator (total downtime across repair records in scope) and
    monthly_fact_template.md section 2 (one row per (entity, month)). 4 h + 10 h = 14 h, one row."""
    _insert(conn, [
        {"ledger": 1, "trouble": _dt("2026-03-03 08:00"), "completion": _dt("2026-03-03 12:00"), "is_failure": True},
        {"ledger": 1, "trouble": _dt("2026-03-10 08:00"), "completion": _dt("2026-03-10 18:00"), "is_failure": True},
    ])
    assert _fact_rows(conn) == [(1, "2026-03", pytest.approx(14.0))]


def test_overlapping_and_month_spanning_repairs_never_duplicate_a_device_month(conn):
    """出典: monthly_fact_template.md §2。分母は行数から読めるので、同じ (機器, 月) が2行に分かれると
    機器が二重に数えられる。重なる修理と月をまたぐ修理を混ぜても、(機器, 月) ごとに1行。

    Source: monthly_fact_template.md section 2. The denominator can be read from the row count, so a
    (device, month) split into two rows would count the device twice. Overlapping and month-spanning
    repairs mixed together still give one row per (device, month)."""
    _insert(conn, [
        {"ledger": 1, "trouble": _dt("2026-03-05 08:00"), "completion": _dt("2026-03-06 08:00"), "is_failure": True},
        {"ledger": 1, "trouble": _dt("2026-03-05 12:00"), "completion": _dt("2026-03-07 12:00"), "is_failure": True},
        {"ledger": 1, "trouble": _dt("2026-03-30 12:00"), "completion": _dt("2026-04-02 12:00"), "is_failure": True},
    ])
    rows = _fact_rows(conn)
    keys = [(ledger, month) for ledger, month, _ in rows]
    assert len(keys) == len(set(keys)) == 2
    # 3月 = 24 + 48 + 36 時間、4月 = 36 時間 (重なる修理も記録ごとに足す: 分子は記録の合計)
    # March = 24 + 48 + 36 h, April = 36 h (overlapping repairs are summed per record: the numerator
    # is the total over records)
    assert dict(((l, m), h) for l, m, h in rows) == {(1, "2026-03"): pytest.approx(108.0),
                                                     (1, "2026-04"): pytest.approx(36.0)}


# --- 2. 修理があった (機器, 月) は、時間が 0 でも1行ある ---
# --- 2. A (device, month) with a repair has a row even at 0 h ---

def test_repair_with_recorded_zero_hours_still_makes_a_row(conn):
    """出典: poc_metric_definition.md entry 10 (オンプレが出すのは「修理があったか」と「何時間止まったか」)。
    時刻が入った 10:30 から 10:30 の修理は 0 時間 (2026-10-06 のルール) だが、修理はあった。機器は
    「修理された機器」として数えられるので、行は残る。

    Source: entry 10 (on-premise supplies whether there was a repair and how many hours stopped). A
    repair 10:30 to 10:30 with recorded times is 0 h (2026-10-06 rule), but a repair happened. The
    device counts as serviced, so the row stays."""
    _insert(conn, [{"ledger": 1, "trouble": _dt("2026-03-10 10:30"),
                    "completion": _dt("2026-03-10 10:30"), "is_failure": True}])
    assert _fact_rows(conn) == [(1, "2026-03", pytest.approx(0.0))]
    assert _serviced_devices(conn, "2026-03") == 1


def test_two_devices_with_repairs_in_one_month_are_two_serviced_devices(conn):
    """出典: poc_metric_definition.md entry 10 の分母 (選んだ期間に修理があった機器の台数)。
    Source: entry 10's denominator (devices with a repair in the chosen period)."""
    _insert(conn, [
        {"ledger": 1, "trouble": _dt("2026-03-03 08:00"), "completion": _dt("2026-03-03 12:00"), "is_failure": True},
        {"ledger": 2, "trouble": _dt("2026-03-04 08:00"), "completion": _dt("2026-03-04 20:00"), "is_failure": True},
    ])
    assert _serviced_devices(conn, "2026-03") == 2


# --- 3. 点検などの時間は混ざらない ---
# --- 3. Non-failure time is not mixed in ---

@pytest.mark.parametrize("classification", ["maintenance", "inspection", "no_fault"])
def test_non_failure_time_is_not_mixed_into_the_same_device_month(conn, classification):
    """出典: poc_metric_definition.md entry 10 の条件 (ADR-2026-06-16: 点検の時間を混ぜない)。同じ
    (機器, 月) に故障 10 時間と点検 100 時間があっても、fact は 10 時間だけ。

    Source: entry 10's condition (ADR-2026-06-16: do not mix inspection time). With 10 h of failure
    and 100 h of non-failure in the same (device, month), the fact holds 10 h only."""
    _insert(conn, [
        {"ledger": 1, "trouble": _dt("2026-03-10 08:00"), "completion": _dt("2026-03-10 18:00"), "is_failure": True},
        {"ledger": 1, "trouble": _dt("2026-03-12 08:00"), "completion": _dt("2026-03-16 12:00"),
         "is_failure": False, "cls": classification},
    ])
    assert _fact_rows(conn) == [(1, "2026-03", pytest.approx(10.0))]


def test_a_month_with_only_a_non_failure_repair_is_not_a_serviced_month(conn):
    """出典: 同上。故障が3月にあり、点検だけが4月にある機器は、4月に「修理された機器」として数えない。
    Source: same. A device with a failure in March and only an inspection in April is not counted as
    serviced in April."""
    _insert(conn, [
        {"ledger": 1, "trouble": _dt("2026-03-10 08:00"), "completion": _dt("2026-03-10 18:00"), "is_failure": True},
        {"ledger": 1, "trouble": _dt("2026-04-10 08:00"), "completion": _dt("2026-04-10 18:00"),
         "is_failure": False, "cls": "inspection"},
    ])
    assert _serviced_devices(conn, "2026-03") == 1
    assert _serviced_devices(conn, "2026-04") == 0


# --- 4. 3か月以上にまたがる修理は、触れた月すべてに行ができる ---
# --- 4. A repair spanning 3+ months gets a row in every month it touches ---

def test_repair_spanning_three_months_makes_a_row_in_each_month(conn):
    """出典: monthly_fact_template.md §2 (月をまたぐ記録は月ごとの行) と entry 10 の分母 (期間の中に修理が
    あった機器)。3/30 12:00 から 5/2 12:00: 3月 36 時間、4月 720 時間 (まるごと)、5月 36 時間。真ん中の月も
    「修理中の機器」。

    Source: monthly_fact_template.md section 2 and entry 10's denominator (devices with a repair in
    the period). 3/30 12:00 to 5/2 12:00: March 36 h, April 720 h (the whole month), May 36 h. The
    middle month counts as well: the device was under repair."""
    _insert(conn, [{"ledger": 1, "trouble": _dt("2026-03-30 12:00"),
                    "completion": _dt("2026-05-02 12:00"), "is_failure": True}])
    assert _fact_rows(conn) == [
        (1, "2026-03", pytest.approx(36.0)),
        (1, "2026-04", pytest.approx(720.0)),
        (1, "2026-05", pytest.approx(36.0)),
    ]