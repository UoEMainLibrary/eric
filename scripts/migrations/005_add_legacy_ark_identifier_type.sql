-- Preserve superseded ARKs as resolver aliases when their canonical spelling
-- is re-keyed with a shoulder.  Values remain unique within each type.
INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'ark_legacy', 'Legacy Archival Resource Key', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'ark_legacy'
);
