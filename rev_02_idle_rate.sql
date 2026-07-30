-- ============================================================================
-- ② idle_rate (施設 × 分類)
-- Ref: ADR-2026-07-27-idle-utilization-metric-definitions §5 (§5.6, §5.7)
--      ADR-2026-05-01-report-base-period-resolution
--
-- 使い方: psql -f 02_idle_rate.sql
--
-- 2026-07-29 更新 (2周目、実行時エラー対応):
--   - CTE は statement をまたいで参照できない。CREATE TEMP TABLE 方式に変更。
--   - me.deleted_at は実在しないと判明。miyazawa へ確認中、TODO 明記のうえ
--     フィルタ無しで進める (01 と同じ扱い)。
-- ============================================================================
-- ② idle_rate (Facility × Category)
-- Ref: ADR-2026-07-27-idle-utilization-metric-definitions §5 (§5.6, §5.7)
--      ADR-2026-05-01-report-base-period-resolution
--
-- Usage: psql -f 02_idle_rate.sql
--
-- Updated on 2026-07-29 (Round 2, Runtime Error Fixes):
--   - CTEs cannot be referenced across statements. Switched to CREATE TEMP TABLE approach.
--   - Confirmed that `me.deleted_at` does not exist. Currently checking with Miyazawa.
--     Proceeding without the filter for now with a TODO note (same handling as 01).
-- ============================================================================

\set facility_id 1
\set period_start '2026-01-01'
\set period_end '2026-06-30'

DROP TABLE IF EXISTS tmp_params, tmp_facility_classifications, tmp_active_devices,
    tmp_candidate_devices, tmp_eligible_devices, tmp_eligible_resolved;

CREATE TEMP TABLE tmp_params AS
SELECT
    :facility_id::int     AS facility_id,
    :'period_start'::date AS period_start,
    :'period_end'::date   AS period_end;

-- 施設が使う分類 (rank = 優先/表示順であって階層順ではない。§5.2 で検証済み)
-- Categories used by facilities (rank = priority/display order, not hierarchical order. Verified in §5.2)
CREATE TEMP TABLE tmp_facility_classifications AS
SELECT
    s.medical_facility_id,
    s.rank,
    s.classification_id,
    c.classification_level,
    c.parent_classification_id
FROM pub.equipment_classification_report_selection s
JOIN pub.facility_equipment_classification c ON c.classification_id = s.classification_id
CROSS JOIN tmp_params p
WHERE s.medical_facility_id = p.facility_id;

-- 対象期間内に貸出 or 修理の実績がある機器 (= active)
-- Devices with rental or repair records within the target period (= active)
CREATE TEMP TABLE tmp_active_devices AS
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

-- 母数の起点は pub.medical_equipment (§5.6: キー衝突時は cur 側で数えると
-- 二重カウントになるため pub 側から数える)。
-- cur<->pub 結合キー (ADR §5.6, miyazawa 確認済み 2026-07-29):
--   facility_equipment_number = COALESCE(NULLIF(client_device_number,''), medical_facility_seq_id::text)
-- TODO: add `AND me.deleted_at IS NULL` once the H-2 soft-delete migration
--       (c3e3b8785652, 2026-07-22) is present in this DB.
--       Currently a no-op: medical_equipment.deleted_at is NULL for all rows,
--       and this local DB (WSL, restored from 2026-06-15 S3 dump) predates
--       the migration anyway. (miyazawa 2026-07-29 回答)
--
-- 注意: equipment_usage_flags.is_active は母数フィルタとして使わない。
-- is_active の実質は「稼働しているか」であり②が問うているものと同じため、
-- フィルタに使うと未稼働機器が母数から消え idle_rate が構造的にゼロへ寄る
-- (循環)。(miyazawa 2026-07-29 回答、§3 参照)
-- override_is_included=false は除外、override_classification_id は分類上書き (§5.2)。
--
-- ============================================================================ 
--
-- The baseline for the denominator starts from pub.medical_equipment (§5.6: Counting on the 
-- cur side in case of key conflicts would cause double-counting, so we count from the pub side).
-- cur <-> pub join key (ADR §5.6, confirmed with Miyazawa on 2026-07-29):
--   facility_equipment_number = COALESCE(NULLIF(client_device_number,''), medical_facility_seq_id::text)
-- TODO: add `AND me.deleted_at IS NULL` once the H-2 soft-delete migration
--       (c3e3b8785652, 2026-07-22) is present in this DB.
--       Currently a no-op: medical_equipment.deleted_at is NULL for all rows,
--       and this local DB (WSL, restored from 2026-06-15 S3 dump) predates
--       the migration anyway. (Miyazawa 2026-07-29 response)
--
-- NOTE: Do not use equipment_usage_flags.is_active as a denominator filter.
-- The true nature of `is_active` is whether it is operating, which is the same metric
-- that ② is querying. Filtering by it would remove idle devices from the denominator,
-- structurally driving the idle_rate towards zero (circular logic). (Miyazawa 2026-07-29 response, ref §3)
-- Exclude rows with override_is_included=false, and use override_classification_id for category overrides (§5.2).
-- ============================================================================

CREATE TEMP TABLE tmp_candidate_devices AS
SELECT
    me.equipment_id,
    me.medical_facility_id,
    me.classification_id_level1,
    me.classification_id_level2,
    me.classification_id_level3,
    me.purchase_date,
    me.disposal_date,
    l.medical_device_ledger_id,
    s.override_classification_id
FROM pub.medical_equipment me
CROSS JOIN tmp_params p
LEFT JOIN cur.medical_device_ledger l
ON l.medical_facility_id = me.medical_facility_id
AND COALESCE(NULLIF(l.client_device_number, ''), l.medical_facility_seq_id::text)
    = me.facility_equipment_number
LEFT JOIN pub.medical_equipment_analysis_setting s ON s.equipment_id = me.equipment_id
WHERE me.medical_facility_id = p.facility_id
AND COALESCE(s.override_is_included, true) = true;

-- §5.7 母数条件: 「期間中ずっと保有 (held_whole_period)」または
-- 「期間中に活動実績あり (has_activity)」のどちらかを満たす機器のみ母数に含める。
-- NULL の purchase_date/disposal_date は「保有していた」扱い。
--
-- §5.7 Denominator Conditions: Include a device in the denominator only if it meets either 
-- "held_whole_period" or "has_activity" during the target period.
-- NULL values for purchase_date/disposal_date are treated as "held".

CREATE TEMP TABLE tmp_eligible_devices AS
SELECT
    cd.*,
    (
        (cd.purchase_date IS NULL OR cd.purchase_date <= p.period_start)
        AND (cd.disposal_date IS NULL OR cd.disposal_date > p.period_end)
    ) AS held_whole_period,
    (cd.medical_device_ledger_id IN (SELECT medical_device_ledger_id FROM tmp_active_devices)) AS has_activity
FROM tmp_candidate_devices cd
CROSS JOIN tmp_params p;

-- 機器ごとの実効 classification_id (override 優先。override は単一列のため、
-- 対象levelがfacility_classificationsと同じ前提。複数levelにまたがるoverrideが
-- 実データにあれば要相談、フォロー継続中)。
--
-- Effective classification_id per device (override takes priority. Since override is a single column, 
-- it assumes the target level matches facility_classifications. If actual data contains overrides 
-- spanning multiple levels, further discussion is needed—follow-up is ongoing).

CREATE TEMP TABLE tmp_eligible_resolved AS
SELECT
    equipment_id,
    medical_facility_id,
    COALESCE(override_classification_id, classification_id_level1) AS resolved_level1,
    COALESCE(override_classification_id, classification_id_level2) AS resolved_level2,
    COALESCE(override_classification_id, classification_id_level3) AS resolved_level3,
    medical_device_ledger_id
FROM tmp_eligible_devices
WHERE held_whole_period OR has_activity;

-- ---- メイン: 分類ごとの idle_rate ----
-- ---- Main: idle_rate per classification ----
SELECT
    fc.rank,
    fc.classification_id,
    fc.classification_level,
    COUNT(edr.equipment_id) AS equipment_count,
    COUNT(edr.equipment_id) FILTER (WHERE ad.medical_device_ledger_id IS NOT NULL) AS active_equipment_count,
    ROUND(
        (1 - COUNT(edr.equipment_id) FILTER (WHERE ad.medical_device_ledger_id IS NOT NULL)::numeric
            / NULLIF(COUNT(edr.equipment_id), 0)) * 100,
        2
    ) AS idle_rate_pct
FROM tmp_facility_classifications fc
LEFT JOIN tmp_eligible_resolved edr
    ON (fc.classification_level = 1 AND edr.resolved_level1 = fc.classification_id)
    OR (fc.classification_level = 2 AND edr.resolved_level2 = fc.classification_id)
    OR (fc.classification_level = 3 AND edr.resolved_level3 = fc.classification_id)
LEFT JOIN tmp_active_devices ad ON ad.medical_device_ledger_id = edr.medical_device_ledger_id
GROUP BY fc.rank, fc.classification_id, fc.classification_level
ORDER BY fc.rank;

-- ---- §5.7 要求: excluded_count と (b) brought-back count ----
-- ---- §5.7 Requirements: excluded_count and (b) brought-back count ----
SELECT
    (SELECT COUNT(*) FROM tmp_candidate_devices) AS total_candidates,
    (SELECT COUNT(*) FROM tmp_eligible_devices WHERE held_whole_period OR has_activity) AS eligible_count,
    (SELECT COUNT(*) FROM tmp_candidate_devices)
        - (SELECT COUNT(*) FROM tmp_eligible_devices WHERE held_whole_period OR has_activity) AS excluded_count,
    (SELECT COUNT(*) FROM tmp_eligible_devices WHERE NOT held_whole_period AND has_activity) AS brought_back_by_activity_count;

-- ---- §5.6 要求: key-collision check (denominator/numerator の重複カウント検証) ----
-- ---- §5.6 Requirement: key-collision check (verify no duplicate counting in denominator/numerator) ----
SELECT
    (SELECT COUNT(*) FROM pub.medical_equipment WHERE medical_facility_id = 1) AS pub_raw_count,
    (
        SELECT COUNT(*) FROM (
            SELECT equipment_id FROM tmp_candidate_devices
            GROUP BY equipment_id HAVING COUNT(*) > 1
        ) d
    ) AS dup_equipment_count;

-- ---- §5.7 補足: purchase_date / disposal_date の NULL 率 (excluded_count=0 の解釈に必要) ----
-- ---- §5.7 Supplement: NULL rate of purchase_date/disposal_date (needed to interpret excluded_count=0) ----
SELECT
    COUNT(*) AS total,
    COUNT(purchase_date) AS has_purchase_date,
    COUNT(disposal_date) AS has_disposal_date
FROM pub.medical_equipment
WHERE medical_facility_id = 1;

-- ---- §5.2 確認: この施設の分類設定行数 ----
-- ---- §5.2 Check: number of classification rows configured for this facility ----
SELECT COUNT(*) AS classification_row_count
FROM pub.equipment_classification_report_selection
WHERE medical_facility_id = 1;