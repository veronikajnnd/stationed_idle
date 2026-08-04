SELECT
    m.original_name,
    m.device_count,
    CASE
        WHEN m.original_name ~ '^[A-Za-z0-9&.,\s]+$' THEN 'latin'
        WHEN m.original_name ~ '[\u4e00-\u9fff]' THEN 'kanji_mixed'
        ELSE 'kana'
    END AS script_type,
    CASE
        WHEN m.original_name ~ '^[A-Za-z0-9&.,\s]+$'
            THEN LOWER(REGEXP_REPLACE(m.original_name, '[^A-Za-z]', '', 'g'))
        ELSE LOWER(tmp_kana_to_romaji(m.original_name))
    END AS romaji_key
FROM tmp_manufacturer_normalized m
ORDER BY
    CASE
        WHEN m.original_name ~ '^[A-Za-z0-9&.,\s]+$'
            THEN LOWER(REGEXP_REPLACE(m.original_name, '[^A-Za-z]', '', 'g'))
        ELSE LOWER(tmp_kana_to_romaji(m.original_name))
    END,
    m.device_count DESC;