-- Rename the manifest foundation to the source-neutral Compound Object model.
--
-- This migration preserves every existing row, including minted ARKs and
-- memberships.  Run it only while the collection sync jobs are stopped.

-- `identifier.value` was globally unique.  Catalogue-system IDs are only
-- unique in their own system and can equal an Archipelago NID, so scope the
-- uniqueness to identifier type before adding source IDs.
-- SQLAlchemy installations have used both `value` and `ix_identifier_value`
-- as the name of the old index.  Discover it rather than assuming either.
SET @old_identifier_value_unique_index = (
    SELECT `INDEX_NAME`
    FROM `information_schema`.`STATISTICS`
    WHERE `TABLE_SCHEMA` = DATABASE()
      AND `TABLE_NAME` = 'identifier'
      AND `COLUMN_NAME` = 'value'
      AND `NON_UNIQUE` = 0
      AND `INDEX_NAME` <> 'PRIMARY'
    LIMIT 1
);
SET @drop_old_identifier_value_unique = IF(
    @old_identifier_value_unique_index IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE `identifier` DROP INDEX `', @old_identifier_value_unique_index, '`')
);
PREPARE drop_old_identifier_value_unique FROM @drop_old_identifier_value_unique;
EXECUTE drop_old_identifier_value_unique;
DEALLOCATE PREPARE drop_old_identifier_value_unique;

ALTER TABLE `identifier`
    ADD UNIQUE KEY `uq_identifier_type_value` (`type_id`, `value`);

RENAME TABLE
    `collection` TO `compound_object`,
    `collection_source_record` TO `compound_object_source_record`,
    `collection_item` TO `compound_object_item`;

ALTER TABLE `compound_object`
    ADD COLUMN `domain` VARCHAR(32) NULL AFTER `object_id`,
    ADD KEY `ix_compound_object_domain` (`domain`);

-- Rename dependent columns and their indexes/foreign keys for clarity.  The
-- parent table rename above has retained all rows and relationships.
ALTER TABLE `compound_object_source_record`
    DROP FOREIGN KEY `fk_collection_source_record_collection`,
    DROP INDEX `uq_collection_source_record`,
    CHANGE COLUMN `collection_id` `compound_object_id` INT NOT NULL,
    ADD UNIQUE KEY `uq_compound_object_source_record`
        (`compound_object_id`, `source_system`, `primary_identifier_id`),
    ADD CONSTRAINT `fk_compound_object_source_record_compound_object`
        FOREIGN KEY (`compound_object_id`) REFERENCES `compound_object` (`id`);

ALTER TABLE `compound_object_item`
    DROP FOREIGN KEY `fk_collection_item_collection`,
    DROP INDEX `uq_collection_item`,
    DROP INDEX `ix_collection_item_collection_sequence`,
    CHANGE COLUMN `collection_id` `compound_object_id` INT NOT NULL,
    ADD UNIQUE KEY `uq_compound_object_item` (`compound_object_id`, `object_id`),
    ADD KEY `ix_compound_object_item_compound_object_sequence`
        (`compound_object_id`, `sequence`),
    ADD CONSTRAINT `fk_compound_object_item_compound_object`
        FOREIGN KEY (`compound_object_id`) REFERENCES `compound_object` (`id`);

-- The ERIC Object type describes the role of the parent record, independent
-- of Archipelago's current "Digital Object Collection" content type.
UPDATE `object_type`
SET `name` = 'Compound Object'
WHERE `name` = 'Digital Object Collection';

-- These identifiers retain their source-system semantics and their raw values.
-- URL construction can be supplied later when public catalogue URL patterns
-- are agreed.
INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'archives_space', 'ArchivesSpace record ID', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'archives_space'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'alma', 'Alma record ID', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'alma'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT 'vernon', 'Vernon record ID', NULL FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'vernon'
);
