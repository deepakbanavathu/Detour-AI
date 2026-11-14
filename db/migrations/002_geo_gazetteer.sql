-- Gazetteer (Module 1.5). Created before net.* because net.edge.road_gaz_id references it.
CREATE TABLE IF NOT EXISTS geo.gazetteer (
  gaz_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id       SMALLINT NOT NULL DEFAULT 1,
  canonical     TEXT NOT NULL,
  feature_class TEXT NOT NULL CHECK (feature_class IN
    ('JUNCTION','ROAD','AREA','LANDMARK','UNDERPASS','FLYOVER','GATE','BRIDGE','SIGNAL')),
  geom          GEOMETRY(GEOMETRY, 4326) NOT NULL,     -- POINT, LINESTRING or MULTIPOLYGON by class
  h3_r8         H3INDEX NOT NULL,
  osm_type      CHAR(1),
  osm_id        BIGINT,
  popularity    REAL NOT NULL DEFAULT 0,               -- ln(1 + mentions in trailing 365 d)
  beat_zone     TEXT                                   -- traffic police division, e.g. 'HYD-E'
);
CREATE INDEX IF NOT EXISTS gaz_geom ON geo.gazetteer USING GIST (geom);
CREATE INDEX IF NOT EXISTS gaz_h3   ON geo.gazetteer (h3_r8);

CREATE TABLE IF NOT EXISTS geo.gazetteer_alias (
  alias  TEXT   NOT NULL,                              -- normalized: lowercase, transliterated, expanded
  gaz_id BIGINT NOT NULL REFERENCES geo.gazetteer ON DELETE CASCADE,
  weight REAL   NOT NULL DEFAULT 1,                    -- curated 1.0, OSM alt_name 0.8, mined 0.5
  PRIMARY KEY (alias, gaz_id)
);
CREATE INDEX IF NOT EXISTS gaz_alias_trgm ON geo.gazetteer_alias USING GIN (alias gin_trgm_ops);

-- Gated layouts: polygons of private layouts; hand-mapped, kept separate from OSM-derived tables (ODbL).
CREATE TABLE IF NOT EXISTS geo.gated_layout (
  layout_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id   SMALLINT NOT NULL DEFAULT 1,
  name      TEXT,
  geom      GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
  source    TEXT NOT NULL DEFAULT 'FIELD'              -- FIELD | BHUVAN_TRACE | PARTNER
);
CREATE INDEX IF NOT EXISTS gated_layout_geom ON geo.gated_layout USING GIST (geom);
