-- Review fixes (v0.11): where each zone came from is recorded correctly, and when it last changed.

ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Zones made before the origin column existed all defaulted to ADVISORY. Zones built from a licensed traffic feed
-- must be marked as such, because they may not be passed on in the Waze-format feed.
UPDATE geo.zone_of_avoidance z SET origin = 'FEED'
FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id AND a.model_ver LIKE 'tomtom%' AND z.origin <> 'FEED';
UPDATE geo.zone_of_avoidance z SET origin = 'CROWD'
FROM ingest.advisory_ir a WHERE a.ir_id = z.ir_id AND a.model_ver = 'crowd-pin' AND z.origin <> 'CROWD';
