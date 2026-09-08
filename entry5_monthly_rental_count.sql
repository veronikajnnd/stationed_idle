-- WARNING: this script DROPs and rebuilds cur.monthly_rental_count —
-- running it deletes any existing data in that table without confirmation.
-- 注意: このスクリプトは cur.monthly_rental_count を DROP して作り直し
-- ます。実行すると、既存のデータは確認なしに削除されます。
-- ============================================================
-- Entry 5 (*機器別貸出回数[FIXED], rentals per unit) — vertical slice, draft
-- Entry 5（*機器別貸出回数[FIXED]）— 縦スライス、ドラフト
-- ============================================================
-- Output: device x hospital x department x month fact table of rental
-- counts. Unlike entry 4 (a duration that can span, and be split across,
-- multiple months), a rental COUNT is a single discrete event -- it is
-- bucketed into the one month it started in, not exploded across every
-- month it touches. See poc_metric_definition.md, entry 5, for the current
-- decision trail.
-- 出力: 機器 x 病院 x 部署 x 月ごとの貸出回数のfactテーブル。entry 4
-- （複数月にまたがり得る、分割が必要な「時間」）と違い、貸出回数は1回起きた
-- ら1回の離散イベント -- 開始した月1つだけに割り当てる。触れた月すべてに
-- 展開する entry 4 のロジックとは異なる。詳しい経緯は
-- poc_metric_definition.md の entry 5 を参照。
--
-- STATUS: this file's grouping/rules follow poc_metric_definition.md
-- entry 5's Phase2 の定義, which is still marked "draft, pending
-- confirmation" there -- unlike entry 4/9/10/11, this has NOT yet been
-- through a Miyazawa-san review cycle. Treat this SQL as a first pass to
-- validate against, not yet something to trust for a real number.
-- ステータス: このファイルの集計方法は poc_metric_definition.md の entry 5
-- 「Phase2 の定義」に沿っているが、そこはまだ「draft, pending
-- confirmation」のまま -- entry 4/9/10/11と違い、まだMiyazawaさんのレビュー
-- を一度も通っていない。このSQLは検証用の第一版であり、まだ本物の数字として
-- 信頼できる段階ではない。
--
-- REVISED 2026-09-08: schema confirmed via information_schema (43 columns,
-- cur.medical_device_rental_history). Resolves most of the original
-- ASSUMPTIONS below, and surfaces one new one, mirroring what task11 found
-- on medical_device_repair_history:
--   - CONFIRMED: medical_facility_id (integer, NOT NULL), medical_facility_name
--     (text), recipient_department (text), client_device_number (text) all
--     exist exactly as guessed.
--   - CONFIRMED: the table's real primary key is
--     medical_device_rental_history_id (bigint, NOT NULL) -- one row is one
--     rental record, so COUNT(*) is a valid stand-in for what Phase2 の定義
--     calls "COUNT(rental_id)". The actual rental_id column is nullable
--     text (likely a source-system id, not this table's own key) -- not
--     safe to COUNT() directly since it can be null.
--   - NEW FINDING: the table carries BOTH raw (rental_start_date,
--     return_date) and curated (calculated_rental_start_date,
--     calculated_return_date) date columns, the same raw-vs-calculated
--     split found on medical_device_repair_history during task11/entry 4 --
--     there, the calculated_* columns were the correct ones to use. This
--     file now uses calculated_rental_start_date/calculated_return_date by
--     default, on that precedent, but this has NOT been separately
--     confirmed for the rental table specifically -- see the sanity-check
--     query below, run it before trusting the switch.
--   - Also present: is_returned (boolean, NOT NULL, the rental equivalent of
--     repair_history's is_completed) and rental_duration_hours (numeric,
--     already computed, the rental equivalent of calculated_downtime_hours)
--     -- neither is needed for entry 5 itself (a plain count doesn't need a
--     still-open gate, per the still-open record rule), but both are worth
--     remembering for entries 7/19 (operating hours) later.
-- 2026-09-08 改訂: information_schema でスキーマ確認済み（43カラム、
-- cur.medical_device_rental_history）。下記の当初の前提のほとんどが解決し、
-- task11で medical_device_repair_history に見つかったのと同じパターンの
-- 新しい論点が1つ出てきた:
--   - 確認済み: medical_facility_id（integer, NOT NULL）、
--     medical_facility_name（text）、recipient_department（text）、
--     client_device_number（text）はすべて想定どおり実在する。
--   - 確認済み: このテーブルの実際の主キーは
--     medical_device_rental_history_id（bigint, NOT NULL）-- 1行=1貸出記録
--     なので、COUNT(*) は Phase2の定義がいう「COUNT(rental_id)」の代わりと
--     して有効。実際の rental_id カラムは nullable な text（おそらく
--     ソースシステム側のid、このテーブル自身のキーではない）-- null になり
--     得るので直接 COUNT() するのは安全ではない。
--   - 新しい発見: このテーブルには生カラム（rental_start_date,
--     return_date）と curated カラム（calculated_rental_start_date,
--     calculated_return_date）の両方がある。task11/entry 4で
--     medical_device_repair_history に見つかったのと同じ「生 vs
--     curated」の分裂で、あちらでは calculated_* を使うのが正解だった。
--     このファイルはその前例に基づき calculated_rental_start_date /
--     calculated_return_date をデフォルトで使うことにしたが、レンタル
--     テーブルについて個別に確認したわけではない -- 下記のサニティ
--     チェッククエリを、信頼する前に実行すること。
--   - また is_returned（boolean, NOT NULL、repair_historyのis_completedに
--     相当）と rental_duration_hours（numeric、既に計算済み、
--     calculated_downtime_hoursに相当）も存在する -- entry 5自体（単純な
--     カウント）には不要（カウントにはis_completedのようなゲートは不要、
--     未完了レコードのルールどおり）だが、後のentry 7/19（稼働時間）で
--     覚えておく価値あり。
--
-- ASSUMPTIONS STILL TO VERIFY BEFORE TRUSTING THE OUTPUT:
--   - calculated_rental_start_date/calculated_return_date vs the raw
--     columns: adopted by precedent, not independently confirmed for this
--     table. Run the sanity-check query below first.
--   - client_device_number vs 管理No: entry 5's own note says this mapping
--     is "assumed, pending Miyazawa-san's explicit confirmation" -- not
--     resolved by today's schema check, though client_device_number's known
--     fill rate (100% vs device_number's 0%, per poc_metric_definition.md)
--     still makes it the only usable candidate regardless of the naming
--     question.
--   - "One count per rental record regardless of month span" (Phase2 の
--     定義) is implemented here as: bucket by the month
--     calculated_rental_start_date falls in, full stop -- no split, no
--     second row for the return month. This is a reading of the draft
--     definition, not something Miyazawa-san has confirmed as the intended
--     semantics yet -- confirm via Option A below (a device with a
--     month-spanning rental, counted once not twice).
-- 出力結果を信頼する前に確認すべき前提:
--   - calculated_rental_start_date/calculated_return_date と生カラムの
--     関係: 前例に基づき採用したが、このテーブルで個別に確認したわけでは
--     ない。下記のサニティチェッククエリを先に実行すること。
--   - client_device_number と 管理No の対応: entry 5自身のメモに
--     「仮定、Miyazawaさんの明示確認待ち」とある -- 今回のスキーマ確認では
--     解決しない。ただし client_device_number の充足率（100%、
--     device_number は0%、poc_metric_definition.md記載）を考えると、
--     命名の対応関係にかかわらず使える候補はこちらしかない。
--   - 「月をまたいでも貸出記録1件につき1カウント」（Phase2の定義）は、ここ
--     では calculated_rental_start_date が属する月にそのまま割り当てる
--     実装にした -- 分割なし、返却月への2行目もなし。これはドラフト定義の
--     一つの解釈であり、Miyazawaさんが意図した意味として確認済みではない
--     -- 下記のOption A（月をまたぐ貸出が1回だけカウントされることの
--     確認）で検証する。
--
-- OUTPUT: writes the fact into cur.monthly_rental_count, mirroring
-- entry 4's cur/pub split (CONFIRMED 2026-09-04 for entry 4, assumed to
-- apply the same way here -- not yet separately confirmed for entry 5).
-- 出力: 結果を cur.monthly_rental_count に書き込む。entry 4 の
-- cur/pub分離（2026-09-04確認済み）に倣う想定 -- entry 5について個別に
-- 確認したわけではない。

-- ============================================================
-- SANITY CHECK -- run this FIRST. Compares the raw and calculated_*
-- start/return dates: how many rows disagree (different, non-null values
-- on both sides) vs how many are null on one side but not the other.
-- A small/zero disagreement count means the choice of calculated_* over
-- raw barely matters here; a large one means it matters a lot, and is
-- worth understanding why before trusting either column.
-- サニティチェック -- 最初にこれを実行する。生カラムと calculated_*
-- カラムの開始日/返却日を比較: 両方に値があって食い違う行数と、片方だけ
-- null の行数を見る。食い違いがほぼゼロなら calculated_* を使うかどうかは
-- ほぼ影響しない。多ければ、どちらかを信頼する前に理由を理解する価値がある。
-- ============================================================
-- SELECT
--     COUNT(*) FILTER (
--         WHERE rental_start_date IS NOT NULL
--           AND calculated_rental_start_date IS NOT NULL
--           AND rental_start_date <> calculated_rental_start_date
--     ) AS start_date_disagrees,
--     COUNT(*) FILTER (
--         WHERE (rental_start_date IS NULL) <> (calculated_rental_start_date IS NULL)
--     ) AS start_date_null_mismatch,
--     COUNT(*) FILTER (
--         WHERE return_date IS NOT NULL
--           AND calculated_return_date IS NOT NULL
--           AND return_date <> calculated_return_date
--     ) AS return_date_disagrees,
--     COUNT(*) FILTER (
--         WHERE (return_date IS NULL) <> (calculated_return_date IS NULL)
--     ) AS return_date_null_mismatch,
--     COUNT(*) AS total_rows
-- FROM cur.medical_device_rental_history;

DROP TABLE IF EXISTS cur.monthly_rental_count;

CREATE TABLE cur.monthly_rental_count AS
SELECT
    r.client_device_number,
    r.medical_facility_id,
    r.medical_facility_name,
    r.recipient_department,
    date_trunc('month', r.calculated_rental_start_date)::date AS month_start,
    COUNT(*) AS rental_count
FROM cur.medical_device_rental_history r
WHERE r.calculated_rental_start_date IS NOT NULL
GROUP BY
    r.client_device_number,
    r.medical_facility_id,
    r.medical_facility_name,
    r.recipient_department,
    date_trunc('month', r.calculated_rental_start_date)
ORDER BY
    r.client_device_number,
    month_start;

-- VALIDATION (per poc_metric_definition.md entry 5's どう確かめるか,
-- recommendation: B first, cheap and catches systemic bugs, then A on a
-- handful of devices):
-- 検証（poc_metric_definition.md の entry 5「どう確かめるか」に対応。
-- 推奨順: まずB（安価で、join/dedupの系統的なバグを捉えられる）、その後A）:

-- Option B: total reconciliation. sum of this fact table's rental_count
-- must equal a plain COUNT(*) of the source table (same non-null
-- calculated_rental_start_date filter, no period restriction on either
-- side -- this checks the GROUP BY/JOIN didn't drop or duplicate rows,
-- nothing about period filtering yet since that's Superset's job
-- downstream).
-- B: 全体突き合わせ。このfactテーブルのrental_countの合計は、元テーブルの
-- 単純な COUNT(*)（同じ non-null calculated_rental_start_date 条件、期間に
-- よる絞り込みはどちらもなし）と一致するはず -- GROUP BY/JOINで行が消えたり
-- 重複したりしていないかの確認。期間フィルタ自体はSupersetの仕事なのでここ
-- では扱わない。
SELECT
    (SELECT SUM(rental_count) FROM cur.monthly_rental_count) AS table_total,
    (SELECT COUNT(*) FROM cur.medical_device_rental_history WHERE calculated_rental_start_date IS NOT NULL) AS source_table_total;

-- Option A: manual spot-check, 5-10 sample devices including at least one
-- rental that spans a month boundary (calculated_rental_start_date and
-- calculated_return_date in different months) -- confirm it's counted ONCE
-- (in its start month), not twice, per the "no explode-by-month" reading
-- above.
-- A: 手動スポットチェック、5〜10台のサンプル機器。月をまたぐ貸出
-- （calculated_rental_start_date と calculated_return_date が違う月）を
-- 最低1件含める -- 上記の「月展開しない」という解釈どおり、開始月に1回だけ
-- カウントされていることを確認する（2回にならないこと）。
--
-- Step 1: find a device with a month-spanning rental, to pick as one of the
-- samples.
-- ステップ1: 月をまたぐ貸出がある機器を、サンプルの1つとして探す。
--
SELECT client_device_number, calculated_rental_start_date, calculated_return_date
FROM cur.medical_device_rental_history
WHERE
    calculated_return_date IS NOT NULL
    AND date_trunc('month', calculated_rental_start_date) <> date_trunc('month', calculated_return_date)
LIMIT 10;
--
-- Step 2: for each sampled client_device_number, compare the fact table's
-- per-month counts against a manual count from the raw table.
-- ステップ2: サンプルに選んだ client_device_number ごとに、factテーブルの
-- 月別カウントと、生テーブルからの手動カウントを突き合わせる。
--
-- SELECT
--     client_device_number,
--     date_trunc('month', calculated_rental_start_date)::date AS month_start,
--     COUNT(*) AS manual_count
-- FROM cur.medical_device_rental_history
-- WHERE
--     client_device_number = :sample_device  -- fill in one id at a time
--     AND calculated_rental_start_date IS NOT NULL
-- GROUP BY 1, 2
-- ORDER BY 1, 2;