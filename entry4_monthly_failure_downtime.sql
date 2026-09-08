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
-- note below. SUPERSEDED 2026-09-08 (task9) — see that note further down;
-- this SQL no longer keeps its own copy of the logic to compare at all.
-- REVISED 2026-09-03 (14:xx): rebuilt against the REAL cur schema (psql
-- output from streamedixdb), replacing an earlier column-name guess. Two
-- real changes, not just column renames:
--   1. trouble_date/completion_date are `date` in the real table (no time
--      of day) — the earlier draft assumed they carried time-of-day and
--      flagged this as unverified. calculated_trouble_date/
--      calculated_completion_date are `timestamp without time zone` and
--      are used instead, same as calculated_downtime_hours already does.
--   2. The real table already carries curated is_failure (boolean) and
--      repair_classification (text) columns. At the time, this SQL did NOT
--      trust them blindly — it recomputed is_real_failure from the raw text
--      columns and cross-checked the two, because it was unknown whether
--      is_failure was populated with the OLD or the current/refactored
--      classification logic. SUPERSEDED 2026-09-08 (task9): the two are no
--      longer suspected to disagree, see below — is_failure is now read
--      directly and trusted.
-- 2026-09-03 (13:45) 修正: is_real_failure の判定を、PoC Superset
-- ダッシュボード用に既に組んであったメトリクス計算（dataloop-poc リポジトリ）
-- を思い出して照合したところ、より新しいリファクタ済みのロジックだった
-- （このドラフトの元は古い版）。詳細は下記。2026-09-08（task9）でこの
-- 位置づけ自体が上書きされている — 下記参照。このSQLはもう比較対象となる
-- 自前ロジックのコピーを持たない。
-- 2026-09-03 (14:xx) 修正: 実際の cur スキーマ（streamedixdb の psql 出力）
-- に合わせて作り直した。単なるカラム名の置き換えではなく、実質的な変更が
-- 2点ある:
--   1. trouble_date/completion_date は実テーブルでは `date` 型（時刻情報
--      なし）。前の案は時刻情報がある前提で「要確認」としていたが、
--      calculated_trouble_date/calculated_completion_date（timestamp
--      without time zone）を代わりに使う。calculated_downtime_hours が
--      既にそうしているのと同じ考え方。
--   2. 実テーブルには is_failure（boolean）と repair_classification
--      （text）という、既にキュレーション済みのカラムがある。当時このSQLは
--      それらを鵜呑みにせず、is_real_failure を生のテキストカラムから
--      再計算し、両者を突き合わせていた。is_failure が旧ロジックと現行
--      ロジックのどちらで作られたか不明だったため。2026-09-08（task9）で
--      上書き: 今はもう食い違いを疑っておらず、is_failure を直接信頼する。
--
-- REVISED 2026-09-04: cross-check 3 (recomputed vs curated is_failure) found
-- a large one-directional mismatch (6039 completed repairs curated as
-- failure that recomputed as not-failure, vs only 61 the other way). Sample
-- investigation (real raw text, 20 rows) found two distinct, separable
-- causes:
--   1. CONFIRMED BUG, fixed here (in this SQL's own copy at the time): the
--      old logic excluded a repair whenever EITHER column matched an
--      inspection/adjustment word, even if the OTHER column already showed
--      a real failure signal (e.g. failure_reason='原因不明の動作不良' but
--      event_note='定期点検' was being excluded solely because of
--      event_note). Fixed by dropping the separate exclusion check entirely.
--   2. VOCABULARY GAP, fixed here (in this SQL's own copy at the time):
--      'バッテリー消耗' (battery wear) was not in the keyword list at all,
--      despite being an unambiguous device fault, and appeared in ~1/4 of
--      the sampled mismatches. Added to the keyword list.
--   3. NOT fixed here, OPEN ITEM raised to Miyazawa-san at the time: a large
--      share of the remaining mismatches (curated is_failure=true) carried
--      NO failure-describing text at all. Whether a repair record with
--      repair_classification='failure' but no supporting fault description
--      should count toward this metric was left as a business-definition
--      question, not something to guess at in SQL.
--   RESOLVED 2026-09-08 (task9): both fixes above (#1, #2) were carried into
--   failure_classifier.py for real during task11 (2026-09-07, ADR-2026-06-16
--   alignment), and open item #3 is resolved by task9's change itself, not
--   answered directly — see the task9 note below for why. This SQL's own
--   copy of these fixes is deleted along with the rest of the recomputation
--   CTE; the history above is kept only as the record of how the fixes were
--   first found and verified, on 2026-09-04's real data.
-- 2026-09-04 修正: 検証3（再計算 vs curated is_failure の突き合わせ）で、
-- 一方向に大きく偏った不一致を発見（完了済み修理6039件が curated では
-- failure なのに recomputed では failure ではない、逆方向はわずか61件）。
-- 実データのサンプル調査（生テキスト20件）の結果、原因は2つに分離できた:
--   1. 確定バグ、当時ここ（このSQL自前のコピー）で修正済み: 旧ロジックは、
--      片方のカラムに点検/調整系の単語があるだけで除外していた。exclusion
--      チェックを丸ごと削除して修正。
--   2. 語彙不足、当時ここ（このSQL自前のコピー）で修正済み: 'バッテリー
--      消耗' がキーワードリストに一切なかった。キーワードリストに追加。
--   3. 当時ここでは未修正、Miyazawaさんへのオープン項目として提起: 残りの
--      不一致の多くは curated が failure なのに故障を示すテキストが一切
--      ない。これはSQLで推測すべきではないビジネス上の定義の問題として
--      提起した。
--   2026-09-08（task9）解決: 上記1・2の修正は task11（2026-09-07、
--   ADR-2026-06-16 対応）で failure_classifier.py 側に本物として反映済み。
--   オープン項目3は、task9のこの変更自体によって「回答が出た」のではなく
--   「問いが解消された」（詳細は下記task9の注記）。このSQL自前のコピーは
--   再計算CTEごと削除する。上の経緯はあくまで、2026-09-04の実データで
--   最初にこれらの修正を見つけ検証した記録として残す。
--
-- REVISED 2026-09-08 (task9, per Miyazawa-san's 2026-09-07 review): entry 4
-- stops maintaining its own copy of the failure-classification rules and
-- reads cur.medical_device_repair_history.is_failure directly instead. The
-- real_failure_repairs CTE below no longer recomputes anything from
-- failure_reason/event_note text — it just filters on is_failure = true.
--
-- Why this is safe now, when it wasn't in September: as of task11
-- (2026-09-07), failure_classifier.py — the single real source of truth for
-- is_failure/repair_classification, applied by the datacuration-curate
-- pipeline — was brought in line with ADR-2026-06-16 in full (回収/リコール
-- moved from maintenance to failure), and this SQL's own hand-duplicated
-- fixes from 2026-09-04 (the same-record exclusion bug, the バッテリー消耗
-- keyword gap) exist there too. The two copies had converged, so
-- Miyazawa-san's instruction was to stop keeping two copies in sync by hand
-- and collapse to one: the classifier.
--
-- What this removes: cross-check 3 (recomputed vs curated is_failure, see
-- the 2026-09-03/09-04 notes above) no longer means anything, since there is
-- no separate recomputation left to disagree with the curated column —
-- deleted rather than kept as a comparison against itself. The 2026-09-04
-- open item (repair_classification='failure' with no supporting fault text)
-- is resolved by this change, not answered: this query now trusts is_failure
-- as-is, whatever the classifier decided and why, rather than additionally
-- requiring explicit fault-describing text of its own.
--
-- What this means for future classification changes — e.g. Miyazawa-san's
-- 2026-09-08 decision to move バッテリー消耗 back OUT of failure into
-- inspection (ADR update + task10 implementation in progress, not done as
-- of this revision): none of them require touching this file again.
-- Re-running datacuration-curate re-derives is_failure for every row from
-- the current failure_classifier.py, and this query picks up the new
-- numbers on its next run. That is the point of keeping classification in
-- exactly one place.
--
-- 2026-09-08 修正（task9、Miyazawaさんの2026-09-07レビュー対応）: entry 4は
-- 故障判定ロジックの自前コピーを持つのをやめ、
-- cur.medical_device_repair_history.is_failure を直接読むようにした。下記の
-- real_failure_repairs CTEは、failure_reason/event_noteのテキストから
-- 何かを再計算することはもうしない — is_failure = true で絞るだけ。
--
-- なぜ今なら安全か（9月時点ではそうではなかったか）: task11（2026-09-07）
-- の時点で、is_failure/repair_classificationの唯一の正本である
-- failure_classifier.py（datacuration-curateパイプラインが適用）が、
-- ADR-2026-06-16に完全に追従した（回収/リコールがmaintenanceからfailureへ
-- 移動）。このSQL側で2026-09-04に手で直した修正（同一レコード内の誤った
-- 除外バグ、バッテリー消耗のキーワード不足）も、あちら側に同じ内容が入って
-- いる。2つのコピーが収束したので、Miyazawaさんの指示は「手で同期を取り
-- 続けるのをやめて、classifierの1箇所にまとめる」だった。
--
-- これによって無くなるもの: 検証3（再計算 vs curated is_failure、上記
-- 2026-09-03/09-04の注記参照）は意味を失う — 突き合わせる相手の再計算
-- そのものがもう存在しないため、自分自身と比べるだけの検証として残すの
-- ではなく削除した。2026-09-04のオープン項目（repair_classification=
-- 'failure'なのに故障を裏付ける記述がない）も、これで「回答が出た」の
-- ではなく「問い自体が解消された」: このクエリは今後、classifierが何を
-- どう判定したかにかかわらず is_failure をそのまま信頼する。
--
-- 今後の分類変更への影響 — 例: Miyazawaさんの2026-09-08の決定、
-- バッテリー消耗をfailureから外してinspectionへ戻す（ADR更新+task10実装は
-- この改訂時点でまだ未完了）: どれもこのファイルを触る必要がない。
-- datacuration-curateを再実行すれば現行のfailure_classifier.pyに基づいて
-- 全行のis_failureが再計算され、このクエリは次回実行時にその新しい数字を
-- そのまま拾う。分類を1箇所にまとめることの意味はまさにここにある。
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
DROP TABLE IF EXISTS cur.monthly_failure_downtime;

CREATE TABLE cur.monthly_failure_downtime AS
WITH real_failure_repairs AS (
    -- is_real_failure classification: read directly from
    -- cur.medical_device_repair_history.is_failure. This column is
    -- curated by the datacuration-curate pipeline using
    -- failure_classifier.py, the single source of truth for classification
    -- (ADR-2026-06-16, kept in sync with it as of task11, 2026-09-07). No
    -- classification logic of any kind is duplicated here anymore — see the
    -- REVISED 2026-09-08 (task9) note in the file header for the full
    -- reasoning and what this replaced.
    -- is_real_failure の判定は cur.medical_device_repair_history.is_failure
    -- を直接読む。このカラムは datacuration-curate パイプラインが
    -- failure_classifier.py（分類の唯一の正本、ADR-2026-06-16 に
    -- task11=2026-09-07時点で追従済み）で作る。判定ロジックはもうここには
    -- 一切複製しない — 詳しい経緯はファイル冒頭の2026-09-08（task9）の
    -- 注記を参照。
    SELECT
        r.medical_device_ledger_id,
        r.calculated_trouble_date,
        r.calculated_completion_date,
        r.is_completed,
        r.calculated_downtime_hours
    FROM cur.medical_device_repair_history r
    WHERE
        r.calculated_trouble_date IS NOT NULL
        AND r.is_failure = true
),

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
repair_months AS (
    SELECT
        f.medical_device_ledger_id,
        f.calculated_trouble_date,
        f.calculated_completion_date,
        f.is_completed,
        f.calculated_downtime_hours,
        gs.month_start::date AS month_start,
        (gs.month_start + interval '1 month')::timestamp AS month_end
    FROM real_failure_repairs f
    CROSS JOIN LATERAL generate_series(
        date_trunc('month', f.calculated_trouble_date),
        date_trunc(
            'month',
            COALESCE(
                CASE WHEN f.is_completed THEN f.calculated_completion_date END,
                CURRENT_DATE
            )
        ),
        interval '1 month'
    ) AS gs(month_start)
),

-- Downtime hours attributable to each month: the overlap between
-- [calculated_trouble_date, effective_end] and [month_start, month_end),
-- where a still-open repair's effective_end for THIS month is the month's
-- own end (still-open record rule, 2026-09-03 — applied per month rather
-- than per dashboard period, since on-premise's aggregation grain is fixed
-- at month).
-- FIXED 2026-09-04: for the still-open branch, effective_end is
-- LEAST(month_end, CURRENT_TIMESTAMP), not month_end alone. A past month
-- that has already fully elapsed is unaffected (CURRENT_TIMESTAMP is past
-- its month_end, so month_end still wins), but the CURRENT, in-progress
-- month is now capped at "now" instead of counting hours that have not
-- happened yet. Found via device 2's diagnostic query showing a full
-- 720.00 hours for the current month days before it had actually ended.
-- 各月に配分するダウンタイム時間: [calculated_trouble_date, effective_end]
-- と [month_start, month_end) の重なり。まだ完了していない修理の
-- effective_end は、その月の月末とする（2026-09-03 の「未完了レコードの
-- ルール」。オンプレの集計粒度が月固定のため、ダッシュボードの期間単位
-- ではなく月単位で適用）。
-- 2026-09-04 修正: 未完了側の effective_end は month_end 単体ではなく
-- LEAST(month_end, CURRENT_TIMESTAMP) とする。すでに完全に終わった過去の
-- 月には影響しない（CURRENT_TIMESTAMP が month_end を過ぎているので
-- month_end のまま）が、進行中の今月は「まだ起きていない時間」を数えず、
-- 「今」で打ち切るようになる。device 2 の診断クエリで、今月がまだ終わって
-- いないのに 720.00 時間（満月分）になっているのを見つけて発覚。
downtime_by_month AS (
    SELECT
        medical_device_ledger_id,
        month_start,
        is_completed,
        calculated_downtime_hours,
        GREATEST(
            0,
            EXTRACT(
                EPOCH FROM (
                    LEAST(
                        month_end,
                        CASE
                            WHEN is_completed THEN COALESCE(calculated_completion_date, month_end)
                            ELSE LEAST(month_end, CURRENT_TIMESTAMP)
                        END
                    )
                    - GREATEST(month_start::timestamp, calculated_trouble_date)
                )
            ) / 3600.0
        ) AS downtime_hours
    FROM repair_months
)

SELECT
    medical_device_ledger_id,
    month_start,
    ROUND(SUM(downtime_hours)::numeric, 2) AS downtime_hours
FROM downtime_by_month
GROUP BY medical_device_ledger_id, month_start
ORDER BY medical_device_ledger_id, month_start;

-- VALIDATION (per poc_metric_definition.md entry 4's どう確かめるか):
--   1. Pick 2-3 real medical_device_ledger_id values with known repairs,
--      including at least one still-open repair (is_completed = false).
--      Confirm downtime_hours is capped at each month's end, not left as
--      a gap and not extended past "now" some other way.
--   2. Spot-check a sample's is_failure value against its raw event_note /
--      failure_reason text, purely as a sanity check that the curated
--      column looks reasonable — this is no longer a recompute-vs-curated
--      comparison (removed 2026-09-08, task9; see the file header), just an
--      eyeball read, same as anyone would do to sanity-check any curated
--      column they now trust directly.
--   3. For completed repairs, cross-check this query's per-record total
--      (summed across the months it touches) against
--      calculated_downtime_hours, to see whether that column is a plain
--      date-diff or has some other adjustment baked in.
-- REMOVED 2026-09-08 (task9): the old cross-check 3 (recomputed vs curated
-- is_failure) no longer applies, since this SQL has nothing of its own left
-- to recompute — see the file header's 2026-09-08 note for why. The old
-- cross-check 4 is renumbered to 3 above.
-- 検証（poc_metric_definition.md の entry 4「どう確かめるか」に対応）:
--   1. 実際の medical_device_ledger_id を2〜3件選ぶ。まだ完了していない
--      修理（is_completed = false）を含む1件は必ず入れる。downtime_hours
--      が各月の月末で正しく切られているか確認する。
--   2. サンプルの is_failure の値を、event_note / failure_reason の生
--      テキストと突き合わせる。あくまで curated カラムが妥当そうかの目視
--      確認であり、再計算との比較ではない（2026-09-08, task9で削除。
--      ファイル冒頭参照）— 直接信頼するようになった他の curated カラムを
--      普段どおり目視確認するのと同じ位置づけ。
--   3. 完了済み修理について、このクエリの1件あたり合計（触れた月ごとの
--      合計）を calculated_downtime_hours と突き合わせ、そのカラムが単純な
--      日付差分なのか、他の補正が入っているのかを確認する。
-- 2026-09-08（task9）削除: 旧・検証3（再計算 vs curated is_failure）は
-- もう成立しない — このSQLにはもう自前で再計算するものが残っていない
-- ため（理由はファイル冒頭の2026-09-08の注記を参照）。旧・検証4を上で
-- 3番に繰り上げた。

-- Cross-check (per-record total vs calculated_downtime_hours, completed
-- real-failure repairs only). Standalone query — run separately.
-- 突き合わせ（1件あたり合計 vs calculated_downtime_hours、完了済みかつ
-- 真の故障のみ）。単独クエリ — 別途実行する。
--
-- SELECT
--     r.medical_device_ledger_id,
--     r.calculated_trouble_date,
--     r.calculated_completion_date,
--     r.calculated_downtime_hours AS existing_column_hours,
--     ROUND(
--         (EXTRACT(EPOCH FROM (r.calculated_completion_date - r.calculated_trouble_date)) / 3600.0)
--     ::numeric, 2) AS recomputed_hours_straight_diff
-- FROM cur.medical_device_repair_history r
-- WHERE
--     r.is_completed = true
--     AND r.calculated_trouble_date IS NOT NULL
--     AND r.is_failure = true
-- ORDER BY ABS(
--     COALESCE(r.calculated_downtime_hours, 0)
--     - EXTRACT(EPOCH FROM (r.calculated_completion_date - r.calculated_trouble_date)) / 3600.0
-- ) DESC
-- LIMIT 20;  -- biggest disagreements first

-- ============================================================
-- DIAGNOSTICS requested in the 2026-09-04 (09:34) review, run BEFORE the
-- validation above. These are not part of poc_metric_definition.md's
-- どう確かめるか list (that's the validation queries above) — they exist to
-- scope a data-quality question the review raised: whether the
-- month-explosion logic lets a very old still-open repair inflate a
-- single device's row count, before doing the 2-3 device spot-check.
-- 2026-09-04（09:34）レビューで依頼された診断クエリ。上記の検証より先に
-- 実行する。poc_metric_definition.md の「どう確かめるか」（検証クエリ）とは
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
SELECT *
FROM cur.monthly_failure_downtime
WHERE medical_device_ledger_id = 2
ORDER BY month_start;

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
SELECT
    EXTRACT(YEAR FROM calculated_trouble_date) AS trouble_year,
    COUNT(*) AS n_unfinished_repairs
FROM cur.medical_device_repair_history
WHERE
    is_completed = false
    OR calculated_completion_date IS NULL
GROUP BY 1
ORDER BY 1;

-- Diagnostic B2 (bonus, not literally asked for): same count, scoped to
-- just the is_real_failure population — this is the subset that actually
-- explodes into cur.monthly_failure_downtime, so it's a closer proxy for
-- "how much does this affect entry 4's output specifically" than
-- Diagnostic B's unscoped count. Worth having both: B answers Miyazawa-san's
-- question as asked, B2 answers "does this affect entry 4."
-- UPDATED 2026-09-08 (task9): scoped with is_failure = true directly,
-- instead of the removed keyword regex — same idea, same query shape, just
-- reading the curated column like everything else in this file now does.
-- 診断B2（おまけ、依頼された範囲ではない）: is_real_failure に絞った
-- 同じ集計 — cur.monthly_failure_downtime に実際に展開される対象なので、
-- 「entry 4の出力に具体的にどれだけ影響するか」により近い指標になる。
-- 診断Bは依頼どおりの範囲への回答、診断B2は「entry 4への影響」への回答、
-- 両方あった方がよい。
-- 2026-09-08（task9）更新: 削除したキーワード正規表現の代わりに
-- is_failure = true で直接絞る — 考え方・クエリの形は変わらず、この
-- ファイルの他の部分と同じく curated カラムをそのまま読むだけになった。
SELECT
    EXTRACT(YEAR FROM r.calculated_trouble_date) AS trouble_year,
    COUNT(*) AS n_unfinished_real_failure_repairs
FROM cur.medical_device_repair_history r
WHERE
    (r.is_completed = false OR r.calculated_completion_date IS NULL)
    AND r.calculated_trouble_date IS NOT NULL
    AND r.is_failure = true
GROUP BY 1
ORDER BY 1;