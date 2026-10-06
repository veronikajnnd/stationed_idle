-- WARNING: this script DROPs and rebuilds cur.monthly_rental_count —
-- running it deletes any existing data in that table without confirmation.
-- 注意: このスクリプトは cur.monthly_rental_count を DROP して作り直し
-- ます。実行すると、既存のデータは確認なしに削除されます。
-- ============================================================
-- Entry 5 (*機器別貸出回数[FIXED], rentals per unit) — vertical slice, draft
-- Entry 5（*機器別貸出回数[FIXED]）— 縦スライス、ドラフト
-- ============================================================
-- Output (since 2026-10-06): one row per rental record per calendar month it
-- touches, carrying the record's identifier and the rental hours of that
-- month (rental_hours). There is NO count column: the screen counts rentals
-- with COUNT(DISTINCT medical_device_rental_history_id) (see
-- monthly_fact_template.md sections 3 to 5). The hours serve entry 9
-- (rental headroom, committed time = rental hours + recovery time) and
-- entries 7/19/33. See poc_metric_definition.md, entry 5 and entry 9, for the
-- decision trail. The table name is kept as it is; it no longer holds a count.
-- 出力（2026-10-06 以降）: 貸出レコード x それが触れる暦月ごとに1行。レコードの
-- 識別子と、その月の貸出時間（rental_hours）を持つ。回数の列は置かない:
-- 画面側が COUNT(DISTINCT medical_device_rental_history_id) で数える
-- （monthly_fact_template.md の3〜5節）。時間は entry 9（貸出余力、拘束時間 =
-- 貸出時間 + リカバリ時間）と entry 7/19/33 のために使う。経緯は
-- poc_metric_definition.md の entry 5 と entry 9 を参照。テーブル名は変えない
-- （もう回数は入っていない）。
--
-- REVISED 2026-10-06 (task12, entry 9): the fact now follows
-- monthly_fact_template.md (Miyazawa-san, 2026-09-09), which says entries 4 and
-- 5 came out in different shapes by guesswork and fixes the shape. Changes:
--   1. rental_hours added (the column entry 9 needs). A rental spanning
--      several months now has one row per month, hours cut to that month.
--   2. medical_device_rental_history_id added as the identifier; the
--      rental_count column is removed (template section 3: a count is an
--      answer, not a fact). Rentals are counted on the screen with
--      COUNT(DISTINCT id), so the same table answers "how many in March" and
--      "how many this year".
--   3. The device key is medical_device_ledger_id instead of
--      client_device_number (template section 6: client_device_number is the
--      facility's own value and collides across facilities).
--      medical_device_ledger_id IS NOT NULL is written explicitly until
--      ADR-2026-09-09 is applied.
--   4. Hours convention for rentals (date-only records): a return date with
--      time exactly 00:00:00 means the end of that day, so the return day is
--      counted in full, as in the template's worked example (3/31 -> 4/2 =
--      24 h in March + 48 h in April). A start is the beginning of its day. A
--      time that was recorded is used as is. A rental with no return date is
--      still open: included, cut at month end and at now, nothing added. The
--      repair 09:00 / 17:00 rule (2026-10-06) does not apply to rentals.
--      Cross-check this convention against rental_duration_hours (query 3 in
--      the validation section below) before trusting it.
--   5. The old rule "bucket the count into the start month only, no month
--      explosion" is superseded: the count is now derived from the rows.
-- 2026-10-06 修正（task12、entry 9）: このfactを monthly_fact_template.md
-- （Miyazawaさん、2026-09-09）の形に合わせた。同文書は、entry 4 と entry 5 が推測で
-- 違う形になったと述べ、形を決めている。変更点:
--   1. rental_hours を追加（entry 9 が必要とする列）。複数の月にまたがる貸出は
--      月ごとに1行になり、時間はその月に切って入る。
--   2. 識別子として medical_device_rental_history_id を追加し、rental_count 列を
--      削除（テンプレートの3節: 回数は事実ではなく答え）。貸出の数は画面側が
--      COUNT(DISTINCT id) で数える。同じテーブルで「3月は何回」も「今年は何回」も
--      答えられる。
--   3. 機器のキーを client_device_number から medical_device_ledger_id に変更
--      （テンプレートの6節: client_device_number は施設側の値で、施設をまたぐと
--      衝突する）。ADR-2026-09-09 の適用までは medical_device_ledger_id IS NOT NULL
--      を明示する。
--   4. 貸出（日付だけの記録）の時間の決め方: 返却日の時刻がちょうど 00:00:00 なら
--      「その日の終わり」と読み、返却日の日もまるごと数える（テンプレートの
--      worked example: 3/31 -> 4/2 = 3月 24 h + 4月 48 h）。開始は、その日の始まり。
--      時刻が入っていればそのまま使う。返却日が無い貸出は未完了: 除外せず、月末と
--      「今」で切って含め、何も足さない。修理の 09:00 / 17:00 ルール（2026-10-06）は
--      貸出には使わない。この決め方は、信頼する前に rental_duration_hours と
--      突き合わせること（下の検証の3番）。
--   5. 旧ルール「回数は開始月だけに割り当てる、月に展開しない」は上書き: 回数は
--      行から導く。
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

-- REVISED 2026-09-09 (task9 follow-up, Miyazawa-san's entry5 review):
-- medical_facility_name was dropped from this GROUP BY / SELECT.
-- 診断クエリ (COUNT(DISTINCT medical_facility_name) per medical_facility_id)
-- を実データで実行した結果、medical_facility_id=1 の distinct_names は 0 件、
-- つまりこのカラムは全行 NULL で、実質的に情報を持っていない。GROUP BY に
-- 残しても現時点では行の分割は起きないが、"意味のない列を念のため引き回す"
-- のはentry4のcurated_is_failure/curated_repair_classificationと同じ形の
-- 問題（方向は逆で、今回は「追加してはいけない列」）なので、確認できた時点で
-- 削除する。将来 medical_facility_name が実際に埋まるようになった場合は、
-- 1つの medical_facility_id が複数の名前を持ちうるかを再度確認してから
-- GROUP BY に戻すこと。
--
-- Diagnostic query run against real data (COUNT(DISTINCT medical_facility_name)
-- per medical_facility_id) showed medical_facility_id=1 has distinct_names=0 --
-- meaning this column is NULL for 100% of rows, carrying no information today.
-- Leaving it in the GROUP BY wasn't wrong (it wasn't splitting any rows), but
-- it's the same shape of issue as entry4's curated_is_failure /
-- curated_repair_classification (a column threaded through "just in case"),
-- just in the opposite direction (a column that should never have been added
-- rather than one that should have been removed). Dropped now that it's
-- confirmed. If medical_facility_name is ever populated for real, re-check
-- whether one medical_facility_id can map to multiple names before adding it
-- back to the GROUP BY.
-- (2026-10-06: the statement below has no GROUP BY any more; the note above
-- is kept as the record of why medical_facility_name is not carried.)
-- （2026-10-06: 下の文には GROUP BY がもう無い。上の注記は、medical_facility_name
-- を持たない理由の記録として残す。）
CREATE TABLE cur.monthly_rental_count AS
WITH rentals AS (
    -- One row per rental record. rental_start is the start (a date-only start
    -- is the beginning of its day). rental_end is the end of the rental: a
    -- return date whose time is exactly 00:00:00 is date-only, so it means the
    -- END of that day (the next day 00:00); a recorded time is used as is; no
    -- return date means the rental is still open, so it runs up to now (the
    -- still-open record rule, 2026-09-03). The double cast makes this work
    -- whether the column is a date or a timestamp.
    -- 貸出レコードごとに1行。rental_start は開始（日付だけの開始は、その日の始まり）。
    -- rental_end は貸出の終わり: 返却日の時刻がちょうど 00:00:00 なら日付だけの
    -- 記録なので「その日の終わり」（翌日の 00:00）。時刻が入っていればそのまま。
    -- 返却日が無ければ未完了なので「今」まで（未完了レコードのルール、2026-09-03）。
    -- 二重キャストは、列が date でも timestamp でも動くようにするため。
    SELECT
        r.medical_device_rental_history_id,
        r.medical_device_ledger_id,
        r.medical_facility_id,
        r.recipient_department,
        r.calculated_rental_start_date::timestamp AS rental_start,
        CASE
            WHEN r.calculated_return_date IS NULL
                THEN LOCALTIMESTAMP
            WHEN r.calculated_return_date::timestamp::time = TIME '00:00:00'
                THEN r.calculated_return_date::date + 1
            ELSE r.calculated_return_date::timestamp
        END AS rental_end
    FROM cur.medical_device_rental_history r
    -- medical_device_ledger_id IS NOT NULL: monthly_fact_template.md section 6
    -- until ADR-2026-09-09 is applied (then the column is NOT NULL and this
    -- condition can be removed). A rental not linked to the ledger is the
    -- rental time of no device.
    -- medical_device_ledger_id IS NOT NULL: ADR-2026-09-09 の適用までは
    -- monthly_fact_template.md の6節に従って明示する（適用後は列が NOT NULL に
    -- なるので、この条件は外してよい）。台帳に紐付かない貸出は、どの機器の貸出
    -- 時間でもない。
    WHERE r.calculated_rental_start_date IS NOT NULL
      AND r.medical_device_ledger_id IS NOT NULL
),

-- One row per (rental, calendar month it touches). The series runs from the
-- start month to the month of the last instant of the rental (rental_end minus
-- one microsecond, so a rental ending exactly at midnight does not touch the
-- next month). GREATEST keeps the start month even if the return date is before
-- the start (a broken record keeps its identifier, with 0 h). A very old
-- still-open rental produces a row for every month up to now (see the
-- diagnostic in the validation section).
-- 貸出 x それが触れる暦月ごとに1行。系列は開始月から、貸出の最後の瞬間の月
-- （rental_end の1マイクロ秒前。ちょうど 0 時に終わる貸出が翌月に触れないように）
-- まで。GREATEST で、返却日が開始日より前でも開始月は残る（壊れた記録も識別子を
-- 失わず、0 時間になる）。非常に古い未完了の貸出は、今月まですべての月に行を
-- 作る（検証の節の診断を参照）。
rental_months AS (
    SELECT
        x.medical_device_rental_history_id,
        x.medical_device_ledger_id,
        x.medical_facility_id,
        x.recipient_department,
        x.rental_start,
        x.rental_end,
        gs.month_start::date AS month_start,
        (gs.month_start + interval '1 month') AS month_end
    FROM rentals x
    CROSS JOIN LATERAL generate_series(
        date_trunc('month', x.rental_start),
        GREATEST(
            date_trunc('month', x.rental_start),
            date_trunc('month', x.rental_end - interval '1 microsecond')
        ),
        interval '1 month'
    ) AS gs(month_start)
)

-- rental_hours: the overlap between [rental_start, rental_end] and the month
-- [month_start, month_end), in hours, never negative. Rows are unique per
-- (rental, month) by construction, so no GROUP BY is needed.
-- rental_hours: [rental_start, rental_end] とその月 [month_start, month_end) の
-- 重なりを時間で。負にはならない。行は構造上 (貸出, 月) ごとに一意なので
-- GROUP BY は要らない。
SELECT
    medical_device_ledger_id,
    medical_facility_id,
    recipient_department,
    month_start,
    medical_device_rental_history_id,
    ROUND(
        GREATEST(
            0,
            EXTRACT(
                EPOCH FROM (
                    LEAST(month_end, rental_end)
                    - GREATEST(month_start::timestamp, rental_start)
                )
            ) / 3600.0
        )::numeric,
        2
    ) AS rental_hours
FROM rental_months
ORDER BY
    medical_device_ledger_id,
    month_start,
    medical_device_rental_history_id;

-- ============================================================
-- VALIDATION OF THE SHAPE SINCE 2026-10-06 (monthly_fact_template.md section 4
-- and 7). Run after building the table. Each query is read-only.
-- 2026-10-06 以降の形の検証（monthly_fact_template.md の4節と7節）。テーブルを
-- 作ってから実行する。どのクエリも読み取り専用。
-- ============================================================
-- 1. Every rental (with a start date and a ledger id) has at least one row:
--    the two numbers must be equal. This catches a wrong grain.
-- 1. 開始日と ledger id を持つ貸出はすべて、少なくとも1行ある: 2つの数字は一致する
--    はず。粒度の誤りを捕まえられる。（このクエリだけは、旧い形の Option B と同じく
--    ファイルを実行すると走る。）
-- This one runs when the file is run, as the old Option B did.
SELECT
    (SELECT COUNT(DISTINCT medical_device_rental_history_id) FROM cur.monthly_rental_count) AS fact_rentals,
    (SELECT COUNT(*) FROM cur.medical_device_rental_history
     WHERE calculated_rental_start_date IS NOT NULL
       AND medical_device_ledger_id IS NOT NULL) AS source_rentals;
--
-- 2. One row per (rental, month): this must return 0 rows.
-- 2. 貸出 x 月ごとに1行: 0行が返るはず。
-- SELECT medical_device_rental_history_id, month_start, COUNT(*)
-- FROM cur.monthly_rental_count
-- GROUP BY 1, 2
-- HAVING COUNT(*) > 1;
--
-- 3. Hours convention: for returned rentals, compare the total of the rows of
--    one rental with the stored rental_duration_hours. "equal" means this SQL
--    and the stored column agree; "one_day_more" means the stored column does
--    not count the return day (this SQL counts it, following the template's
--    worked example); anything else needs a look before trusting the hours.
-- 3. 時間の決め方: 返却済みの貸出について、1件の行の合計を保存済みの
--    rental_duration_hours と比べる。equal は、このSQLと保存済みの列が一致。
--    one_day_more は、保存済みの列は返却日を数えない（このSQLは、テンプレートの
--    worked example に従って数える）。それ以外は、時間を信頼する前に確認が必要。
-- WITH f AS (
--     SELECT medical_device_rental_history_id AS id, SUM(rental_hours) AS fact_hours
--     FROM cur.monthly_rental_count
--     GROUP BY 1
-- )
-- SELECT
--     COUNT(*) AS returned_rentals,
--     COUNT(*) FILTER (WHERE abs(f.fact_hours - r.rental_duration_hours) < 0.02) AS equal,
--     COUNT(*) FILTER (WHERE abs(f.fact_hours - 24 - r.rental_duration_hours) < 0.02) AS one_day_more,
--     COUNT(*) FILTER (WHERE r.rental_duration_hours IS NULL) AS stored_hours_null
-- FROM cur.medical_device_rental_history r
-- JOIN f ON f.id = r.medical_device_rental_history_id
-- WHERE r.calculated_return_date IS NOT NULL;
--
-- 4. Diagnostic (same idea as entry 4's diagnostic B): still-open rentals by
--    start year. A very old still-open rental produces one row per month up to
--    now, so this shows how many rows come from open rentals.
-- 4. 診断（entry 4 の診断Bと同じ考え方）: 未完了の貸出を開始年ごとに数える。
--    非常に古い未完了の貸出は今月まで毎月1行を作るので、未完了の貸出から来る
--    行がどれだけあるかを見る。
-- SELECT EXTRACT(YEAR FROM calculated_rental_start_date) AS start_year, COUNT(*) AS open_rentals
-- FROM cur.medical_device_rental_history
-- WHERE calculated_return_date IS NULL
--   AND calculated_rental_start_date IS NOT NULL
--   AND medical_device_ledger_id IS NOT NULL
-- GROUP BY 1
-- ORDER BY 1;
--
-- ============================================================
-- OLD SHAPE (until 2026-10-05), kept as a record. The queries below refer to
-- rental_count and client_device_number, which no longer exist in the table,
-- so they do not run against the new table as they are.
-- 旧い形（2026-10-05 まで）。記録として残す。下のクエリは、もう存在しない
-- rental_count と client_device_number を参照しているので、そのままでは新しい
-- テーブルに対して動かない。
-- ============================================================
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
-- (Commented out 2026-10-06: it reads rental_count, which no longer exists. The
-- new query 1 above does the same job for the new shape.)
-- （2026-10-06 にコメントアウト: もう存在しない rental_count を読むため。新しい形では
-- 上の1番が同じ役目を果たす。）
-- SELECT
--     (SELECT SUM(rental_count) FROM cur.monthly_rental_count) AS fact_table_total,
--     (SELECT COUNT(*) FROM cur.medical_device_rental_history WHERE calculated_rental_start_date IS NOT NULL) AS source_table_total;

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
-- SELECT client_device_number, calculated_rental_start_date, calculated_return_date
-- FROM cur.medical_device_rental_history
-- WHERE
--     calculated_return_date IS NOT NULL
--     AND date_trunc('month', calculated_rental_start_date) <> date_trunc('month', calculated_return_date)
-- LIMIT 10;
--
-- NOTE (2026-09-09, Miyazawa-san's entry5 review follow-up): the query above,
-- and the first attempt at excluding it (adding
-- "AND calculated_rental_start_date <> '2026-03-31'"), both still return only
-- March->April boundary cases -- the excluded version just lands on
-- 2026-03-30 instead of 2026-03-31, a different day but the SAME month pair.
-- All 10 candidates the first version returned happened to share the exact
-- date 2026-03-31, so 6 samples drawn from that list tested one boundary
-- type, not 6 varied ones, exactly as Miyazawa-san's review pointed out. To
-- actually get a different kind of boundary (a different pair of adjacent
-- months, ideally a year rollover), exclude the whole month of March instead
-- of one date, or search directly for a December->January case:
-- 備考（2026-09-09、entry5レビューのフォローアップ）: 上のクエリも、それを
-- 「AND calculated_rental_start_date <> '2026-03-31'」で除外した最初の対応も、
-- どちらも3月→4月の境界しか返さない -- 除外後は2026-03-30になるだけで、
-- 日付が違うだけで同じ月の組み合わせのまま。最初のクエリが返した10件は
-- すべて同じ日付2026-03-31だったので、そこから選んだ6件のサンプルは
-- 1種類の境界しかテストしておらず、Miyazawa-san のレビュー指摘どおりだった。
-- 本当に別種の境界（できれば年をまたぐ12月→1月）を得るには、1つの日付では
-- なく3月全体を除外するか、12月→1月のケースを直接検索する:
--
-- -- Option 1: exclude the entire month of March, not just one date
-- SELECT client_device_number, calculated_rental_start_date, calculated_return_date
-- FROM cur.medical_device_rental_history
-- WHERE
--     calculated_return_date IS NOT NULL
--     AND date_trunc('month', calculated_rental_start_date) <> date_trunc('month', calculated_return_date)
--     AND date_trunc('month', calculated_rental_start_date) <> '2026-03-01'
-- LIMIT 10;
--
-- -- Option 2: search specifically for a year-end rollover (Dec -> Jan)
-- SELECT client_device_number, calculated_rental_start_date, calculated_return_date
-- FROM cur.medical_device_rental_history
-- WHERE
--     calculated_return_date IS NOT NULL
--     AND EXTRACT(MONTH FROM calculated_rental_start_date) = 12
--     AND EXTRACT(MONTH FROM calculated_return_date) = 1
-- LIMIT 10;
--
-- Result 2026-09-10: Option 1 (exclude March start) surfaced a genuinely
-- different boundary, Feb->Mar (calculated_rental_start_date = 2026-02-28,
-- e.g. CV186, IP571, IP409, IP573, IP617, IP689, UN223, DM755, IP659,
-- SP1165). Option 2 (Dec->Jan filter) also matched real data (start
-- 2025-12-28, e.g. IP845, FP055, IP587, CV199, SP882, NO004, NM040,
-- SP1111, SP1140, IP737) -- a genuine year rollover, not just a different
-- day within the same month pair. Both give Step 2 a real second and third
-- boundary type to test, on top of the March->April set from 2026-09-08.
-- 2026-09-10の結果: Option 1（開始月が3月のものを除外）は本当に別種の境界
-- （2月→3月、calculated_rental_start_date = 2026-02-28、例: CV186, IP571,
-- IP409ほか）を返した。Option 2（12月→1月フィルタ）も実データに一致
-- （開始2025-12-28、例: IP845, FP055, IP587ほか）-- 単に同じ月の組み合わせの
-- 別の日ではなく、本当に年をまたぐケース。これでStep2は2026-09-08の
-- 3月→4月のセットに加えて、本当に別の2種類の境界でテストできる。
--
-- Step 2 (corrected 2026-09-08 after two false starts -- see the task9/
-- entry5 worklog for the full story): compare the fact table's counts
-- against a manual count from the raw table, at the SAME grain the fact
-- table actually uses (device x facility x department x month, not just
-- device x month -- an earlier attempt grouped too coarsely and produced a
-- misleading row-count mismatch that looked like a bug but wasn't), and
-- with BOTH sides of the join scoped to the sample device list (an earlier
-- attempt only filtered the manual side, so the FULL OUTER JOIN matched
-- against the entire 224k-row fact table and produced ~224k false
-- "mismatches" -- every other device with nothing to match against).
-- This is a diff-style query: it returns ONLY rows where the two sides
-- disagree, so an empty result (0 rows) is success, not a null result.
-- ステップ2（2026-09-08、2回の空振りの後に修正 -- 経緯はtask9/entry5の
-- worklog参照）: factテーブルの実際のgrain（device x facility x
-- department x 月。device x 月だけではない -- 最初の試みは粗すぎる粒度で
-- グルーピングしてしまい、バグに見えるが実はバグではない行数の不一致を
-- 出してしまった）に合わせて、生テーブルからの手動カウントと突き合わせる。
-- 両サイドともサンプル機器リストで絞り込む（最初の試みはmanual側だけ絞って
-- おり、FULL OUTER JOIN がfactテーブル全体（22万行超）と突き合わさり、
-- 約22万件の偽の「不一致」を出してしまった -- 単に比較対象がない他の
-- 機器たち）。これは差分クエリなので、両者が食い違う行だけを返す --
-- 空（0行）が成功であり、結果が無いという意味ではない。
--
-- NOTE: ":sample_devices" below is a plain-text placeholder for this comment
-- block, not a psql variable -- copy the query out, delete the leading "--"
-- on each line, and replace BOTH ":sample_devices" occurrences with a real
-- literal list (e.g. 'CV181','IP775') before running it. Running it as-is
-- (or with \set sample_devices unset) raises a syntax error at ":".
-- 注意: 以下の ":sample_devices" はこのコメント内のプレースホルダーであり、
-- psql変数ではない -- クエリ本体をコピーし、各行先頭の"--"を削除した上で、
-- ":sample_devices" の2箇所を実際のリテラルのリスト（例: 'CV181','IP775'）に
-- 置き換えてから実行すること。そのまま実行する（\set sample_devices を
-- 設定しないまま実行する）と ":" 付近で構文エラーになる。
--
-- WITH manual AS (
--     SELECT
--         client_device_number,
--         medical_facility_id,
--         recipient_department,
--         date_trunc('month', calculated_rental_start_date)::date AS month_start,
--         COUNT(*) AS manual_count
--     FROM cur.medical_device_rental_history
--     WHERE
--         client_device_number IN (:sample_devices)  -- e.g. 'CV181','IP775',...
--         AND calculated_rental_start_date IS NOT NULL
--     GROUP BY 1, 2, 3, 4
-- ),
-- fact AS (
--     SELECT client_device_number, medical_facility_id, recipient_department, month_start, rental_count
--     FROM cur.monthly_rental_count
--     WHERE client_device_number IN (:sample_devices)
-- )
-- SELECT
--     COALESCE(m.client_device_number, f.client_device_number) AS client_device_number,
--     COALESCE(m.medical_facility_id, f.medical_facility_id) AS medical_facility_id,
--     COALESCE(m.recipient_department, f.recipient_department) AS recipient_department,
--     COALESCE(m.month_start, f.month_start) AS month_start,
--     m.manual_count,
--     f.rental_count AS fact_count
-- FROM manual m
-- FULL OUTER JOIN fact f
--     ON f.client_device_number = m.client_device_number
--    AND f.medical_facility_id = m.medical_facility_id
--    AND f.recipient_department IS NOT DISTINCT FROM m.recipient_department
--    AND f.month_start = m.month_start
-- WHERE m.manual_count IS DISTINCT FROM f.rental_count;
--
-- Result 2026-09-08: 0 rows, first against 2 sample devices (CV181, IP775),
-- then re-run against 6 (CV181, IP775, FT016, IP773, IP530, SP1122), all
-- drawn from Step 1's month-spanning candidates. See the task9/entry5
-- worklog for the full output and analysis.
-- 2026-09-08の結果: 0行。まず2台（CV181, IP775）、次に6台
-- （CV181, IP775, FT016, IP773, IP530, SP1122、いずれもStep1で見つけた
-- 月をまたぐ候補）で再実行、どちらも0行。詳しい出力と分析はtask9/entry5の
-- worklog参照。