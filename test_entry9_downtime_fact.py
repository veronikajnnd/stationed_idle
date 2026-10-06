"""
entry 9 (貸出余力の合計拘束時間) の「回復時間の項」= entry 4 の月次ダウンタイム fact の守るテスト。
Guard tests for entry 9's recovery-time term, which is entry 4's monthly downtime fact.

出典 / Sources (各 test の期待値の根拠にした文書 / the documents each expectation is based on):
    - poc_metric_definition.md entry 9 (2026-09-03): 回復時間の項 = entry 4 の月ごとに切った
      ダウンタイム (未完了は月末で切る、除外しない)
    - ADR-2026-06-16: failure の定義 (点検・軽微作業は含めない)
    - monthly_fact_template.md §2 (粒度 = 実体 x 月、月をまたぐ記録は月ごとの行)、
      §6/§7 (ledger_id IS NOT NULL を明示する)

何を試すか / What this tests:
    entry4_monthly_failure_downtime.sql の SELECT 本体 (下の load_fact_select で読む。
    コピーしない) を、合成した修理行に対して実行し、月次 fact が上の文書どおりになるかを見る。
    テーブルは作らない: 一時テーブルに行を入れ、with_source() で fact の元テーブルをそれに差し替える。
    cur.* には触れない。

    Runs the SELECT body of entry4_monthly_failure_downtime.sql (read through
    load_fact_select below, never copied) against synthetic repair rows and checks
    the monthly fact matches the documents above. No persistent table is created: rows go
    into a temp table and with_source() swaps the fact's source table for it. cur.* is never
    touched.

実行方法 / How to run (stationed_idle のルートで。DB が必要):
    pytest test_entry9_downtime_fact.py -v

    接続先は TEST_DATABASE_URL があればそれ、無ければ streamedix_common.config.get_database_url()。
    書き込むのはセッション内の一時テーブル (t_hist) だけで、最後に必ず rollback する。cur.* の
    テーブルは読みも書きもしない (fact の元テーブルを一時テーブルに差し替えるため)。
    接続できない場合は、全 test を skip する。

    The DB URL is TEST_DATABASE_URL when set, otherwise streamedix_common.config.get_database_url().
    Only a session-local temp table (t_hist) is written, and it is always rolled back. No cur.* table
    is read or written (the fact's source table is swapped for the temp table). All tests skip when
    no connection can be made.
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


# --- 1. 故障の修理は、その月のダウンタイムとして数えられる (基準) ---
# --- 1. A failure repair counts toward its month's downtime (baseline) ---

def test_failure_repair_within_one_month_counts_its_hours(conn):
    """出典: poc_metric_definition.md entry 4/9 (ダウンタイム = 故障の修理の停止時間)。
    Source: entry 4/9 (downtime = stopped hours of a failure repair)."""
    _insert(conn, [{"ledger": 1, "trouble": _dt("2026-03-10 08:00"),
                    "completion": _dt("2026-03-10 18:00"), "is_failure": True}])
    assert _fact(conn) == {(1, "2026-03"): pytest.approx(10.0)}


# --- 2. 故障ではない分類の修理は、ダウンタイムに入らない ---
# --- 2. A repair classified as non-failure does not enter downtime ---

@pytest.mark.parametrize("classification", ["maintenance", "inspection", "no_fault"])
def test_non_failure_classification_adds_no_downtime(conn, classification):
    """出典: ADR-2026-06-16 (failure に点検・軽微作業は含めない) と entry 10 の条件 (点検の時間を
    混ぜない)。このレイヤーの SQL は is_failure (boolean) しか見ないので、3つの分類は同じ振る舞い。
    これが「分類が変わった行は fact から消える」の根拠。

    Source: ADR-2026-06-16 (inspection/maintenance are not failure) and entry 10's condition
    (do not mix inspection time in). The SQL at this layer only reads is_failure (boolean),
    so all three labels behave the same; this is what makes a reclassified row leave the fact."""
    _insert(conn, [{"ledger": 1, "trouble": _dt("2026-03-10 08:00"),
                    "completion": _dt("2026-03-10 18:00"), "is_failure": False, "cls": classification}])
    assert _fact(conn) == {}


# --- 3. 月をまたぐ修理は月ごとの行に分かれる ---
# --- 3. A repair spanning a month boundary splits into one row per month ---

def test_repair_spanning_two_months_splits_per_month(conn):
    """出典: monthly_fact_template.md §2 (月をまたぐ記録は月ごとの行、1日0:00から月末まで)。
    3/30 12:00 から 4/2 12:00 まで = 3月 36時間 + 4月 36時間 = 72時間。

    Source: monthly_fact_template.md §2. 3/30 12:00 to 4/2 12:00 = 36h in March + 36h in April."""
    _insert(conn, [{"ledger": 1, "trouble": _dt("2026-03-30 12:00"),
                    "completion": _dt("2026-04-02 12:00"), "is_failure": True}])
    assert _fact(conn) == {
        (1, "2026-03"): pytest.approx(36.0),
        (1, "2026-04"): pytest.approx(36.0),
    }


# --- 4. まだ終わっていない修理は、除外せず月末 (または現在) で切って含める ---
# --- 4. A still-open repair is included, capped at month end (or now), never excluded ---

def test_still_open_repair_is_included_and_capped(conn):
    """出典: poc_metric_definition.md の「未完了レコードのルール」(2026-09-03) と、日付だけの修理時刻の
    ルール (2026-10-06)。先月1日 0:00 から未完了の修理は、先月は月末まで、今月は「今」までで切られた行が出る。
    開始がちょうど 0:00 なので 9:00 に置きかえられ、先月の時間は満月分より 9 時間短い。

    Source: the still-open record rule (2026-09-03) and the date-only repair time rule (2026-10-06).
    A repair open since the 1st of last month yields last month up to month end and a row for this month
    capped at now. The start is exactly 00:00, so it is replaced by 09:00 and last month's hours are 9 h
    short of a full month."""
    this_month, last_month = conn.execute(
        text("SELECT date_trunc('month', now())::timestamp, "
             "(date_trunc('month', now()) - interval '1 month')::timestamp")
    ).one()
    _insert(conn, [{"ledger": 1, "trouble": last_month, "completion": None,
                    "is_completed": False, "is_failure": True}])
    fact = _fact(conn)

    days_last_month = (this_month - last_month).days
    # 開始 00:00 -> 9:00 の置きかえで、満月分から 9 時間引く (2026-10-06 のルールが決めた変更)
    # The 00:00 -> 09:00 start replacement takes 9 h off a full month (the change decided by the
    # 2026-10-06 rule)
    assert fact[(1, last_month.strftime("%Y-%m"))] == pytest.approx(days_last_month * 24.0 - 9.0)

    elapsed_hours = conn.execute(
        text("SELECT extract(epoch FROM (now()::timestamp - date_trunc('month', now())::timestamp)) / 3600.0")
    ).scalar()
    assert 0 <= fact[(1, this_month.strftime("%Y-%m"))] <= float(elapsed_hours) + 0.01


# --- 5. 発生日が無い修理は fact に入らない ---
# --- 5. A repair without a trouble date does not enter the fact ---

def test_repair_without_trouble_date_is_excluded(conn):
    """出典: SQL の前提 (calculated_trouble_date IS NOT NULL)。期間を決められない行は数えられない。
    Source: the SQL's own precondition; a row with no start cannot be placed in any month."""
    _insert(conn, [{"ledger": 1, "trouble": None, "completion": _dt("2026-03-10 18:00"),
                    "is_failure": True}])
    assert _fact(conn) == {}


# --- 6. 台帳に紐付かない修理 (medical_device_ledger_id が NULL) は集計に入れない ---
# --- 6. A repair not linked to the ledger (ledger_id NULL) is not aggregated ---

def test_repair_with_null_ledger_id_is_excluded(conn):
    """出典: monthly_fact_template.md §6/§7 (ADR-2026-09-09 の適用までは、集計のたびに
    medical_device_ledger_id IS NOT NULL を明示する)。台帳に紐付かない修理は、どの機器のダウンタイム
    でもないので、fact に (NULL, 月) という行として出てはいけない。

    ADR-2026-09-09 が適用されて列が NOT NULL になれば、この条件は SQL からも、この test からも外してよい。

    Source: monthly_fact_template.md section 6/7 (until ADR-2026-09-09 is applied, write
    medical_device_ledger_id IS NOT NULL explicitly in every aggregate). A repair not linked to the
    ledger is the downtime of no device, so it must not appear as a (NULL, month) row in the fact.

    Once ADR-2026-09-09 is applied and the column is NOT NULL, the condition can be dropped from the
    SQL and this test can be dropped with it."""
    _insert(conn, [{"ledger": None, "trouble": _dt("2026-03-10 08:00"),
                    "completion": _dt("2026-03-10 18:00"), "is_failure": True}])
    assert _fact(conn) == {}


# --- 7. 1行の分類が failure から外れても、変わるのはその行が触れた月だけ ---
# --- 7. Flipping one row out of failure changes only the months that row touches ---

def test_flipping_one_row_changes_only_its_own_device_months(conn):
    """entry 9 の before/after で使う不変条件そのもの。reclassification で failure から外れた行は、
    その行が触れた (機器, 月) の時間だけを、その行の分だけ減らす。ほかの (機器, 月) は1つも変わらない。

    The invariant the entry 9 before/after relies on: a row that leaves failure reduces only the
    (device, month) cells it touched, by exactly its own hours. No other cell moves."""
    rows = [
        {"id": 1, "ledger": 1, "trouble": _dt("2026-03-05 00:00"), "completion": _dt("2026-03-05 10:00"), "is_failure": True},
        {"id": 2, "ledger": 2, "trouble": _dt("2026-03-30 12:00"), "completion": _dt("2026-04-02 12:00"), "is_failure": True},
        {"id": 3, "ledger": 1, "trouble": _dt("2026-04-10 00:00"), "completion": _dt("2026-04-10 05:00"), "is_failure": True},
    ]
    _insert(conn, rows)
    before = _fact(conn)

    conn.execute(text("UPDATE t_hist SET is_failure = false, repair_classification = 'maintenance' "
                      "WHERE medical_device_repair_history_id = 2"))
    after = _fact(conn)

    # 機器2 (行2) の2か月分だけが消え、機器1 の2行は完全に同じ
    # Device 2's two months (row 2) disappear; device 1's two cells are identical.
    assert before[(2, "2026-03")] == pytest.approx(36.0)
    assert before[(2, "2026-04")] == pytest.approx(36.0)
    assert (2, "2026-03") not in after and (2, "2026-04") not in after
    assert {k: v for k, v in after.items() if k[0] == 1} == {k: v for k, v in before.items() if k[0] == 1}


# =====================================================================================
# 8. 日付だけの修理時刻のルール (poc_metric_definition.md, 2026-10-06, Miyazawa-san の決定)
# 8. Date-only repair time rule (poc_metric_definition.md, 2026-10-06, Miyazawa-san's decision)
#
# 開始・完了の「時刻がちょうど 00:00:00 の側だけ」を、開始 → 9:00、完了 → 17:00 に置きかえてから
# 計算する。時刻が入っている側はそのまま使う。置きかえは fact の SQL の中だけ。
# Only the side whose time is exactly 00:00:00 is replaced (start -> 09:00, completion -> 17:00)
# before hours are calculated; a side with a recorded time is used as is. Inside the fact SQL only.
#
# 期待値は、文書に書かれた例 (2/13 00:00 -> 2/13 00:00 = 8h など) と、ルールの文言から取った。
# Expected values come from the examples written in the document and from the wording of the rule.
# =====================================================================================

def _hours_of(conn, trouble, completion, ledger=1):
    """1 件の完了済み修理を入れて、{(機器, 月): 時間} を返す。

    Insert one completed repair and return {(device, month): hours}."""
    _insert(conn, [{"ledger": ledger, "trouble": _dt(trouble), "completion": _dt(completion),
                    "is_failure": True}])
    return _fact(conn)


def test_date_only_same_day_repair_counts_8_hours(conn):
    """出典: ルールの例「2/13 00:00 -> 2/13 00:00 = 8 h」。開始 9:00、完了 17:00。
    Source: the document's example, 2/13 00:00 -> 2/13 00:00 = 8 h (start 09:00, completion 17:00)."""
    assert _hours_of(conn, "2026-02-13 00:00", "2026-02-13 00:00") == {(1, "2026-02"): pytest.approx(8.0)}


def test_date_only_repair_ending_next_day_counts_32_hours(conn):
    """出典: ルールの例「2/13 00:00 -> 2/14 00:00 = 32 h (今までは 24 h)」。2/13 9:00 から 2/14 17:00 まで。
    Source: the document's example, 2/13 00:00 -> 2/14 00:00 = 32 h (was 24 h): 2/13 09:00 to 2/14 17:00."""
    assert _hours_of(conn, "2026-02-13 00:00", "2026-02-14 00:00") == {(1, "2026-02"): pytest.approx(32.0)}


@pytest.mark.parametrize(
    "trouble, completion, expected_hours",
    [
        # 出典: ルールの例「2/13 00:00 -> 2/13 11:00 = 2 h」(開始だけ置きかえる)
        # Source: the document's example, 2/13 00:00 -> 2/13 11:00 = 2 h (only the start is replaced)
        ("2026-02-13 00:00", "2026-02-13 11:00", 2.0),
        # 完了に時刻があるまま、開始だけ日付のみで、日をまたぐ場合: 2/13 9:00 から 2/15 12:00 まで
        # Completion has a time, only the start is date-only, across days: 2/13 09:00 to 2/15 12:00
        ("2026-02-13 00:00", "2026-02-15 12:00", 51.0),
    ],
)
def test_only_the_date_only_start_is_replaced_by_9am(conn, trouble, completion, expected_hours):
    """出典: 「置きかえは、それぞれの側で独立に行う」。完了に時刻がある側はそのまま使う。
    Source: each side is replaced on its own; a side with a recorded time is used as is."""
    assert _hours_of(conn, trouble, completion) == {(1, "2026-02"): pytest.approx(expected_hours)}


def test_only_the_date_only_completion_is_replaced_by_5pm(conn):
    """出典: 同じ「それぞれの側で独立に」。開始に時刻があり、完了だけ日付のみ: 2/13 10:00 から 2/14 17:00 まで = 31 h。
    Source: same per-side rule. Start has a time, only the completion is date-only:
    2/13 10:00 to 2/14 17:00 = 31 h."""
    assert _hours_of(conn, "2026-02-13 10:00", "2026-02-14 00:00") == {(1, "2026-02"): pytest.approx(31.0)}


def test_recorded_time_is_used_as_is_even_if_zero_hours(conn):
    """出典: ルールの例「2/13 10:30 -> 2/13 10:30 = 0 h (時刻が入っている)」。0 時間 0 分でもそのまま。
    fact にセル自体が残るかどうかは、文書が決めていないので、ここでは時間だけを見る (セルが無ければ 0 と読む)。

    Source: the document's example, 2/13 10:30 -> 2/13 10:30 = 0 h (time recorded). Whether the cell
    itself stays in the fact is not decided by the document, so only the hours are checked here
    (a missing cell reads as 0)."""
    fact = _hours_of(conn, "2026-02-13 10:30", "2026-02-13 10:30")
    assert fact.get((1, "2026-02"), 0.0) == pytest.approx(0.0)


def test_only_exactly_midnight_counts_as_date_only(conn):
    """出典: 「時刻がちょうど 00:00:00 の側だけ」。00:00:01 は時刻が入っているので置きかえない。
    2/13 00:00:01 から 2/13 10:00 まで = 9 時間 59 分 59 秒 (置きかえたなら 1 時間)。

    Source: "exactly 00:00:00". 00:00:01 is a recorded time and is not replaced:
    2/13 00:00:01 to 2/13 10:00 is 9 h 59 min 59 s (it would be 1 h if replaced)."""
    fact = _hours_of(conn, "2026-02-13 00:00", "2026-02-13 10:00")  # 比較用: 置きかえあり / for contrast: replaced
    assert fact == {(1, "2026-02"): pytest.approx(1.0)}
    _insert(conn, [{"id": 99, "ledger": 2, "trouble": datetime(2026, 2, 13, 0, 0, 1),
                    "completion": _dt("2026-02-13 10:00"), "is_failure": True}])
    fact = _fact(conn)
    assert fact[(2, "2026-02")] == pytest.approx(10.0, abs=0.01)


@pytest.mark.parametrize(
    "trouble, completion",
    [
        # 出典: 「置きかえたあと、終わりが始まりより前になる行は 0 時間」 開始 20:00、完了は同じ日の日付のみ (17:00)
        # Source: "if the end is before the start after the replacement: 0 h". Start 20:00, completion
        # date-only on the same day (17:00)
        ("2026-02-13 20:00", "2026-02-13 00:00"),
        # 開始が日付のみ (9:00)、完了が 8:00 と記録されている
        # Start date-only (09:00), completion recorded as 08:00
        ("2026-02-13 00:00", "2026-02-13 08:00"),
    ],
)
def test_end_before_start_after_replacement_is_zero_hours(conn, trouble, completion):
    """出典: ルールの「置きかえたあと、終わりが始まりより前になる場合は 0 時間」。負の時間にならない。
    Source: the rule's end-before-start case: 0 h, never negative."""
    fact = _hours_of(conn, trouble, completion)
    assert fact.get((1, "2026-02"), 0.0) == pytest.approx(0.0)


def test_date_only_repair_across_a_month_boundary_splits_after_replacement(conn):
    """出典: 「そのあとは今までどおり計算する (月ごとに分ける)」。3/31 00:00 -> 4/1 00:00 は、
    3/31 9:00 から 4/1 17:00 まで。3月は 3/31 9:00 から 24:00 までの 15 時間、4月は 0:00 から 17:00 までの 17 時間。

    Source: "then everything is calculated as before (split by month)". 3/31 00:00 -> 4/1 00:00 is
    3/31 09:00 to 4/1 17:00: 15 h in March (09:00 to 24:00) and 17 h in April (00:00 to 17:00)."""
    assert _hours_of(conn, "2026-03-31 00:00", "2026-04-01 00:00") == {
        (1, "2026-03"): pytest.approx(15.0),
        (1, "2026-04"): pytest.approx(17.0),
    }