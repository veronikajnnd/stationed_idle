-- WARNING: this script DROPs and rebuilds cur.monthly_failure_downtime —
-- running it deletes any existing data in that table without confirmation.
-- 注意: このスクリプトは cur.monthly_failure_downtime を DROP して作り直し
-- ます。実行すると、既存のデータは確認なしに削除されます。
-- ============================================================
-- Entry 4 (*故障率, failure rate) — vertical slice, definition step
-- Entry 4（*故障率）— 縦スライス、定義ステップ
-- ============================================================
-- Output: device x month fact table of downtime hours, for real-failure
-- repairs only. This is on-premise's whole job for this metric — the
-- failure-rate PERCENTAGE (downtime / period length) is Superset's,
-- computed once a period is selected on screen. See poc_metric_definition.md,
-- entry 4, for the full decision trail (Miyazawa-san, 2026-09-01/02/03).
-- 出力: 機器 x 月ごとの、真の故障によるダウンタイム時間のfactテーブル。
-- このメトリクスに関するオンプレの仕事はこれで全部。故障率の「%」計算
-- （ダウンタイム ÷ 期間の長さ）は Superset側の仕事（画面で期間が選ばれて
-- から計算する）。決定の経緯は poc_metric_definition.md の entry 4 を参照。
--
-- REVISED 2026-09-03 (13:45): is_real_failure logic checked against the
-- metric calculation already built for the PoC Superset dashboards
-- (dataloop-poc repo) — that turned out to have a refactored, current
-- version, different from the older one this draft started with. See
-- note below.
-- REVISED 2026-09-03 (14:xx): rebuilt against the REAL cur schema (psql
-- output from streamedixdb), replacing an earlier column-name guess. Two
-- real changes, not just column renames:
--   1. trouble_date/completion_date are `date` in the real table (no time
--      of day) — the earlier draft assumed they carried time-of-day and
--      flagged this as unverified. calculated_trouble_date/
--      calculated_completion_date are `timestamp without time zone` and
--      are used instead, same as calculated_downtime_hours already does.
--   2. The real table already carries curated is_failure (boolean) and
--      repair_classification (text) columns. This SQL does NOT trust them
--      blindly — it recomputes is_real_failure from the raw text columns
--      (see real_failure_repairs below) and cross-checks the two in the
--      validation query at the bottom, because it's unknown whether
--      is_failure was populated with the OLD or the current/refactored
--      classification logic. If they disagree a lot, that's a
--      curation-pipeline question for the team, not something to paper
--      over here.
-- 2026-09-03 (13:45) 修正: is_real_failure の判定を、PoC Superset
-- ダッシュボード用に既に組んであったメトリクス計算（dataloop-poc リポジトリ）
-- を思い出して照合したところ、より新しいリファクタ済みのロジックだった
-- （このドラフトの元は古い版）。詳細は下記。
-- 2026-09-03 (14:xx) 修正: 実際の cur スキーマ（streamedixdb の psql 出力）
-- に合わせて作り直した。単なるカラム名の置き換えではなく、実質的な変更が
-- 2点ある:
--   1. trouble_date/completion_date は実テーブルでは `date` 型（時刻情報
--      なし）。前の案は時刻情報がある前提で「要確認」としていたが、
--      calculated_trouble_date/calculated_completion_date（timestamp
--      without time zone）を代わりに使う。calculated_downtime_hours が
--      既にそうしているのと同じ考え方。
--   2. 実テーブルには is_failure（boolean）と repair_classification
--      （text）という、既にキュレーション済みのカラムがある。このSQLは
--      それらを鵜呑みにしない — is_real_failure を生のテキストカラムから
--      再計算し（real_failure_repairs 参照）、末尾の検証クエリで両者を
--      突き合わせる。is_failure が旧ロジックと現行ロジックのどちらで
--      作られたか不明なため。大きく食い違うようなら、ここで握りつぶさず、
--      チームへのキュレーション周りの確認事項とする。
--
-- REVISED 2026-09-04: cross-check 3 (recomputed vs curated is_failure) found
-- a large one-directional mismatch (6039 completed repairs curated as
-- failure that recomputed as not-failure, vs only 61 the other way). Sample
-- investigation (real raw text, 20 rows) found two distinct, separable
-- causes:
--   1. CONFIRMED BUG, fixed here: the old logic excluded a repair whenever
--      EITHER column matched an inspection/adjustment word, even if the
--      OTHER column already showed a real failure signal (e.g.
--      failure_reason='原因不明の動作不良' but event_note='定期点検' was
--      being excluded solely because of event_note). Fixed by dropping the
--      separate exclusion check entirely -- if a failure keyword matches
--      anywhere, that is now sufficient on its own, regardless of what the
--      other column says about the visit type.
--   2. VOCABULARY GAP, fixed here: 'バッテリー消耗' (battery wear) was not
--      in the keyword list at all, despite being an unambiguous device
--      fault (battery needing replacement), and appeared in ~1/4 of the
--      sampled mismatches. Added to the keyword list.
--   3. NOT fixed here, OPEN ITEM for Miyazawa-san: a large share of the
--      remaining mismatches (curated is_failure=true) carry NO
--      failure-describing text at all -- just a visit-type label such as
--      'メーカー定期点検' (manufacturer's routine inspection) or '調整'
--      (adjustment), with nothing suggesting a fault was found. One sampled
--      row (failure_reason='特に問題なし', i.e. "no particular problem")
--      was curated as failure despite the text directly saying otherwise.
--      This is a business definition question, not something to guess at
--      in SQL: should a repair record with repair_classification='failure'
--      but no supporting fault description count toward this metric? Until
--      answered, this query's is_real_failure still requires explicit
--      fault-describing text, so it does NOT count these -- if the answer
--      turns out to be "yes, count them", this query will still
--      undercount. See poc_metric_definition.md's Open Items for the
--      question as raised.
-- 2026-09-04 修正: 検証3（再計算 vs curated is_failure の突き合わせ）で、
-- 一方向に大きく偏った不一致を発見（完了済み修理6039件が curated では
-- failure なのに recomputed では failure ではない、逆方向はわずか61件）。
-- 実データのサンプル調査（生テキスト20件）の結果、原因は2つに分離できた:
--   1. 確定バグ、ここで修正済み: 旧ロジックは、片方のカラムに点検/調整系の
--      単語があるだけで除外していた。もう片方のカラムに既に本物の故障
--      サインがあってもお構いなしだった（例: failure_reason='原因不明の
--      動作不良' なのに event_note='定期点検' というだけで除外されていた）。
--      exclusion チェックを丸ごと削除して修正: どちらかのカラムに故障
--      キーワードがマッチすればそれだけで十分とし、もう片方のカラムが
--      訪問種別を何と書いていようと関係ないようにした。
--   2. 語彙不足、ここで修正済み: 'バッテリー消耗' がキーワードリストに
--      一切なかった。バッテリー交換が必要という明確な機器の不具合なのに、
--      サンプルの不一致の約1/4で出現していた。キーワードリストに追加。
--   3. ここでは未修正、Miyazawaさんへのオープン項目: 残りの不一致の多くは
--      curated が failure なのに、故障を示すテキストが一切なく、
--      「メーカー定期点検」や「調整」のような訪問種別のラベルだけで、
--      何か不具合が見つかったことを示す記述がない。サンプル中の1件は
--      failure_reason='特に問題なし'（つまり「特に問題なし」）なのに
--      curated では failure とされていた。これはSQLで推測すべきではない
--      ビジネス上の定義の問題: repair_classification='failure' だが
--      故障を裏付ける記述が一切ない修理記録は、このメトリクスにカウント
--      すべきか？ 回答が出るまでは、このクエリの is_real_failure は
--      引き続き明示的な故障記述テキストを要求するため、これらはカウント
--      しない -- 「カウントすべき」という回答になった場合、このクエリは
--      過少カウントのままになる。問い合わせ内容は poc_metric_definition.md
--      の Open Items を参照。
--
-- ASSUMPTIONS STILL TO VERIFY BEFORE RUNNING:
--   - medical_device_ledger_id is the right device key (matches the
--     grouping used in the PoC Superset metric calculation) —
--     client_device_number is also available on this table if that turns
--     out to be preferred.
--   - is_completed is trusted as the "still open" signal, in addition to
--     checking calculated_completion_date IS NULL. If a record can have
--     is_completed = true with calculated_completion_date still NULL (or
--     vice versa), the CASE below needs a rule for that — not something
--     to guess at from the DDL alone.
-- 実行前に確認すべき前提:
--   - デバイスキーが medical_device_ledger_id で正しいか
--     （PoC Superset のメトリクス計算のグルーピングに合わせた）。
--     client_device_number も同テーブルにあるので、そちらが良ければ変更。
--   - is_completed を「未完了」判定の根拠として、calculated_completion_date
--     IS NULL のチェックと併用している。is_completed = true なのに
--     calculated_completion_date が NULL、またはその逆のケースがあり得る
--     なら、下記の CASE にルールが必要（DDL だけからは判断できない）。
--
-- DELIBERATELY NOT reusing repair_history.calculated_downtime_hours as the
-- source of truth for the monthly split: that column is a single
-- whole-repair total (not month-bucketed), and for a completed repair it
-- may carry Miyazawa-san's demo-only correction #3 (0 -> 24 substitution),
-- which does not carry over to Phase 2. It IS used in the validation query
-- below, as a cross-check for completed repairs only.
-- calculated_downtime_hours は月次分割の正とは意図的にしない: このカラムは
-- 修理1件ごとの合計（月別に分かれていない）で、完了済み修理については
-- デモ用の補正#3（0 を 24 に置き換え）が入っている可能性があり、Phase 2
-- には引き継がない。ただし完了済み修理に限り、検証クエリで突き合わせに使う。

-- OUTPUT: writes the fact into cur.monthly_failure_downtime. CONFIRMED
-- 2026-09-04 (Miyazawa-san): cur is the original/authoritative schema for
-- curated facts on-premise produces; pub is only the automatic sync path
-- from cur onward to RDS, where Superset (dion) reads it filtered by a
-- user-selected period to compute the failure-rate percentage. Nothing
-- here writes to pub directly. Swap to INSERT INTO ... (with a matching
-- CREATE TABLE, or ON CONFLICT logic for a re-run) once this is a
-- recurring job rather than a one-off vertical-slice output.
-- 出力: 結果を cur.monthly_failure_downtime に書き込む。2026-09-04
-- （Miyazawaさん）確認済み: cur はオンプレが作るキュレーション済みfactの
-- 正本スキーマ。pub は cur から先への自動同期経路にすぎず、RDS側で
-- Superset（dionさん担当）が期間選択に応じて故障率%を計算する際に読む。
-- ここから pub へ直接書き込むことはない。定期実行するジョブになったら、
-- INSERT INTO ...（対応する CREATE TABLE か、再実行時の ON CONFLICT
-- 処理つき）に置き換える。
-- DROP TABLE IF EXISTS cur.monthly_failure_downtime;

-- CREATE TABLE cur.monthly_failure_downtime AS
-- WITH real_failure_repairs AS (
    -- is_real_failure classification, matching the current PoC Superset
    -- metric calculation's "FIXED LOGIC" (2026-09-03 13:45 correction from
    -- an older version this draft started with). See
    -- poc_metric_definition.md entry 4/10 for the full reasoning
    -- (ADR-2026-06-16: failure = device fault + recall, not
    -- manufacturer-only).
    -- is_real_failure の判定は、PoC Superset のメトリクス計算にある
    -- 現行の「FIXED LOGIC」に合わせている（詳細は poc_metric_definition.md
    -- の entry 4/10）。
--     SELECT
--         r.medical_device_ledger_id,
--         r.calculated_trouble_date,
--         r.calculated_completion_date,
--         r.is_completed,
--         r.calculated_downtime_hours,
--         r.is_failure AS curated_is_failure,
--         r.repair_classification AS curated_repair_classification
--     -- 2026-09-04: exclusion check (調整/バージョンアップ/特に問題なし/点検)
--     -- removed -- it was cancelling genuine failures whenever the OTHER
--     -- column mentioned an inspection/adjustment visit. A failure keyword
--     -- match is now sufficient on its own. バッテリー消耗 added to the
--     -- keyword list (see header comment for the full investigation).
--     -- 2026-09-04: exclusion チェック（調整/バージョンアップ/特に問題なし/
--     -- 点検）を削除 -- もう片方のカラムが点検/調整系の訪問だと述べている
--     -- だけで、本物の故障を打ち消してしまっていたため。故障キーワードが
--     -- マッチすればそれだけで十分とする。バッテリー消耗をキーワードに追加
--     -- （詳しい調査経緯はヘッダーコメント参照）。
--     FROM cur.medical_device_repair_history r
--     WHERE
--         r.calculated_trouble_date IS NOT NULL
--         AND (
--             COALESCE(r.failure_reason, '')
--                 ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
--             OR COALESCE(r.event_note, '')
--                 ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
--         )
-- ),

-- One row per (repair, calendar month it touches). A still-open repair
-- (is_completed = false, or calculated_completion_date IS NULL) is treated
-- as touching every month from calculated_trouble_date through the current
-- month — the still-open record rule (2026-09-03) then caps its hours at
-- each of those months' own end, not the other way around.
-- 修理 x それが触れる暦月ごとに1行。まだ完了していない修理
-- （is_completed = false、または calculated_completion_date が NULL）は、
-- calculated_trouble_date から今月まで、すべての月に触れているとみなす。
-- 各月の時間は「未完了レコードのルール」（2026-09-03）で、それぞれの月末
-- で切る。
-- repair_months AS (
--     SELECT
--         f.medical_device_ledger_id,
--         f.calculated_trouble_date,
--         f.calculated_completion_date,
--         f.is_completed,
--         f.calculated_downtime_hours,
--         f.curated_is_failure,
--         f.curated_repair_classification,
--         gs.month_start::date AS month_start,
--         (gs.month_start + interval '1 month')::timestamp AS month_end
--     FROM real_failure_repairs f
--     CROSS JOIN LATERAL generate_series(
--         date_trunc('month', f.calculated_trouble_date),
--         date_trunc(
--             'month',
--             COALESCE(
--                 CASE WHEN f.is_completed THEN f.calculated_completion_date END,
--                 CURRENT_DATE
--             )
--         ),
--         interval '1 month'
--     ) AS gs(month_start)
-- ),

-- Downtime hours attributable to each month: the overlap between
-- [calculated_trouble_date, effective_end] and [month_start, month_end),
-- where a still-open repair's effective_end for THIS month is the month's
-- own end (still-open record rule, 2026-09-03 — applied per month rather
-- than per dashboard period, since on-premise's aggregation grain is fixed
-- at month).
-- 各月に配分するダウンタイム時間: [calculated_trouble_date, effective_end]
-- と [month_start, month_end) の重なり。まだ完了していない修理の
-- effective_end は、その月の月末とする（2026-09-03 の「未完了レコードの
-- ルール」。オンプレの集計粒度が月固定のため、ダッシュボードの期間単位
-- ではなく月単位で適用）。
-- downtime_by_month AS (
--     SELECT
--         medical_device_ledger_id,
--         month_start,
--         is_completed,
--         calculated_downtime_hours,
--         curated_is_failure,
--         curated_repair_classification,
--         GREATEST(
--             0,
--             EXTRACT(
--                 EPOCH FROM (
--                     LEAST(
--                         month_end,
--                         CASE
--                             WHEN is_completed THEN COALESCE(calculated_completion_date, month_end)
--                             ELSE LEAST(month_end, CURRENT_TIMESTAMP)
--                         END
--                     )
--                     - GREATEST(month_start::timestamp, calculated_trouble_date)
--                 )
--             ) / 3600.0
--         ) AS downtime_hours
--     FROM repair_months
-- )

-- SELECT
--     medical_device_ledger_id,
--     month_start,
--     ROUND(SUM(downtime_hours)::numeric, 2) AS downtime_hours
-- FROM downtime_by_month
-- GROUP BY medical_device_ledger_id, month_start
-- ORDER BY medical_device_ledger_id, month_start;

-- VALIDATION (per poc_metric_definition.md entry 4's どう確かめるか):
--   1. Pick 2-3 real medical_device_ledger_id values with known repairs,
--      including at least one still-open repair (is_completed = false).
--      Confirm downtime_hours is capped at each month's end, not left as
--      a gap and not extended past "now" some other way.
--   2. Cross-check is_real_failure's classification against a sample's
--      raw event_note / failure_reason text — confirm it agrees with a
--      human reading, INCLUDING at least one in-house (non-manufacturer)
--      repair with a real failure signal (the case the 2026-09-03 logic
--      correction was meant to catch).
--   3. NEW (real-schema pass): cross-check this SQL's recomputed
--      is_real_failure against the table's own curated is_failure /
--      repair_classification columns, for COMPLETED repairs only (open
--      repairs may not have a final classification yet). A large
--      disagreement means either the curation pipeline is on the OLD
--      classification, or the assumptions here need another look —
--      report either way, don't silently prefer one over the other.
--   4. NEW: for completed repairs, cross-check this query's per-record
--      total (summed across the months it touches) against
--      calculated_downtime_hours, to see whether that column is a plain
--      date-diff or has some other adjustment baked in.
-- 検証（poc_metric_definition.md の entry 4「どう確かめるか」に対応）:
--   1. 実際の medical_device_ledger_id を2〜3件選ぶ。まだ完了していない
--      修理（is_completed = false）を含む1件は必ず入れる。downtime_hours
--      が各月の月末で正しく切られているか確認する。
--   2. is_real_failure の判定結果を、サンプルの event_note /
--      failure_reason の生テキストと突き合わせる。自社修理（メーカー
--      対応ではない）で本当の故障信号があるケースを最低1件含めること。
--   3. 新規（実スキーマ対応）: このSQLが再計算した is_real_failure を、
--      テーブル自身の is_failure / repair_classification と突き合わせる
--      （完了済み修理のみ対象。未完了はまだ最終判定が出ていない可能性が
--      あるため）。大きく食い違う場合、キュレーション側が旧ロジックの
--      ままか、こちらの前提が違うかのどちらか — どちらかに決めつけず
--      報告する。
--   4. 新規: 完了済み修理について、このクエリの1件あたり合計
--      （触れた月ごとの合計）を calculated_downtime_hours と突き合わせ、
--      そのカラムが単純な日付差分なのか、他の補正が入っているのかを
--      確認する。

-- Cross-check 3: recomputed vs curated is_failure (completed repairs only).
-- Standalone query (does not depend on the CTEs above) — run separately.
-- 検証3: 再計算した is_real_failure と、キュレーション済み is_failure の
-- 突き合わせ（完了済みのみ）。上記のCTEに依存しない単独クエリ — 別途実行する。

SELECT
    r.is_failure AS curated_is_failure,
    (
        r.calculated_trouble_date IS NOT NULL
        AND (
            COALESCE(r.failure_reason, '')
                ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
            OR COALESCE(r.event_note, '')
                ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
        )
    ) AS recomputed_is_real_failure,
    COUNT(*) AS n_repairs
FROM cur.medical_device_repair_history r
WHERE r.is_completed = true
GROUP BY 1, 2
ORDER BY 1, 2;
--
-- Read the 4-row result as a 2x2: curated=true/recomputed=true and
-- curated=false/recomputed=false are agreement; the other two rows are
-- the disagreement to report, whichever way it goes.
-- 結果は2x2として読む: curated=true/recomputed=true と
-- curated=false/recomputed=false が一致、残り2行がどちらの方向であれ
-- 報告すべき不一致。

-- Cross-check 4: this query's per-record total vs calculated_downtime_hours
-- (completed real-failure repairs only). Standalone query — run separately.
-- 検証4: このクエリの1件あたり合計と calculated_downtime_hours の突き合わせ
-- （完了済みかつ真の故障のみ）。単独クエリ — 別途実行する。
--
SELECT
    r.medical_device_ledger_id,
    r.calculated_trouble_date,
    r.calculated_completion_date,
    r.calculated_downtime_hours AS existing_column_hours,
    ROUND(
        (EXTRACT(EPOCH FROM (r.calculated_completion_date - r.calculated_trouble_date)) / 3600.0)
    ::numeric, 2) AS recomputed_hours_straight_diff
FROM cur.medical_device_repair_history r
WHERE
    r.is_completed = true
    AND r.calculated_trouble_date IS NOT NULL
    AND (
        COALESCE(r.failure_reason, '')
            ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
        OR COALESCE(r.event_note, '')
            ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
    )
ORDER BY ABS(
    COALESCE(r.calculated_downtime_hours, 0)
    - EXTRACT(EPOCH FROM (r.calculated_completion_date - r.calculated_trouble_date)) / 3600.0
) DESC
LIMIT 20;  -- biggest disagreements first

-- ============================================================
-- DIAGNOSTICS requested in the 2026-09-04 (09:34) review, run BEFORE the
-- validation above. These are not part of poc_metric_definition.md's
-- どう確かめるか list (that's cross-checks 1-4 above) — they exist to
-- scope a data-quality question the review raised: whether the
-- month-explosion logic lets a very old still-open repair inflate a
-- single device's row count, before doing the 2-3 device spot-check.
-- 2026-09-04（09:34）レビューで依頼された診断クエリ。上記の検証より先に
-- 実行する。poc_metric_definition.md の「どう確かめるか」（検証3/4）とは
-- 別物 — レビューが指摘したデータ品質の懸念（月展開ロジックが、非常に
-- 古い未完了修理1件のせいである機器の行数を大きく膨らませていないか）の
-- 範囲を、2〜3機器のスポットチェックより先に把握するためのもの。
-- ============================================================

-- Diagnostic A (review point 5): device 2's full month-by-month history.
-- The round-number hours (720.00 = 30x24, 744.00 = 31x24) on consecutive
-- months are consistent with a device that was down for whole calendar
-- months in a row. No LIMIT on purpose — check the LAST row's month_start;
-- if it reaches 2026-09-01 (the current month), that confirms a repair
-- that has been open since a very old calculated_trouble_date, still
-- getting exploded into every month up to today.
-- 診断A（レビュー指摘5）: 機器2番の全期間の月次履歴。連続する月で
-- 720.00（=30x24）、744.00（=31x24）というきりの良い時間は、その機器が
-- 暦月まるごと停止していたことと整合する。意図的にLIMITなし — 最後の行の
-- month_start を確認する。2026-09-01（今月）まで続いていれば、非常に古い
-- calculated_trouble_date からずっと未完了のままの修理が、今月まで
-- 毎月展開され続けていることの裏付けになる。
-- SELECT *
-- FROM cur.monthly_failure_downtime
-- WHERE medical_device_ledger_id = 2
-- ORDER BY month_start;

-- Diagnostic B (review point 6): count of unfinished repairs by year of
-- calculated_trouble_date, across ALL repairs (not filtered to
-- is_real_failure) — this is the literal scope Miyazawa-san asked for,
-- since the month-explosion issue is about the source table's still-open
-- rows in general, not only the ones that pass the failure classification.
-- Run this FIRST, before any per-device spot-check: a 2-3 device sample
-- would not have revealed how many rows (and which years) are driving the
-- explosion.
-- 診断B（レビュー指摘6）: calculated_trouble_date の年ごとの未完了修理件数。
-- is_real_failure で絞らず全修理を対象とする — Miyazawaさんの依頼どおりの
-- 範囲そのまま（月展開の問題は、故障判定を通過した行に限らず、元テーブルの
-- 未完了行全般の話のため）。機器単位のスポットチェックより先に実行する:
-- 2〜3機器のサンプルだけでは、どの年のどれだけの行数が膨張の原因かは
-- わからない。
-- SELECT
--     EXTRACT(YEAR FROM calculated_trouble_date) AS trouble_year,
--     COUNT(*) AS n_unfinished_repairs
-- FROM cur.medical_device_repair_history
-- WHERE
--     is_completed = false
--     OR calculated_completion_date IS NULL
-- GROUP BY 1
-- ORDER BY 1;

-- Diagnostic B2 (bonus, not literally asked for): same count, scoped to
-- just the is_real_failure population — this is the subset that actually
-- explodes into cur.monthly_failure_downtime, so it's a closer proxy for
-- "how much does this affect entry 4's output specifically" than
-- Diagnostic B's unscoped count. Worth having both: B answers Miyazawa-san's
-- question as asked, B2 answers "does this affect entry 4."
-- 診断B2（おまけ、依頼された範囲ではない）: is_real_failure に絞った
-- 同じ集計 — cur.monthly_failure_downtime に実際に展開される対象なので、
-- 「entry 4の出力に具体的にどれだけ影響するか」により近い指標になる。
-- 診断Bは依頼どおりの範囲への回答、診断B2は「entry 4への影響」への回答、
-- 両方あった方がよい。
-- SELECT
--     EXTRACT(YEAR FROM r.calculated_trouble_date) AS trouble_year,
--     COUNT(*) AS n_unfinished_real_failure_repairs
-- FROM cur.medical_device_repair_history r
-- WHERE
--     (r.is_completed = false OR r.calculated_completion_date IS NULL)
--     AND r.calculated_trouble_date IS NOT NULL
--     AND (
--         COALESCE(r.failure_reason, '')
--             ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
--         OR COALESCE(r.event_note, '')
--             ~ '動作不良|破損|断線|劣化|不良|修理不能|メーカー修理|オーバーホール|バッテリー消耗'
--     )
-- GROUP BY 1
-- ORDER BY 1;