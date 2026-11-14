-- Source registry, review queue, and the TOMTOM source type.

ALTER TABLE ingest.raw_message DROP CONSTRAINT IF EXISTS raw_message_source_type_check;
ALTER TABLE ingest.raw_message ADD CONSTRAINT raw_message_source_type_check
  CHECK (source_type IN ('X','PDF_OCR','RSS','TELEGRAM','FIELD_OFFICER','PASTE','TOMTOM'));

CREATE TABLE IF NOT EXISTS ingest.source (
  source_id   INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id     SMALLINT NOT NULL DEFAULT 1,
  kind        TEXT NOT NULL CHECK (kind IN ('RSS','X','TOMTOM_INCIDENTS','OPEN_METEO')),
  name        TEXT NOT NULL,
  handle      TEXT NOT NULL DEFAULT '',            -- feed URL, @handle, or '' for bbox-driven sources
  config      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-kind options: fetch_article, user_id, grid_deg ...
  cred        REAL NOT NULL DEFAULT 0.5,           -- starting credibility (data sources tab)
  interval_s  INT NOT NULL DEFAULT 300,
  enabled     BOOLEAN NOT NULL DEFAULT true,
  cursor      TEXT,                                -- since_id, etag, last guid
  last_polled TIMESTAMPTZ,
  last_ok     TIMESTAMPTZ,
  last_error  TEXT,
  items_total INT NOT NULL DEFAULT 0,
  zones_total INT NOT NULL DEFAULT 0,
  UNIQUE (city_id, kind, handle)
);

-- Sources that need no URL from you. RSS feeds and X handles are added with POST /v1/sources once confirmed.
INSERT INTO ingest.source (kind, name, handle, config, cred, interval_s) VALUES
 ('OPEN_METEO', 'Open-Meteo rain grid', '', '{"grid_deg": 0.05}', 0.6, 3600),
 ('TOMTOM_INCIDENTS', 'TomTom traffic incidents', '', '{"categories": [1, 3, 8, 9, 11]}', 0.8, 1200)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS ingest.review (
  review_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  msg_id     BIGINT NOT NULL REFERENCES ingest.raw_message ON DELETE CASCADE,
  sentence   TEXT NOT NULL,                        -- the normalized sentence that failed
  reason     TEXT NOT NULL,                        -- NO_ANCHOR | UNRESOLVED | AMBIGUOUS
  anchor     TEXT,
  candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
  ir         JSONB,
  status     TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','DISMISSED')),
  resolved_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_open ON ingest.review (created_at DESC) WHERE status = 'OPEN';
