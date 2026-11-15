-- Builds geo.gazetteer and geo.gazetteer_alias from the osm2pgsql flex tables, and links net.edge.road_gaz_id.
-- Re-runnable: replaces gazetteer content (aliases cascade). Run after build_network.sql.

\timing on
SET work_mem = '512MB';

UPDATE net.edge SET road_gaz_id = NULL WHERE road_gaz_id IS NOT NULL;
-- DELETE, not TRUNCATE ... CASCADE: net.edge references geo.gazetteer, and a cascading truncate would empty the network.
TRUNCATE geo.gazetteer_alias;
DELETE FROM geo.gazetteer;
ALTER TABLE geo.gazetteer ALTER COLUMN gaz_id RESTART;

-- helper: normalize a name the same way the API normalizer does for ASCII names
CREATE OR REPLACE FUNCTION geo.norm_name(t TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
  SELECT NULLIF(btrim(regexp_replace(regexp_replace(lower(t), '[^a-z0-9/ \-]+', ' ', 'g'), '\s+', ' ', 'g')), '')
$$;

-- 1. ROAD: named highway ways merged by name within the city ---------------------------------------
WITH named AS (
  SELECT geo.norm_name(tags->>'name') AS nm, tags->>'name' AS canonical, geom
  FROM osm.highway WHERE tags ? 'name'
), merged AS (
  SELECT nm, min(canonical) AS canonical, ST_LineMerge(ST_Collect(geom)) AS geom
  FROM named WHERE nm IS NOT NULL GROUP BY nm
)
INSERT INTO geo.gazetteer (city_id, canonical, feature_class, geom, h3_r8, osm_type, popularity)
SELECT 1, canonical, 'ROAD', geom, h3_lat_lng_to_cell(ST_Centroid(geom), 8), 'w', 0
FROM merged;

-- link edges to their road feature
UPDATE net.edge e
SET road_gaz_id = g.gaz_id
FROM osm.highway h
JOIN geo.gazetteer g ON g.feature_class = 'ROAD' AND g.canonical = (
  SELECT min(h2.tags->>'name') FROM osm.highway h2 WHERE geo.norm_name(h2.tags->>'name') = geo.norm_name(h.tags->>'name'))
WHERE e.osm_way_id = h.way_id AND h.tags ? 'name';

-- 2. UNDERPASS / FLYOVER / BRIDGE: named ways with tunnel / bridge tags, one feature per name ---------
WITH named AS (
  SELECT geo.norm_name(tags->>'name') AS nm, tags->>'name' AS canonical, geom,
         CASE WHEN tags->>'tunnel' IN ('yes','building_passage') OR (tags ? 'layer' AND (tags->>'layer') ~ '^-' ) THEN 'UNDERPASS'
              WHEN tags->>'bridge' = 'yes' THEN 'FLYOVER' END AS fc
  FROM osm.highway
  WHERE tags ? 'name' AND (tags->>'tunnel' IN ('yes','building_passage') OR tags->>'bridge' = 'yes'
                          OR (tags ? 'layer' AND (tags->>'layer') ~ '^-'))
)
INSERT INTO geo.gazetteer (city_id, canonical, feature_class, geom, h3_r8, osm_type, popularity)
SELECT 1, min(canonical), fc, ST_LineMerge(ST_Collect(geom)), h3_lat_lng_to_cell(ST_Centroid(ST_Collect(geom)), 8), 'w', 0
FROM named WHERE nm IS NOT NULL AND fc IS NOT NULL GROUP BY nm, fc;

-- 3. JUNCTION / SIGNAL / LANDMARK points --------------------------------------------------------------
INSERT INTO geo.gazetteer (city_id, canonical, feature_class, geom, h3_r8, osm_type, osm_id, popularity)
SELECT 1, name,
       CASE kind WHEN 'JUNCTION' THEN 'JUNCTION' WHEN 'SIGNAL' THEN 'SIGNAL' ELSE 'LANDMARK' END,
       geom, h3_lat_lng_to_cell(geom, 8), 'n', node_id, 0
FROM osm.place_point WHERE kind IN ('JUNCTION','SIGNAL','LANDMARK') AND geo.norm_name(name) IS NOT NULL;

-- 4. AREA: polygons first, then place points that have no polygon of the same name -------------------
INSERT INTO geo.gazetteer (city_id, canonical, feature_class, geom, h3_r8, osm_type, osm_id, popularity)
SELECT 1, name, 'AREA', ST_Multi(geom), h3_lat_lng_to_cell(ST_Centroid(geom), 8), osm_type::char(1), osm_id,
       CASE kind WHEN 'PLACE' THEN 1.0 WHEN 'BOUNDARY' THEN 0.8 ELSE 0.5 END
FROM osm.place_area WHERE geo.norm_name(name) IS NOT NULL;

INSERT INTO geo.gazetteer (city_id, canonical, feature_class, geom, h3_r8, osm_type, osm_id, popularity)
SELECT 1, p.name, 'AREA', p.geom, h3_lat_lng_to_cell(p.geom, 8), 'n', p.node_id, 1.0
FROM osm.place_point p
WHERE p.kind = 'PLACE' AND geo.norm_name(p.name) IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM geo.gazetteer g WHERE g.feature_class = 'AREA' AND geo.norm_name(g.canonical) = geo.norm_name(p.name));

-- 5. Aliases: canonical name, alt/old names, local-script names (as typed), and the no-suffix form ---------
INSERT INTO geo.gazetteer_alias (alias, gaz_id, weight)
SELECT DISTINCT ON (a.alias, g.gaz_id) a.alias, g.gaz_id, a.weight
FROM geo.gazetteer g
CROSS JOIN LATERAL (
  SELECT geo.norm_name(g.canonical) AS alias, 1.0::real AS weight
  UNION ALL
  SELECT geo.norm_name(regexp_replace(g.canonical, '\s+(road|junction|flyover|underpass|circle|cross roads|x roads|signal|main road)$', '', 'i')), 0.9
  UNION ALL
  SELECT geo.norm_name(h.tags->>'alt_name'), 0.8 FROM osm.highway h WHERE g.osm_type = 'w' AND h.tags->>'name' = g.canonical AND h.tags ? 'alt_name'
  UNION ALL
  SELECT geo.norm_name(h.tags->>'old_name'), 0.7 FROM osm.highway h WHERE g.osm_type = 'w' AND h.tags->>'name' = g.canonical AND h.tags ? 'old_name'
  UNION ALL
  SELECT geo.norm_name(p.tags->>'alt_name'), 0.8 FROM osm.place_point p WHERE g.osm_type = 'n' AND p.node_id = g.osm_id AND p.tags ? 'alt_name'
) a
WHERE a.alias IS NOT NULL AND length(a.alias) >= 3
ORDER BY a.alias, g.gaz_id, a.weight DESC
ON CONFLICT DO NOTHING;

ANALYZE geo.gazetteer; ANALYZE geo.gazetteer_alias;

SELECT feature_class, count(*) FROM geo.gazetteer GROUP BY feature_class ORDER BY 2 DESC;
SELECT 'aliases' AS what, count(*) FROM geo.gazetteer_alias
UNION ALL SELECT 'edges linked to a road feature', count(*) FROM net.edge WHERE road_gaz_id IS NOT NULL;
