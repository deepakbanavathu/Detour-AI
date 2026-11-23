-- Riders correct the map from the road. A person on a live route says what was wrong where they are (gate closed,
-- too narrow, no entry, blocked); matching reports are grouped into corrections an operator applies with one click.
-- Applied width and access corrections are kept by OSM way and segment, so a re-import puts them back.

ALTER TABLE net.edge_dim DROP CONSTRAINT IF EXISTS edge_dim_width_src_check;
ALTER TABLE net.edge_dim ADD CONSTRAINT edge_dim_width_src_check
  CHECK (width_src IN ('OSM_TAG','LANES_INFER','CLASS_DEFAULT','SURVEY','TELEMETRY','FEEDBACK'));

CREATE TABLE IF NOT EXISTS client.route_feedback (
  fb_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sub_id     BIGINT REFERENCES client.subscriber ON DELETE SET NULL,
  kind       TEXT NOT NULL,                              -- allowed values are set in migration 018, so re-applying this file never narrows them
  profile    TEXT NOT NULL,                              -- the vehicle the person was on: 2W or 4W_HATCH
  geom       GEOMETRY(Point, 4326) NOT NULL,
  edge_id    BIGINT,                                     -- the road stretch it was matched to (ids change on re-import)
  osm_way_id BIGINT,
  seg        INT,
  status     TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','APPLIED','DISMISSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at  TIMESTAMPTZ,
  closed_by  TEXT
);
CREATE INDEX IF NOT EXISTS route_feedback_open ON client.route_feedback (osm_way_id, seg, kind) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS route_feedback_sub_day ON client.route_feedback (sub_id, created_at);

-- what an operator accepted, keyed by the OSM way and segment so it outlives edge ids
CREATE TABLE IF NOT EXISTS net.correction (
  osm_way_id BIGINT NOT NULL,
  seg        INT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('TOO_NARROW','NO_ENTRY')),
  width_m    REAL,                                       -- TOO_NARROW: the road is at most this wide
  deny_mask  SMALLINT NOT NULL DEFAULT 0,                -- NO_ENTRY: profile bits that may not enter
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_by TEXT,
  PRIMARY KEY (osm_way_id, seg, kind)
);

-- Put every accepted correction onto the current edges. Called after an operator applies one, and at the end of
-- build_network.sql so a re-import keeps them. Returns the number of edges changed.
CREATE OR REPLACE FUNCTION net.apply_corrections() RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE net.edge_dim d SET width_m = LEAST(d.width_m, k.width_m), width_src = 'FEEDBACK'
  FROM net.edge e JOIN net.correction k ON k.osm_way_id = e.osm_way_id AND k.seg = e.seg AND k.kind = 'TOO_NARROW'
  WHERE d.edge_id = e.edge_id AND (d.width_m > k.width_m OR d.width_src <> 'FEEDBACK');
  UPDATE net.edge_dim d SET access_mask = d.access_mask & ~k.deny_mask
  FROM net.edge e JOIN net.correction k ON k.osm_way_id = e.osm_way_id AND k.seg = e.seg AND k.kind = 'NO_ENTRY'
  WHERE d.edge_id = e.edge_id AND (d.access_mask & k.deny_mask) <> 0;
  WITH ch AS (
    UPDATE net.edge_dim d SET static_mask = net.static_mask(e.oneway, d.width_m, d.clearance_m, d.max_gvw_t, d.layout_private) & d.access_mask
    FROM net.edge e JOIN net.correction k ON k.osm_way_id = e.osm_way_id AND k.seg = e.seg
    WHERE d.edge_id = e.edge_id
      AND d.static_mask <> (net.static_mask(e.oneway, d.width_m, d.clearance_m, d.max_gvw_t, d.layout_private) & d.access_mask)
    RETURNING 1)
  SELECT count(*) INTO n FROM ch;
  RETURN n;
END $$;

-- corrections whose edge does not show them (0 after apply_corrections; the verifier checks this after a rebuild)
CREATE OR REPLACE FUNCTION net.corrections_unapplied() RETURNS INT LANGUAGE sql STABLE AS $$
  SELECT count(*)::int FROM net.correction k JOIN net.edge e ON e.osm_way_id = k.osm_way_id AND e.seg = k.seg JOIN net.edge_dim d USING (edge_id)
  WHERE (k.kind = 'TOO_NARROW' AND d.width_m > k.width_m) OR (k.kind = 'NO_ENTRY' AND (d.access_mask & k.deny_mask) <> 0)
$$;
