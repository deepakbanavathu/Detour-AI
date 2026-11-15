-- Module 2: dimensional network. Static geometry -> per-edge profile bitmask; volatile state in net.edge_dynamic.

CREATE TABLE IF NOT EXISTS net.vehicle_profile (
  profile_id         SMALLINT PRIMARY KEY,            -- bit position in every mask
  code               TEXT UNIQUE NOT NULL,
  width_m            REAL NOT NULL,
  height_m           REAL NOT NULL,
  length_m           REAL NOT NULL,
  gvw_t              REAL NOT NULL,
  turn_radius_m      REAL NOT NULL,                   -- kerb-to-kerb
  ground_clearance_m REAL NOT NULL,                   -- wading limit (Module 4)
  can_filter         BOOLEAN NOT NULL,                -- lane-filter past stopped 4W
  can_alley          BOOLEAN NOT NULL,
  max_speed_kmh      REAL NOT NULL
);
INSERT INTO net.vehicle_profile VALUES
 (0,'2W',      0.80,1.30, 2.00, 0.25, 1.2,0.16,true, true, 70),
 (1,'3W',      1.40,1.75, 2.65, 0.70, 2.8,0.18,false,true, 50),
 (2,'4W_HATCH',1.70,1.55, 3.80, 1.50, 4.9,0.17,false,false,90),
 (3,'4W_SUV',  1.90,1.80, 4.60, 2.20, 5.6,0.21,false,false,90),
 (4,'LCV',     2.00,2.30, 5.20, 3.50, 6.5,0.20,false,false,70),
 (5,'HCV',     2.55,3.80,12.00,16.20,11.0,0.28,false,false,60),
 (6,'BUS',     2.55,3.40,12.00,16.00,11.5,0.25,false,false,60)
ON CONFLICT (profile_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS net.node (
  node_id   BIGINT PRIMARY KEY,                       -- OSM node id
  geom      GEOMETRY(POINT, 4326) NOT NULL,
  h3_r11    H3INDEX NOT NULL,
  barrier   TEXT,                                     -- gate | bollard | lift_gate | block | NULL
  signal    BOOLEAN NOT NULL DEFAULT false,
  elev_m REAL, slope_deg REAL, twi REAL, hand_m REAL, flow_acc REAL,   -- filled by Module 4
  is_sink   BOOLEAN NOT NULL DEFAULT false,
  underpass BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS node_geom ON net.node USING GIST (geom);
CREATE INDEX IF NOT EXISTS node_h3   ON net.node (h3_r11);

CREATE TABLE IF NOT EXISTS net.edge (
  edge_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id      SMALLINT NOT NULL DEFAULT 1,
  osm_way_id   BIGINT NOT NULL,
  seg          SMALLINT NOT NULL,                     -- segment index within the OSM way
  src          BIGINT NOT NULL REFERENCES net.node,
  dst          BIGINT NOT NULL REFERENCES net.node,
  geom         GEOMETRY(LINESTRING, 4326) NOT NULL,   -- digitised src -> dst
  length_m     REAL NOT NULL,
  bearing_deg  REAL NOT NULL,                         -- azimuth src -> dst, degrees clockwise from north
  highway      TEXT NOT NULL,
  highway_rank SMALLINT NOT NULL,                     -- trunk 1 .. living_street 7, service 8
  road_gaz_id  BIGINT REFERENCES geo.gazetteer,
  name         TEXT,
  oneway       BOOLEAN NOT NULL,
  lanes        SMALLINT,
  freeflow_kmh REAL NOT NULL,
  h3_r9        H3INDEX[] NOT NULL                     -- cells crossed, from ST_Segmentize at 30 m
);
CREATE INDEX IF NOT EXISTS edge_geom ON net.edge USING GIST (geom);
CREATE INDEX IF NOT EXISTS edge_h3_gin ON net.edge USING GIN (h3_r9);   -- named apart from the net.edge_h3 table
CREATE INDEX IF NOT EXISTS edge_src  ON net.edge (src);
CREATE INDEX IF NOT EXISTS edge_dst  ON net.edge (dst);
CREATE INDEX IF NOT EXISTS edge_way  ON net.edge (osm_way_id);
CREATE INDEX IF NOT EXISTS edge_name_trgm ON net.edge USING GIN (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS net.edge_h3 (                -- cell-to-edge with length share (Modules 3, 5)
  h3_r9   H3INDEX NOT NULL,
  edge_id BIGINT  NOT NULL REFERENCES net.edge ON DELETE CASCADE,
  frac    REAL    NOT NULL,
  PRIMARY KEY (h3_r9, edge_id)
);

CREATE TABLE IF NOT EXISTS net.edge_dim (
  edge_id        BIGINT PRIMARY KEY REFERENCES net.edge ON DELETE CASCADE,
  width_m        REAL NOT NULL,
  width_src      TEXT NOT NULL CHECK (width_src IN ('OSM_TAG','LANES_INFER','CLASS_DEFAULT','SURVEY','TELEMETRY')),
  clearance_m    REAL,                                -- maxheight; NULL = unrestricted
  max_gvw_t      REAL,
  surface_class  SMALLINT NOT NULL,                   -- 0 asphalt, 1 concrete, 2 paving, 3 unpaved
  median         BOOLEAN NOT NULL,
  filterable     BOOLEAN NOT NULL,                    -- lanes >= 2 AND width >= 6.5 AND motorcycle <> 'no'
  alley          BOOLEAN NOT NULL,                    -- width < 2.5
  layout_private BOOLEAN NOT NULL,
  speed_breakers SMALLINT NOT NULL DEFAULT 0,
  static_mask    SMALLINT NOT NULL                    -- bit i set = profile i passes static geometry
);

CREATE TABLE IF NOT EXISTS net.turn (
  in_edge         BIGINT NOT NULL REFERENCES net.edge ON DELETE CASCADE,
  via_node        BIGINT NOT NULL REFERENCES net.node ON DELETE CASCADE,
  out_edge        BIGINT NOT NULL REFERENCES net.edge ON DELETE CASCADE,
  deflection_deg  REAL NOT NULL,                      -- 0 straight, 90 right angle, 180 U-turn
  avail_radius_m  REAL NOT NULL,
  allowed_mask    SMALLINT NOT NULL,
  osm_restriction TEXT,
  PRIMARY KEY (in_edge, via_node, out_edge)
);

CREATE TABLE IF NOT EXISTS net.edge_dynamic (           -- volatile; one row per edge, overwritten in place
  edge_id          BIGINT PRIMARY KEY REFERENCES net.edge ON DELETE CASCADE,
  gate_state       TEXT NOT NULL DEFAULT 'OPEN'
                   CHECK (gate_state IN ('OPEN','CLOSED_REPORTED','CLOSED_INFERRED','UNKNOWN')),
  gate_volatility  REAL NOT NULL DEFAULT 0,
  p_open_now       REAL NOT NULL DEFAULT 1,
  two_wheeler_gap  BOOLEAN NOT NULL DEFAULT false,
  zone_penalty_fwd REAL NOT NULL DEFAULT 0,
  zone_penalty_rev REAL NOT NULL DEFAULT 0,
  zone_mask        SMALLINT NOT NULL DEFAULT 0,
  p_submerge       REAL NOT NULL DEFAULT 0,
  depth_est_m      REAL NOT NULL DEFAULT 0,
  speed_now_kmh    REAL,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
) WITH (fillfactor = 70);

-- What pgRouting consumes (Module 1 corridor path; also the v0 route API).
CREATE OR REPLACE VIEW net.edge_pgr AS
SELECT edge_id AS id, src AS source, dst AS target, length_m AS cost,
       CASE WHEN oneway THEN -1 ELSE length_m END AS reverse_cost,
       city_id, highway_rank, road_gaz_id
FROM net.edge;

-- '7', '7 m', '23 ft' -> metres
CREATE OR REPLACE FUNCTION net.parse_width(t TEXT) RETURNS REAL LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN t IS NULL THEN NULL
    -- regexp_match returns capture groups, so the whole number must be the first group
    WHEN t ~ '^\s*\d+(\.\d+)?\s*(m)?\s*$' THEN (regexp_match(t, '(\d+(?:\.\d+)?)'))[1]::real
    WHEN t ~ 'ft'                         THEN (regexp_match(t, '(\d+(?:\.\d+)?)'))[1]::real * 0.3048
    ELSE NULL END
$$;

-- Static exclusion matrix: a profile passes when it fits beside oncoming traffic (two-way) or alone (one-way).
CREATE OR REPLACE FUNCTION net.static_mask(oneway BOOLEAN, width REAL, clearance REAL, gvw REAL, priv BOOLEAN)
RETURNS SMALLINT LANGUAGE sql STABLE AS $$
  SELECT COALESCE(bit_or(1 << p.profile_id), 0)::smallint
  FROM net.vehicle_profile p
  WHERE width >= p.width_m + CASE WHEN oneway THEN 0.4 ELSE 1.8 + 0.4 END   -- 1.8 m = oncoming hatchback
    AND (clearance IS NULL OR clearance >= p.height_m + 0.15)
    AND (gvw IS NULL OR gvw >= p.gvw_t)
    AND NOT (priv AND p.profile_id >= 4)                                      -- LCV/HCV/BUS never enter gated layouts
$$;
