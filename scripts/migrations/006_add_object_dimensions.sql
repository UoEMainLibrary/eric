-- Persist IIIF source dimensions so legacy full/full image requests can be
-- capped only when their source image exceeds the browser-safe size.
ALTER TABLE `object`
    ADD COLUMN `width` INT NULL AFTER `primary_id`,
    ADD COLUMN `height` INT NULL AFTER `width`;
