-- Expose domain-scoped compound-object references as normal ERIC identifiers.
-- Existing display and normalisation fields remain on compound_object.

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'luna_book', 'LUNA book-reader ID', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'luna_book'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'archives_reference', 'Archives reference', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'archives_reference'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'rare_books_shelfmark', 'Rare Books shelfmark', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'rare_books_shelfmark'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'vernon_accession', 'Vernon accession number', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'vernon_accession'
);
