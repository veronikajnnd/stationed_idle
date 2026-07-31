-- ============================================================================
-- ① is_stationed (機器単位)
-- Ref: ADR-2026-07-27-idle-utilization-metric-definitions §5
--      ADR-2026-05-01-report-base-period-resolution
--
-- 使い方: psql -f 01_is_stationed.sql
--
-- 2026-07-29 更新 (2周目、実行時エラー対応):
--   - CTE は statement をまたいで参照できない (WITH は1文だけ有効)。
--     複数の SELECT で使い回すため CREATE TEMP TABLE 方式に変更。
--   - me.deleted_at は実在しないと判明 (information_schema で18列確認、該当なし)。
--     miyazawa へ確認中。確認取れるまでフィルタ無しで進める (TODO 明記)。
-- ============================================================================
-- ① is_stationed (Per Device)
-- Ref: ADR-2026-07-27-idle-utilization-metric-definitions §5
--      ADR-2026-05-01-report-base-period-resolution
--
-- Usage: psql -f 01_is_stationed.sql
--
-- Updated on 2026-07-29 (Round 2, Runtime Error Fixes):
--   - CTEs cannot be referenced across statements (WITH clauses are only valid for a single statement).
--     Switched to CREATE TEMP TABLE approach to reuse across multiple SELECT statements.
--   - Confirmed that `me.deleted_at` does not exist (verified 18 columns in information_schema, no match).
--     Currently checking with Miyazawa. Proceeding without the filter until confirmed (with a TODO note).
-- ============================================================================

\set facility_id 1
\set period_start '2026-01-01'
\set period_end '2026-06-30'

DROP TABLE IF EXISTS tmp_params, tmp_base_devices, tmp_committed_intervals,
    tmp_merged_device, tmp_committed_hours, tmp_merged_device_location,
    tmp_location_hours, tmp_dominant_location;

CREATE TEMP TABLE tmp_params AS
SELECT
    :facility_id::int     AS facility_id,
    :'period_start'::date AS period_start,
    :'period_end'::date   AS period_end;

-- 母集団: 施設内の全機器 (pub.medical_equipment 起点、§5.6 のキーで cur と結合)。
-- TODO: add `AND me.deleted_at IS NULL` once the H-2 soft-delete migration
--       (c3e3b8785652, 2026-07-22) is present in this DB.
--       Currently a no-op: medical_equipment.deleted_at is NULL for all rows,
--       and this local DB (WSL, restored from 2026-06-15 S3 dump) predates
--       the migration anyway. (miyazawa 2026-07-29 回答)
--
-- ============================================================================
--
-- 注意: equipment_usage_flags.is_active は母数フィルタとして使わない。
-- is_active の実質は「稼働しているか」であり①②が問うているものと同じため、
-- フィルタに使うと未稼働機器が母数から消えて指標が循環してしまう
-- (miyazawa 2026-07-29 回答、§3 参照)。
--
-- Population: All devices within the facility (starting from pub.medical_equipment, joined with cur using the key from §5.6).
-- TODO: add `AND me.deleted_at IS NULL` once the H-2 soft-delete migration
--       (c3e3b8785652, 2026-07-22) is present in this DB.
--       Currently a no-op: medical_equipment.deleted_at is NULL for all rows,
--       and this local DB (WSL, restored from 2026-06-15 S3 dump) predates
--       the migration anyway. (Miyazawa 2026-07-29 response)
--
-- NOTE: Do not use equipment_usage_flags.is_active as a population filter.
-- The true nature of `is_active` is whether it is operating, which is the same metric
-- that ① and ② are querying. Filtering by it would remove idle devices from the population,
-- causing the metric to become circular (Miyazawa 2026-07-29 response, ref §3).

CREATE TEMP TABLE tmp_base_devices AS
SELECT
    me.equipment_id,
    l.medical_device_ledger_id
FROM pub.medical_equipment me
CROSS JOIN tmp_params p
LEFT JOIN cur.medical_device_ledger l
ON l.medical_facility_id = me.medical_facility_id
AND COALESCE(NULLIF(l.client_device_number, ''), l.medical_facility_seq_id::text)
    = me.facility_equipment_number
WHERE me.medical_facility_id = p.facility_id;

-- 貸出: Date 粒度 -> [start 00:00, return + 1日 00:00)。返却日の扱いは §5.5 未決、
-- miyazawa の暫定方針 (1日=24h) を採用。修理は DateTime のまま使用。
--
-- Rentals: Date granularity -> [start 00:00, return + 1 day 00:00). The handling of return dates is pending in §5.5, 
-- so Miyazawa's provisional policy (1 day = 24h) has been adopted. Repairs are used with DateTime format as-is.

CREATE TEMP TABLE tmp_committed_intervals AS
SELECT medical_device_ledger_id, location, tsrange(clipped_start, clipped_end, '[)') AS interval
FROM (
    SELECT
        r.medical_device_ledger_id,
        r.recipient_department AS location,
        GREATEST(r.calculated_rental_start_date, p.period_start)::timestamp AS clipped_start,
        (LEAST(COALESCE(r.calculated_return_date, p.period_end), p.period_end)
            + INTERVAL '1 day')::timestamp AS clipped_end
    FROM cur.medical_device_rental_history r
    CROSS JOIN tmp_params p
    WHERE r.medical_device_ledger_id IS NOT NULL
    AND r.calculated_rental_start_date <= p.period_end
    AND COALESCE(r.calculated_return_date, p.period_end) >= p.period_start

    UNION ALL

    SELECT
        rep.medical_device_ledger_id,
        rep.user_department AS location,
        GREATEST(rep.calculated_trouble_date, p.period_start::timestamp) AS clipped_start,
        LEAST(
            COALESCE(rep.calculated_completion_date, p.period_end::timestamp + INTERVAL '1 day'),
            p.period_end::timestamp + INTERVAL '1 day'
        ) AS clipped_end
    FROM cur.medical_device_repair_history rep
    CROSS JOIN tmp_params p
    WHERE rep.medical_device_ledger_id IS NOT NULL
    AND rep.calculated_trouble_date <= p.period_end::timestamp + INTERVAL '1 day'
    AND COALESCE(rep.calculated_completion_date, p.period_end::timestamp) >= p.period_start::timestamp
) all_activity
WHERE clipped_start < clipped_end;

-- 拘束時間の "和" (union of intervals)。単純SUMだと重複区間で100%超になるため
-- range_agg (PG14+) でマージしてから合計する。
--
-- "Union" of intervals for tied-up time (union of intervals). A simple SUM would exceed 100% due to overlapping intervals, 
-- so merge them using range_agg (PG14+) before calculating the total duration.

CREATE TEMP TABLE tmp_merged_device AS
SELECT medical_device_ledger_id, unnest(range_agg(interval)) AS merged_interval
FROM tmp_committed_intervals
GROUP BY medical_device_ledger_id;

CREATE TEMP TABLE tmp_committed_hours AS
SELECT
    medical_device_ledger_id,
    SUM(EXTRACT(EPOCH FROM (upper(merged_interval) - lower(merged_interval))) / 3600.0) AS committed_hours
FROM tmp_merged_device
GROUP BY medical_device_ledger_id;

-- 拠点集中度も location 単位で range_agg してからマージ後の値で計算する
-- (修正理由: 同一 location 内で貸出・修理が重なると raw SUM が committed_hours を
-- 上回り location_concentration_pct が 100% を超えうる。is_stationed を
-- 実態より通りやすくする方向の誤差になるため簡略化を撤回し修正した)。
--
-- The hub concentration rate is also calculated using merged values after performing range_agg per location.
-- (Reason for fix: If rentals and repairs overlap within the same location, a raw SUM could exceed 
-- committed_hours, causing location_concentration_pct to go over 100%. Since this error would bias 
-- is_stationed to be met more easily than actual conditions, the simplification was discarded and corrected).

CREATE TEMP TABLE tmp_merged_device_location AS
SELECT medical_device_ledger_id, location, unnest(range_agg(interval)) AS merged_interval
FROM tmp_committed_intervals
WHERE location IS NOT NULL
GROUP BY medical_device_ledger_id, location;

CREATE TEMP TABLE tmp_location_hours AS
SELECT
    medical_device_ledger_id,
    location,
    SUM(EXTRACT(EPOCH FROM (upper(merged_interval) - lower(merged_interval))) / 3600.0) AS location_hours
FROM tmp_merged_device_location
GROUP BY medical_device_ledger_id, location;

CREATE TEMP TABLE tmp_dominant_location AS
SELECT DISTINCT ON (medical_device_ledger_id)
    medical_device_ledger_id,
    location AS dominant_location,
    location_hours AS dominant_location_hours
FROM tmp_location_hours
ORDER BY medical_device_ledger_id, location_hours DESC;

-- ============================================================================
-- ---- メイン結果: 機器ごとの is_stationed 判定 ----
-- 実績が無い機器も row を出す (is_stationed=false, committed_hours=0)。
-- ============================================================================
-- ---- Main Results: is_stationed determination per device ----
-- Include rows even for devices with no activity records (is_stationed=false, committed_hours=0).
-- ============================================================================

SELECT
    b.equipment_id,
    b.medical_device_ledger_id,
    COALESCE(c.committed_hours, 0) AS committed_hours,
    EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0 AS available_hours,
    ROUND((COALESCE(c.committed_hours, 0)
        / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0)
        * 100)::numeric, 2) AS occupancy_rate_pct,
    d.dominant_location,
    ROUND((d.dominant_location_hours / NULLIF(c.committed_hours, 0) * 100)::numeric, 2) AS location_concentration_pct,
    (
        COALESCE(c.committed_hours, 0)
            / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0) >= 0.95
        AND COALESCE(d.dominant_location_hours, 0) / NULLIF(c.committed_hours, 0) >= 0.80
    ) AS is_stationed
FROM tmp_base_devices b
CROSS JOIN tmp_params p
LEFT JOIN tmp_committed_hours c ON c.medical_device_ledger_id = b.medical_device_ledger_id
LEFT JOIN tmp_dominant_location d ON d.medical_device_ledger_id = b.medical_device_ledger_id
ORDER BY b.equipment_id;

-- ---- ADR §5.1 分布 (1): locations-per-device ----
-- ---- ADR §5.1 Distribution (1): locations-per-device ----
SELECT
    location_count,
    COUNT(*) AS device_count
FROM (
    SELECT medical_device_ledger_id, COUNT(DISTINCT location) AS location_count
    FROM tmp_location_hours
    GROUP BY medical_device_ledger_id
) sub
GROUP BY location_count
ORDER BY location_count;

-- ---- ADR §5.1 分布 (2): dominant-location share ヒストグラム ----
-- ---- ADR §5.1 Distribution (2): dominant-location share histogram ----
SELECT
    CASE
        WHEN pct < 50 THEN '00-50%'
        WHEN pct < 80 THEN '50-80%'
        WHEN pct < 95 THEN '80-95%'
        WHEN pct <= 100 THEN '95-100%'
        ELSE '100%+ (要確認)'
    END AS bucket,
    COUNT(*) AS device_count
FROM (
    SELECT (d.dominant_location_hours / NULLIF(c.committed_hours, 0) * 100) AS pct
    FROM tmp_dominant_location d
    JOIN tmp_committed_hours c ON c.medical_device_ledger_id = d.medical_device_ledger_id
) sub
GROUP BY 1
ORDER BY 1;

-- ---- 100% 超えの有無を明示的にチェック ----
-- ---- Explicitly check for the presence of values exceeding 100% ----
SELECT COUNT(*) AS location_concentration_over_100pct
FROM tmp_dominant_location d
JOIN tmp_committed_hours c ON c.medical_device_ledger_id = d.medical_device_ledger_id
WHERE (d.dominant_location_hours / NULLIF(c.committed_hours, 0) * 100) > 100;

-- ---- headline: is_stationed = true count ----
SELECT is_stationed, COUNT(*)
FROM (
    SELECT
        b.equipment_id,
        b.medical_device_ledger_id,
        COALESCE(c.committed_hours, 0) AS committed_hours,
        EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0 AS available_hours,
        d.dominant_location,
        (
            COALESCE(c.committed_hours, 0)
                / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0) >= 0.95
            AND COALESCE(d.dominant_location_hours, 0) / NULLIF(c.committed_hours, 0) >= 0.80
        ) AS is_stationed
    FROM tmp_base_devices b
    CROSS JOIN tmp_params p
    LEFT JOIN tmp_committed_hours c ON c.medical_device_ledger_id = b.medical_device_ledger_id
    LEFT JOIN tmp_dominant_location d ON d.medical_device_ledger_id = b.medical_device_ledger_id
) x
GROUP BY is_stationed;


-- ============================================================================
-- ---- 追加調査 (Miyazawa 2026-07-31 レビュー #2, #4 対応) ----
-- ---- Follow-up investigation (per Miyazawa's 2026-07-31 review items #2, #4) ----
-- ============================================================================

-- ---- (#2-a) 返却日が NULL の貸出件数 ----
-- ---- (#2-a) Count of rentals with NULL return date ----
SELECT
    COUNT(*) AS total_rentals,
    COUNT(*) FILTER (WHERE calculated_return_date IS NULL) AS open_rentals,
    ROUND(
        COUNT(*) FILTER (WHERE calculated_return_date IS NULL)::numeric
            / NULLIF(COUNT(*), 0) * 100,
        2
    ) AS open_rentals_pct
FROM cur.medical_device_rental_history;

-- 対象期間に絞った版 (①の母集団に効いてくる分だけを見るため)
-- Period-scoped version (isolates only the rows that actually feed into ①'s population)
SELECT
    COUNT(*) AS total_rentals_in_period,
    COUNT(*) FILTER (WHERE r.calculated_return_date IS NULL) AS open_rentals_in_period,
    ROUND(
        COUNT(*) FILTER (WHERE r.calculated_return_date IS NULL)::numeric
            / NULLIF(COUNT(*), 0) * 100,
        2
    ) AS open_rentals_in_period_pct
FROM cur.medical_device_rental_history r
CROSS JOIN tmp_params p
WHERE r.calculated_rental_start_date <= p.period_end
AND COALESCE(r.calculated_return_date, p.period_end) >= p.period_start;

-- ---- (#2-b) occupancy_rate_pct の分布 (100%付近に山があるか) ----
-- ---- (#2-b) Distribution of occupancy_rate_pct (check for a pileup near 100%) ----
-- tmp_committed_hours / tmp_base_devices を再利用し、01 のメインSELECTと
-- 同じ計算式で occupancy_rate_pct を作ってからバケット化する。
--
-- Reuses tmp_committed_hours / tmp_base_devices and recomputes occupancy_rate_pct
-- with the exact same formula as ①'s main SELECT before bucketing.

CREATE TEMP TABLE tmp_occupancy AS
SELECT
    b.equipment_id,
    b.medical_device_ledger_id,
    COALESCE(c.committed_hours, 0) AS committed_hours,
    EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0 AS available_hours,
    ROUND((COALESCE(c.committed_hours, 0)
        / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0)
        * 100)::numeric, 2) AS occupancy_rate_pct
FROM tmp_base_devices b
CROSS JOIN tmp_params p
LEFT JOIN tmp_committed_hours c ON c.medical_device_ledger_id = b.medical_device_ledger_id;

SELECT
    CASE
        WHEN occupancy_rate_pct = 0   THEN '00 (未稼働 / zero)'
        WHEN occupancy_rate_pct < 25  THEN '01-24%'
        WHEN occupancy_rate_pct < 50  THEN '25-49%'
        WHEN occupancy_rate_pct < 75  THEN '50-74%'
        WHEN occupancy_rate_pct < 95  THEN '75-94%'
        WHEN occupancy_rate_pct < 100 THEN '95-99%'
        WHEN occupancy_rate_pct = 100 THEN '100% ちょうど / exactly 100'
        ELSE '100%+ (要確認)'
    END AS bucket,
    COUNT(*) AS device_count,
    ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100, 2) AS pct_of_total
FROM tmp_occupancy
GROUP BY 1
ORDER BY MIN(occupancy_rate_pct);

-- 100%ちょうどの機器のうち、"return date が NULL の貸出" が寄与しているものの件数
-- (山の正体が open_rentals かどうかを直接つなげて確認する)
--
-- Among devices sitting exactly at 100%, how many have at least one contributing
-- rental with a NULL return date (directly links the pileup to open_rentals)

SELECT
    COUNT(DISTINCT o.medical_device_ledger_id) AS devices_at_100pct,
    COUNT(DISTINCT o.medical_device_ledger_id) FILTER (
        WHERE EXISTS (
            SELECT 1
            FROM cur.medical_device_rental_history r
            CROSS JOIN tmp_params p
            WHERE r.medical_device_ledger_id = o.medical_device_ledger_id
            AND r.calculated_return_date IS NULL
            AND r.calculated_rental_start_date <= p.period_end
        )
    ) AS devices_at_100pct_with_open_rental
FROM tmp_occupancy o
WHERE o.occupancy_rate_pct = 100;

-- ---- (#4) ledger にマッチしなかった機器数 (母集団の取りこぼし確認) ----
-- ---- (#4) Devices that did not match the ledger (checks for dropped rows in the population) ----
SELECT
    COUNT(*) AS total_base_devices,
    COUNT(*) FILTER (WHERE medical_device_ledger_id IS NULL) AS unmatched_to_ledger,
    ROUND(
        COUNT(*) FILTER (WHERE medical_device_ledger_id IS NULL)::numeric
            / NULLIF(COUNT(*), 0) * 100,
        2
    ) AS unmatched_pct
FROM tmp_base_devices;

-- unmatched な機器が is_stationed=false のうちどれだけを占めるか
-- (「常設ではない」と「判定できなかった」を切り分けるための直接証拠)
--
-- How much of is_stationed=false is actually "unmatched to ledger"
-- (direct evidence separating "not stationed" from "could not be judged")

SELECT
    COUNT(*) FILTER (WHERE b.medical_device_ledger_id IS NULL) AS false_due_to_unmatched,
    COUNT(*) FILTER (WHERE b.medical_device_ledger_id IS NOT NULL) AS false_due_to_activity,
    COUNT(*) AS total_false
FROM tmp_base_devices b
LEFT JOIN tmp_dominant_location d ON d.medical_device_ledger_id = b.medical_device_ledger_id
LEFT JOIN tmp_committed_hours c ON c.medical_device_ledger_id = b.medical_device_ledger_id
CROSS JOIN tmp_params p
WHERE NOT (
    COALESCE(c.committed_hours, 0)
        / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0) >= 0.95
    AND COALESCE(d.dominant_location_hours, 0) / NULLIF(c.committed_hours, 0) >= 0.80
);