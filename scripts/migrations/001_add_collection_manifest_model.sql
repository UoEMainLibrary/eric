-- ERIC collection/manifest foundation.
--
-- This migration is additive: it creates new tables and seed rows only.  It
-- does not alter existing Object or Identifier data.

CREATE TABLE IF NOT EXISTS `collection` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `object_id` INT NOT NULL,
    `shelfmark` VARCHAR(255) NULL,
    `shelfmark_normalised` VARCHAR(255) NULL,
    `title` VARCHAR(512) NULL,
    `created_at` DATETIME NOT NULL,
    `updated_at` DATETIME NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_collection_object` (`object_id`),
    KEY `ix_collection_shelfmark` (`shelfmark`),
    KEY `ix_collection_shelfmark_normalised` (`shelfmark_normalised`),
    CONSTRAINT `fk_collection_object`
        FOREIGN KEY (`object_id`) REFERENCES `object` (`id`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `collection_source_record` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `collection_id` INT NOT NULL,
    `source_system` VARCHAR(64) NOT NULL,
    `primary_identifier_id` INT NOT NULL,
    `source_url` VARCHAR(2048) NULL,
    `source_metadata_hash` VARCHAR(64) NULL,
    `last_seen_at` DATETIME NULL,
    `created_at` DATETIME NOT NULL,
    `updated_at` DATETIME NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_collection_source_record` (
        `collection_id`, `source_system`, `primary_identifier_id`
    ),
    KEY `ix_collection_source_record_source_system` (`source_system`),
    KEY `ix_collection_source_record_primary_identifier` (`primary_identifier_id`),
    CONSTRAINT `fk_collection_source_record_collection`
        FOREIGN KEY (`collection_id`) REFERENCES `collection` (`id`),
    CONSTRAINT `fk_collection_source_record_primary_identifier`
        FOREIGN KEY (`primary_identifier_id`) REFERENCES `identifier` (`id`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `collection_item` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `collection_id` INT NOT NULL,
    `object_id` INT NOT NULL,
    `sequence` INT NULL,
    `label` VARCHAR(512) NULL,
    `first_seen_at` DATETIME NOT NULL,
    `last_seen_at` DATETIME NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_collection_item` (`collection_id`, `object_id`),
    KEY `ix_collection_item_collection_sequence` (`collection_id`, `sequence`),
    KEY `ix_collection_item_object` (`object_id`),
    CONSTRAINT `fk_collection_item_collection`
        FOREIGN KEY (`collection_id`) REFERENCES `collection` (`id`),
    CONSTRAINT `fk_collection_item_object`
        FOREIGN KEY (`object_id`) REFERENCES `object` (`id`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `sync_state` (
    `id` INT NOT NULL AUTO_INCREMENT,
    `job_name` VARCHAR(128) NOT NULL,
    `last_successful_source_time` DATETIME NULL,
    `last_completed_at` DATETIME NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'pending',
    `details_json` TEXT NULL,
    `created_at` DATETIME NOT NULL,
    `updated_at` DATETIME NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_sync_state_job_name` (`job_name`)
) ENGINE=InnoDB;

-- Seed rows are deliberately idempotent because the existing type tables do
-- not declare these human-readable values unique.
INSERT INTO `object_type` (`name`, `url_construct`)
SELECT 'Digital Object Collection', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM `object_type` WHERE `name` = 'Digital Object Collection'
);

INSERT INTO `identifier_type` (`shortcode`, `description`, `url_construct`)
SELECT
    'arch_nid',
    'Archipelago Drupal node ID',
    'https://digital.collections.ed.ac.uk/node/<id>'
WHERE NOT EXISTS (
    SELECT 1 FROM `identifier_type` WHERE `shortcode` = 'arch_nid'
);
