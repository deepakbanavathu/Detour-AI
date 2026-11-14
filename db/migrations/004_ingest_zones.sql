-- Module 1: raw advisories, IR, zones of avoidance.

CREATE TABLE IF NOT EXISTS ingest.raw_message (
  msg_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id       SMALLINT NOT NULL DEFAULT 1,
  source_type   TEXT NOT NULL CHECK (source_type IN ('X','PDF_OCR','RSS','TELEGRAM','FIELD_OFFICER','PASTE')),
  source_handle TEXT NOT NULL,                        -- '@HYDTP'; for X, store the numeric user id in source_uid
  source_uid    TEXT,
  source_cred   REAL NOT NULL DEFAULT 0.5,            -- Beta posterior mean, updated by outcome feedback
  ext_id        TEXT NOT NULL,
  ts_published  TIMESTAMPTZ NOT NULL,
  ts_ingested   TIMESTAMPTZ NOT NULL DEFAULT now(),
  text_raw      TEXT,                                 -- purged after 7 days for X content (platform terms)
  text_norm     TEXT NOT NULL,
  lang          TEXT NOT NULL DEFAULT 'en',
  dedup_hash    BYTEA NOT NULL,
  UNIQUE (source_type, ext_id)
);
CREATE INDEX IF NOT EXISTS raw_message_ts ON ingest.raw_message (city_id, ts_published DESC);

CREATE TABLE IF NOT EXISTS ingest.advisory_ir (
  ir_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  msg_id     BIGINT NOT NULL REFERENCES ingest.raw_message ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN
    ('DIVERSION','CLOSURE','WATERLOGGING','VIP_MOVEMENT','GATE_CLOSED','ACCIDENT','PROCESSION','LIFTED')),
  cause      TEXT NOT NULL,
  ir         JSONB NOT NULL,
  model_ver  TEXT NOT NULL,                           -- 'regex-1', 'qwen2.5-7b-q4', ...
  confidence REAL NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS geo.zone_of_avoidance (
  zone_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id           SMALLINT NOT NULL DEFAULT 1,
  ir_id             BIGINT REFERENCES ingest.advisory_ir,
  event_type        TEXT NOT NULL,
  cause             TEXT NOT NULL,
  geom              GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
  geom_utm          GEOMETRY(MULTIPOLYGON) NOT NULL,     -- SRID per city: 32644 Hyderabad, 32643 Bengaluru
  h3_cover_r9       H3INDEX[] NOT NULL,
  bearing_center    REAL,                              -- NULL = all directions
  bearing_halfwidth REAL,
  profile_mask      SMALLINT NOT NULL DEFAULT 127,
  penalty_factor    REAL NOT NULL CHECK (penalty_factor BETWEEN 0 AND 1),
  confidence        REAL NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('CANDIDATE','ACTIVE','DECAYING','EXPIRED','REFUTED')),
  valid_from        TIMESTAMPTZ NOT NULL,
  valid_to          TIMESTAMPTZ NOT NULL,
  corroborations    INT NOT NULL DEFAULT 1,
  reviewed_by       TEXT,                              -- human review during the first 90 days
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS zoa_geom   ON geo.zone_of_avoidance USING GIST (geom);
CREATE INDEX IF NOT EXISTS zoa_h3     ON geo.zone_of_avoidance USING GIN (h3_cover_r9);
CREATE INDEX IF NOT EXISTS zoa_active ON geo.zone_of_avoidance (city_id, valid_to) WHERE status IN ('ACTIVE','DECAYING');

-- Project a committed zone onto directed edges (blueprint 2.4). Call: SELECT geo.apply_zone(zone_id);
CREATE OR REPLACE FUNCTION geo.apply_zone(p_zone BIGINT) RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  WITH z AS (SELECT * FROM geo.zone_of_avoidance WHERE zone_id = p_zone),
  hit AS (
    SELECT e.edge_id,
           z.bearing_center IS NULL OR abs((( e.bearing_deg        - z.bearing_center + 540)::int % 360) - 180) <= z.bearing_halfwidth AS fwd,
           z.bearing_center IS NULL OR abs((((e.bearing_deg + 180) - z.bearing_center + 540)::int % 360) - 180) <= z.bearing_halfwidth AS rev,
           z.penalty_factor * z.confidence AS pen, z.profile_mask AS mask
    FROM net.edge e, z
    WHERE e.h3_r9 && z.h3_cover_r9 AND ST_Intersects(e.geom, z.geom)),
  upd AS (
    UPDATE net.edge_dynamic d
    SET zone_penalty_fwd = GREATEST(d.zone_penalty_fwd, CASE WHEN h.fwd THEN h.pen ELSE 0 END),
        zone_penalty_rev = GREATEST(d.zone_penalty_rev, CASE WHEN h.rev THEN h.pen ELSE 0 END),
        zone_mask = d.zone_mask | h.mask, updated_at = now()
    FROM hit h WHERE d.edge_id = h.edge_id
    RETURNING 1)
  SELECT count(*) INTO n FROM upd;
  RETURN n;
END $$;

-- Recompute every edge's zone penalty from the currently active zones (run after expiries/refutations).
CREATE OR REPLACE FUNCTION geo.reapply_all_zones() RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT := 0; z RECORD;
BEGIN
  UPDATE net.edge_dynamic SET zone_penalty_fwd = 0, zone_penalty_rev = 0, zone_mask = 0, updated_at = now()
  WHERE zone_penalty_fwd > 0 OR zone_penalty_rev > 0 OR zone_mask > 0;
  FOR z IN SELECT zone_id FROM geo.zone_of_avoidance
           WHERE status IN ('ACTIVE','DECAYING') AND valid_to > now() LOOP
    n := n + geo.apply_zone(z.zone_id);
  END LOOP;
  RETURN n;
END $$;
