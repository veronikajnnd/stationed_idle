WITH manual AS (
    SELECT
        client_device_number,
        medical_facility_id,
        recipient_department,
        date_trunc('month', calculated_rental_start_date)::date AS month_start,
        COUNT(*) AS manual_count
    FROM cur.medical_device_rental_history
    WHERE
        client_device_number IN ('CV181', 'IP775', 'FT016', 'IP773', 'IP530', 'SP1122')
        AND calculated_rental_start_date IS NOT NULL
    GROUP BY 1, 2, 3, 4
),
fact AS (
    SELECT
        client_device_number,
        medical_facility_id,
        recipient_department,
        month_start,
        rental_count
    FROM cur.monthly_rental_count
    WHERE client_device_number IN ('CV181', 'IP775', 'FT016', 'IP773', 'IP530', 'SP1122')
)
SELECT
    COALESCE(m.client_device_number, f.client_device_number) AS client_device_number,
    COALESCE(m.medical_facility_id, f.medical_facility_id) AS medical_facility_id,
    COALESCE(m.recipient_department, f.recipient_department) AS recipient_department,
    COALESCE(m.month_start, f.month_start) AS month_start,
    m.manual_count,
    f.rental_count AS fact_count
FROM manual m
FULL OUTER JOIN fact f
    ON f.client_device_number = m.client_device_number
   AND f.medical_facility_id = m.medical_facility_id
   AND f.recipient_department IS NOT DISTINCT FROM m.recipient_department
   AND f.month_start = m.month_start
WHERE m.manual_count IS DISTINCT FROM f.rental_count;