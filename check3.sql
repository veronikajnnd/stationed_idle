DROP FUNCTION IF EXISTS tmp_kana_to_romaji(text);
DROP TABLE IF EXISTS tmp_manufacturer_normalized, tmp_katakana_romaji_map, tmp_manufacturer_romaji;

CREATE TEMP TABLE tmp_manufacturer_normalized AS
SELECT
    manufacturer_name AS original_name,
    COUNT(*) AS device_count,
    UPPER(TRIM(REGEXP_REPLACE(
        TRANSLATE(
            manufacturer_name,
            'ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ',
            'ヲァィゥェォャュョッアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン'
        ),
        '(株式会社|有限会社|㈱|Co\.,?\s*Ltd\.?|Corp\.?|Corporation|Inc\.?|K\.K\.?)',
        '', 'gi'
    ))) AS normalized_name
FROM cur.medical_device_ledger
WHERE manufacturer_name IS NOT NULL
GROUP BY manufacturer_name;

CREATE TEMP TABLE tmp_katakana_romaji_map AS
SELECT * FROM (VALUES
    ('ｷｬ','kya'),('ｷｭ','kyu'),('ｷｮ','kyo'),('ｷﾞｬ','gya'),('ｷﾞｭ','gyu'),('ｷﾞｮ','gyo'),
    ('ｼｬ','sha'),('ｼｭ','shu'),('ｼｮ','sho'),('ｼﾞｬ','ja'),('ｼﾞｭ','ju'),('ｼﾞｮ','jo'),
    ('ﾁｬ','cha'),('ﾁｭ','chu'),('ﾁｮ','cho'),('ﾆｬ','nya'),('ﾆｭ','nyu'),('ﾆｮ','nyo'),
    ('ﾋｬ','hya'),('ﾋｭ','hyu'),('ﾋｮ','hyo'),('ﾋﾞｬ','bya'),('ﾋﾞｭ','byu'),('ﾋﾞｮ','byo'),
    ('ﾋﾟｬ','pya'),('ﾋﾟｭ','pyu'),('ﾋﾟｮ','pyo'),('ﾐｬ','mya'),('ﾐｭ','myu'),('ﾐｮ','myo'),
    ('ﾘｬ','rya'),('ﾘｭ','ryu'),('ﾘｮ','ryo'),
    ('ｶﾞ','ga'),('ｷﾞ','gi'),('ｸﾞ','gu'),('ｹﾞ','ge'),('ｺﾞ','go'),
    ('ｻﾞ','za'),('ｼﾞ','ji'),('ｽﾞ','zu'),('ｾﾞ','ze'),('ｿﾞ','zo'),
    ('ﾀﾞ','da'),('ﾁﾞ','ji'),('ﾂﾞ','zu'),('ﾃﾞ','de'),('ﾄﾞ','do'),
    ('ﾊﾞ','ba'),('ﾋﾞ','bi'),('ﾌﾞ','bu'),('ﾍﾞ','be'),('ﾎﾞ','bo'),
    ('ﾊﾟ','pa'),('ﾋﾟ','pi'),('ﾌﾟ','pu'),('ﾍﾟ','pe'),('ﾎﾟ','po'),
    ('ｱ','a'),('ｲ','i'),('ｳ','u'),('ｴ','e'),('ｵ','o'),
    ('ｶ','ka'),('ｷ','ki'),('ｸ','ku'),('ｹ','ke'),('ｺ','ko'),
    ('ｻ','sa'),('ｼ','shi'),('ｽ','su'),('ｾ','se'),('ｿ','so'),
    ('ﾀ','ta'),('ﾁ','chi'),('ﾂ','tsu'),('ﾃ','te'),('ﾄ','to'),
    ('ﾅ','na'),('ﾆ','ni'),('ﾇ','nu'),('ﾈ','ne'),('ﾉ','no'),
    ('ﾊ','ha'),('ﾋ','hi'),('ﾌ','fu'),('ﾍ','he'),('ﾎ','ho'),
    ('ﾏ','ma'),('ﾐ','mi'),('ﾑ','mu'),('ﾒ','me'),('ﾓ','mo'),
    ('ﾔ','ya'),('ﾕ','yu'),('ﾖ','yo'),
    ('ﾗ','ra'),('ﾘ','ri'),('ﾙ','ru'),('ﾚ','re'),('ﾛ','ro'),
    ('ﾜ','wa'),('ｦ','wo'),('ﾝ','n'),
    ('ｧ',''),('ｨ',''),('ｩ',''),('ｪ',''),('ｫ',''),('ｯ',''),
    ('ｰ','')
) AS t(kana, romaji);

CREATE OR REPLACE FUNCTION tmp_kana_to_romaji(input text) RETURNS text AS $$
DECLARE
    result text := input;
    r RECORD;
BEGIN
    FOR r IN SELECT kana, romaji FROM tmp_katakana_romaji_map ORDER BY LENGTH(kana) DESC LOOP
        result := REPLACE(result, r.kana, r.romaji);
    END LOOP;
    RETURN result;
END;
$$ LANGUAGE plpgsql;

-- Method A: substring containment
SELECT
    a.original_name AS shorter_name,
    a.device_count AS shorter_device_count,
    b.original_name AS longer_name,
    b.device_count AS longer_device_count
FROM tmp_manufacturer_normalized a
JOIN tmp_manufacturer_normalized b
    ON a.normalized_name <> b.normalized_name
    AND b.normalized_name LIKE '%' || a.normalized_name || '%'
    AND LENGTH(a.normalized_name) >= 3
ORDER BY LENGTH(a.normalized_name) DESC, b.device_count DESC;

-- Method B: sorted by phonetic key so similar-sounding rows land near each other.
-- Main output: all 179 manufacturers, Latin as-is / kana+kanji romanized,
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