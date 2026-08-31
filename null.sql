SELECT COUNT(*) AS total_rows,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE delivery_date IS NULL) / COUNT(*), 1) AS delivery_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE purchase_date IS NULL) / COUNT(*), 1) AS purchase_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE disposal_date IS NULL) / COUNT(*), 1) AS disposal_date_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_minor IS NULL) / COUNT(*), 1) AS category_minor_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_middle IS NULL) / COUNT(*), 1) AS category_middle_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_major IS NULL) / COUNT(*), 1) AS category_major_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE classification_id_level1 IS NULL) / COUNT(*), 1) AS category_minor_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE classification_id_level2 IS NULL) / COUNT(*), 1) AS category_middle_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE classification_id_level3 IS NULL) / COUNT(*), 1) AS category_major_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE device_number IS NULL) / COUNT(*), 1) AS device_number_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE client_device_number IS NULL) / COUNT(*), 1) AS client_device_number_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE operation_start_date IS NULL) / COUNT(*), 1) AS operation_start_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE manufacturer_name IS NULL) / COUNT(*), 1) AS manufacturer_name_null_pct
FROM pub.medical_equipment;

SELECT COUNT(*) AS total_rows,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE delivery_date IS NULL) / COUNT(*), 1) AS delivery_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_delivery_date IS NULL) / COUNT(*), 1) AS calculated_delivery_date_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE purchase_date IS NULL) / COUNT(*), 1) AS purchase_date_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_purchase_date IS NULL) / COUNT(*), 1) AS calculated_purchase_date_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE disposal_date IS NULL) / COUNT(*), 1) AS disposal_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_disposal_date IS NULL) / COUNT(*), 1) AS calculated_disposal_date_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_minor IS NULL) / COUNT(*), 1) AS category_minor_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_middle IS NULL) / COUNT(*), 1) AS category_middle_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE category_major IS NULL) / COUNT(*), 1) AS category_major_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE device_number IS NULL) / COUNT(*), 1) AS device_number_null_pct,
    -- ROUND(100.0 * COUNT(*) FILTER (WHERE client_device_number IS NULL) / COUNT(*), 1) AS client_device_number_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE operation_start_date IS NULL) / COUNT(*), 1) AS operation_start_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE manufacturer_name IS NULL) / COUNT(*), 1) AS manufacturer_name_null_pct
FROM cur.medical_device_ledger;

SELECT COUNT(*) AS total_rows,
    ROUND(100.0 * COUNT(*) FILTER (WHERE trouble_date IS NULL) / COUNT(*), 1) AS trouble_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE completion_date IS NULL) / COUNT(*), 1) AS completion_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_completion_date IS NULL) / COUNT(*), 1) AS calc_completion_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE downtime_hours IS NULL) / COUNT(*), 1) AS downtime_hours_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_downtime_hours IS NULL) / COUNT(*), 1) AS calc_downtime_hours_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE failure_reason IS NULL) / COUNT(*), 1) AS failure_reason_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE medical_device_ledger_id IS NULL) / COUNT(*), 1) AS unlinked_to_ledger_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE manufacturer_name IS NULL) / COUNT(*), 1) AS manufacturer_name_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE category_minor IS NULL) / COUNT(*), 1) AS category_minor_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE category_middle IS NULL) / COUNT(*), 1) AS category_middle_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE category_major IS NULL) / COUNT(*), 1) AS category_major_null_pct
FROM cur.medical_device_repair_history;

SELECT COUNT(*) AS total_rows,
    ROUND(100.0 * COUNT(*) FILTER (WHERE rental_start_date IS NULL) / COUNT(*), 1) AS rental_start_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE return_date IS NULL) / COUNT(*), 1) AS return_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE calculated_return_date IS NULL) / COUNT(*), 1) AS calc_return_date_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE recipient_department IS NULL) / COUNT(*), 1) AS recipient_department_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE rental_duration_hours IS NULL) / COUNT(*), 1) AS rental_duration_hours_null_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE is_returned = false) / COUNT(*), 1) AS not_yet_returned_pct
FROM cur.medical_device_rental_history;
