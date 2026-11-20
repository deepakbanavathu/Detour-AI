-- Crowd reports with confirmation and trust (what makes Waze's reports exact), and what the CIFS feed needs.

-- the line or point a zone was drawn around; the CIFS feed publishes this, not the buffer
ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS spine GEOMETRY(Geometry, 4326);
ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS confirm_yes_w REAL NOT NULL DEFAULT 0;   -- summed trust of "still blocked"
ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS confirm_no_w  REAL NOT NULL DEFAULT 0;   -- summed trust of "clear now"
ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'ADVISORY'
  CHECK (origin IN ('ADVISORY','CROWD','FEED'));

ALTER TABLE client.subscriber ADD COLUMN IF NOT EXISTS trust REAL NOT NULL DEFAULT 0.5 CHECK (trust BETWEEN 0 AND 1);

ALTER TABLE ingest.raw_message DROP CONSTRAINT IF EXISTS raw_message_source_type_check;
ALTER TABLE ingest.raw_message ADD CONSTRAINT raw_message_source_type_check
  CHECK (source_type IN ('X','PDF_OCR','RSS','TELEGRAM','FIELD_OFFICER','PASTE','TOMTOM','CROWD'));

CREATE TABLE IF NOT EXISTS client.report (
  report_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sub_id     BIGINT REFERENCES client.subscriber ON DELETE SET NULL,     -- the report outlives a DELETE; the link does not
  kind       TEXT NOT NULL CHECK (kind IN ('WATER','CLOSED','ACCIDENT')),
  geom       GEOMETRY(Point, 4326) NOT NULL,                             -- where the reporter said it is, snapped to the road
  zone_id    BIGINT REFERENCES geo.zone_of_avoidance ON DELETE SET NULL,
  outcome    TEXT NOT NULL DEFAULT 'PENDING' CHECK (outcome IN ('PENDING','CONFIRMED','REFUTED','EXPIRED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS report_pending ON client.report (zone_id) WHERE outcome = 'PENDING';
CREATE INDEX IF NOT EXISTS report_sub_day ON client.report (sub_id, created_at);

-- one "still blocked?" question per person per zone
CREATE TABLE IF NOT EXISTS client.zone_check (
  sub_id      BIGINT NOT NULL REFERENCES client.subscriber ON DELETE CASCADE,
  zone_id     BIGINT NOT NULL REFERENCES geo.zone_of_avoidance ON DELETE CASCADE,
  asked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  answer      BOOLEAN,
  answered_at TIMESTAMPTZ,
  PRIMARY KEY (sub_id, zone_id)
);
