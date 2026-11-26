INSERT INTO identifier_type (shortcode, description, url_construct)
SELECT 'dspace_handle', 'DSpace Handle', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM identifier_type WHERE shortcode = 'dspace_handle'
);
