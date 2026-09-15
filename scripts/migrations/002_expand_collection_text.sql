-- Archipelago collection titles and item labels are not bounded to 512
-- characters. This widening change removes no existing data.

ALTER TABLE `collection`
    MODIFY COLUMN `title` TEXT NULL;

ALTER TABLE `collection_item`
    MODIFY COLUMN `label` TEXT NULL;
