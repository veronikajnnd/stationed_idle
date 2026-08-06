-- ============================================================================
-- Chart 2/4 (manufacturer table), Chart 3 (department), Chart 5 (category idle rate)
-- Ref: 2026-08-04 miyazawa "8/20 サンプル 構成書" §3, §4
--
-- 使い方: psql -f 03_manufacturer_department_category.sql
--
-- Self-contained: 01/02 の temp table には依存しない (別 psql -f 実行は別セッション)。
-- ============================================================================

\set facility_id 1
\set period_start '2026-01-01'
\set period_end '2026-06-30'
\set as_of_date '2026-06-30'

DROP TABLE IF EXISTS tmp_params, tmp_base_devices, tmp_committed_intervals,
    tmp_merged_device, tmp_committed_hours, tmp_merged_device_location,
    tmp_location_hours, tmp_dominant_location, tmp_device_stationed,
    tmp_rental_department_hours, tmp_dominant_department;

CREATE TEMP TABLE tmp_params AS
SELECT
    :facility_id::int     AS facility_id,
    :'period_start'::date AS period_start,
    :'period_end'::date   AS period_end,
    :'as_of_date'::date   AS as_of_date;

-- 母集団: pub.medical_equipment 起点 (§5.6 のキーで cur と結合)。
-- TODO: `AND me.deleted_at IS NULL` when H-2 migration (c3e3b8785652) lands
-- in this DB. Currently a no-op even where present.
CREATE TEMP TABLE tmp_base_devices AS
SELECT
    me.equipment_id,
    me.manufacturer_name,
    me.classification_id_level1,
    l.medical_device_ledger_id,
    l.operation_start_date
FROM pub.medical_equipment me
CROSS JOIN tmp_params p
LEFT JOIN cur.medical_device_ledger l
ON l.medical_facility_id = me.medical_facility_id
AND COALESCE(NULLIF(l.client_device_number, ''), l.medical_facility_seq_id::text)
    = me.facility_equipment_number
WHERE me.medical_facility_id = p.facility_id;

-- ---- is_stationed の再計算  ----
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

CREATE TEMP TABLE tmp_merged_device_location AS
SELECT medical_device_ledger_id, location, unnest(range_agg(interval)) AS merged_interval
FROM tmp_committed_intervals
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

CREATE TEMP TABLE tmp_device_stationed AS
SELECT
    b.equipment_id,
    b.manufacturer_name,
    b.classification_id_level1,
    b.medical_device_ledger_id,
    b.operation_start_date,
    COALESCE(c.committed_hours, 0) AS committed_hours,
    (
        COALESCE(c.committed_hours, 0)
            / (EXTRACT(EPOCH FROM ((p.period_end + INTERVAL '1 day') - p.period_start)) / 3600.0) >= 0.95
        AND COALESCE(d.dominant_location_hours, 0) / NULLIF(c.committed_hours, 0) >= 0.80
    ) AS is_stationed
FROM tmp_base_devices b
CROSS JOIN tmp_params p
LEFT JOIN tmp_committed_hours c ON c.medical_device_ledger_id = b.medical_device_ledger_id
LEFT JOIN tmp_dominant_location d ON d.medical_device_ledger_id = b.medical_device_ledger_id;

-- ============================================================================
-- Chart 1/2/4: manufacturer summary (device count, stationed count, rate, median age)
-- §3 spec: top-9 named by device count, else 「その他」, NULL manufacturer -> 「（不明）」
-- ============================================================================
CREATE TEMP TABLE tmp_manufacturer_top9 AS
SELECT manufacturer_name
FROM tmp_device_stationed
WHERE manufacturer_name IS NOT NULL
GROUP BY manufacturer_name
HAVING COUNT(*) >= 100
ORDER BY COUNT(*) DESC
LIMIT 9;

SELECT
    CASE
        WHEN ds.manufacturer_name IS NULL THEN '（不明）'
        WHEN ds.manufacturer_name IN (SELECT manufacturer_name FROM tmp_manufacturer_top9) THEN ds.manufacturer_name
        ELSE 'その他'
    END AS manufacturer,
    COUNT(*) AS devices,
    COUNT(*) FILTER (WHERE ds.is_stationed) AS stationed,
    ROUND(100.0 * COUNT(*) FILTER (WHERE ds.is_stationed) / COUNT(*), 1) AS stationed_pct,
    ROUND(
        (PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (p.as_of_date::timestamp - ds.operation_start_date::timestamp)) / 86400.0 / 365.25
        ) FILTER (WHERE ds.operation_start_date IS NOT NULL))::numeric,
        1
    ) AS median_age_years,
    COUNT(*) FILTER (WHERE ds.operation_start_date IS NOT NULL) AS n_with_age
FROM tmp_device_stationed ds
CROSS JOIN tmp_params p
GROUP BY 1
ORDER BY stationed_pct DESC;

-- 全体の常設率 (reference line 用)
SELECT
    COUNT(*) AS total_devices,
    COUNT(*) FILTER (WHERE is_stationed) AS total_stationed,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_stationed) / COUNT(*), 1) AS overall_stationed_pct
FROM tmp_device_stationed;

-- ============================================================================
-- Chart 3: 病棟・部門別 常設台数 (recipient_department, top10 by is_stationed count)
-- §3 spec: recipient_department であって、①で使った location (rental+repair 合算) ではない
-- ============================================================================
CREATE TEMP TABLE tmp_rental_department_hours AS
SELECT
    r.medical_device_ledger_id,
    r.recipient_department,
    SUM(
        LEAST(COALESCE(r.calculated_return_date, p.period_end), p.period_end)
        - GREATEST(r.calculated_rental_start_date, p.period_start)
    ) AS days
FROM cur.medical_device_rental_history r
CROSS JOIN tmp_params p
WHERE r.medical_device_ledger_id IS NOT NULL
AND r.recipient_department IS NOT NULL
AND r.calculated_rental_start_date <= p.period_end
AND COALESCE(r.calculated_return_date, p.period_end) >= p.period_start
GROUP BY r.medical_device_ledger_id, r.recipient_department;

CREATE TEMP TABLE tmp_dominant_department AS
SELECT DISTINCT ON (medical_device_ledger_id)
    medical_device_ledger_id,
    recipient_department
FROM tmp_rental_department_hours
ORDER BY medical_device_ledger_id, days DESC;

WITH dept_stationed_counts AS (
    SELECT
        dd.recipient_department,
        COUNT(*) FILTER (WHERE ds.is_stationed) AS stationed_count
    FROM tmp_dominant_department dd
    JOIN tmp_device_stationed ds ON ds.medical_device_ledger_id = dd.medical_device_ledger_id
    GROUP BY dd.recipient_department
),
top10 AS (
    SELECT recipient_department, stationed_count
    FROM dept_stationed_counts
    ORDER BY stationed_count DESC
    LIMIT 10
)
SELECT recipient_department AS department, stationed_count FROM top10
UNION ALL
SELECT
    'その他（' || (SELECT COUNT(*) FROM dept_stationed_counts) - 10 || ' 部門）',
    (SELECT SUM(stationed_count) FROM dept_stationed_counts) - (SELECT SUM(stationed_count) FROM top10)
ORDER BY stationed_count DESC;

-- ============================================================================
-- Chart 5: 分類別 未稼働率② (classification_id_level1 ベース、report_selection は使わない §4.A)
-- top-14 category_major (100+ devices), 「所管外機器」除外, + 「その他」
-- ============================================================================
-- 対象期間内に貸出 or 修理の実績がある機器
CREATE TEMP TABLE tmp_active_devices_c5 AS
SELECT DISTINCT medical_device_ledger_id
FROM (
    SELECT r.medical_device_ledger_id
    FROM cur.medical_device_rental_history r
    CROSS JOIN tmp_params p
    WHERE r.medical_device_ledger_id IS NOT NULL
    AND r.calculated_rental_start_date <= p.period_end
    AND COALESCE(r.calculated_return_date, p.period_end) >= p.period_start
    UNION
    SELECT rep.medical_device_ledger_id
    FROM cur.medical_device_repair_history rep
    CROSS JOIN tmp_params p
    WHERE rep.medical_device_ledger_id IS NOT NULL
    AND rep.calculated_trouble_date <= p.period_end
    AND COALESCE(rep.calculated_completion_date, p.period_end) >= p.period_start
) combined;

WITH classified AS (
    SELECT
        fec.classification_name,
        ds.medical_device_ledger_id
    FROM tmp_device_stationed ds
    JOIN pub.facility_equipment_classification fec
        ON fec.classification_id = ds.classification_id_level1
        AND fec.classification_level = 1
    CROSS JOIN tmp_params p
    WHERE fec.medical_facility_id = p.facility_id
    AND fec.classification_name <> '所管外機器'
),
top14 AS (
    SELECT classification_name
    FROM classified
    GROUP BY classification_name
    HAVING COUNT(*) >= 100
    ORDER BY COUNT(*) DESC
    LIMIT 14
)
SELECT
    CASE WHEN c.classification_name IN (SELECT classification_name FROM top14)
        THEN c.classification_name ELSE 'その他' END AS category,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE ad.medical_device_ledger_id IS NOT NULL) AS active_count,
    ROUND(
        100.0 * (1 - COUNT(*) FILTER (WHERE ad.medical_device_ledger_id IS NOT NULL)::numeric / COUNT(*)),
        1
    ) AS idle_rate_pct
FROM classified c
LEFT JOIN tmp_active_devices_c5 ad ON ad.medical_device_ledger_id = c.medical_device_ledger_id
GROUP BY 1
ORDER BY idle_rate_pct DESC;