-- Builds the dimensional network (Module 2) from osm.highway / osm.point_feature.
-- Re-runnable. Edges are rebuilt; nodes are kept and updated in place (node_id is the OSM node id), so rows that
-- reference nodes survive a re-import: hand-mapped gates (net.gate), sinks with their hydrology, and submersion
-- alert history. Gate cut sets are carried over to the new edge ids through (osm_way_id, seg).

\timing on
SET work_mem = '512MB';
SET maintenance_work_mem = '2GB';

-- remember which OSM way segment each gate's cut-set edge was, before edge ids are reissued
DROP TABLE IF EXISTS net.gate_cut_old;
CREATE TABLE net.gate_cut_old AS
SELECT g.node_id, e.osm_way_id, e.seg
FROM net.gate g CROSS JOIN LATERAL unnest(g.cut_set) AS c(edge_id) JOIN net.edge e ON e.edge_id = c.edge_id;

-- no CASCADE: these are all the tables that reference net.edge, and nothing outside net.* is emptied
TRUNCATE net.edge_dynamic, net.edge_h3, net.edge_dim, net.turn, net.edge RESTART IDENTITY;

-- 1. node lists and geometry vertices, per way ------------------------------------------------
DROP TABLE IF EXISTS osm.way_node;
CREATE TABLE osm.way_node AS
SELECT w.way_id, s.i::int AS seq, s.node_id::bigint AS node_id
FROM osm.highway w
CROSS JOIN LATERAL unnest(string_to_array(w.nodes, ',')) WITH ORDINALITY AS s(node_id, i);
CREATE INDEX ON osm.way_node (node_id);
CREATE INDEX ON osm.way_node (way_id, seq);

DROP TABLE IF EXISTS osm.way_vertex;
CREATE TABLE osm.way_vertex AS
SELECT w.way_id, dp.path[1]::int AS seq, dp.geom
FROM osm.highway w
CROSS JOIN LATERAL ST_DumpPoints(w.geom) AS dp;
CREATE INDEX ON osm.way_vertex (way_id, seq);

-- ways whose vertex count differs from their node count are skipped (rare: invalid geometries)
DROP TABLE IF EXISTS osm.way_ok;
CREATE TABLE osm.way_ok AS
SELECT way_id FROM (SELECT way_id, count(*) AS c FROM osm.way_node   GROUP BY way_id) a
JOIN               (SELECT way_id, count(*) AS c FROM osm.way_vertex GROUP BY way_id) b USING (way_id, c);

-- 2. split nodes: shared between ways, way ends, barriers --------------------------------------
DROP TABLE IF EXISTS osm.split_node;
CREATE TABLE osm.split_node AS
SELECT node_id FROM (
  SELECT wn.node_id, count(*) AS n_uses, bool_or(wn.seq = 1 OR wn.seq = m.max_seq) AS is_end
  FROM osm.way_node wn
  JOIN (SELECT way_id, max(seq) AS max_seq FROM osm.way_node GROUP BY way_id) m USING (way_id)
  GROUP BY wn.node_id) t
WHERE n_uses > 1 OR is_end
UNION
SELECT node_id FROM osm.point_feature WHERE tags ? 'barrier';
CREATE UNIQUE INDEX ON osm.split_node (node_id);

-- 3. segments between split nodes --------------------------------------------------------------
DROP TABLE IF EXISTS net.edge_raw;
CREATE TABLE net.edge_raw AS
WITH v AS (
  SELECT wv.way_id, wv.seq, wn.node_id, wv.geom,
         (sn.node_id IS NOT NULL) AS is_split,
         sum(CASE WHEN sn.node_id IS NOT NULL AND wv.seq > 1 THEN 1 ELSE 0 END)
           OVER (PARTITION BY wv.way_id ORDER BY wv.seq) AS seg_after
  FROM osm.way_vertex wv
  JOIN osm.way_ok USING (way_id)
  JOIN osm.way_node wn USING (way_id, seq)
  LEFT JOIN osm.split_node sn USING (node_id)
),
v2 AS (                      -- a split vertex ends one segment and starts the next
  SELECT way_id, seq, node_id, geom, seg_after AS seg FROM v
  UNION ALL
  SELECT way_id, seq, node_id, geom, seg_after - 1 FROM v WHERE is_split AND seq > 1
)
SELECT way_id, seg::smallint AS seg,
       ST_MakeLine(geom ORDER BY seq) AS geom,
       (array_agg(node_id ORDER BY seq))[1]      AS src_node,
       (array_agg(node_id ORDER BY seq DESC))[1] AS dst_node
FROM v2
GROUP BY way_id, seg
HAVING count(*) >= 2;

-- 4. nodes ---------------------------------------------------------------------------------------
DROP TABLE IF EXISTS net.node_new;
CREATE TABLE net.node_new AS
SELECT DISTINCT ON (n.node_id) n.node_id, n.geom, h3_lat_lng_to_cell(n.geom, 11) AS h3_r11,
       pf.tags->>'barrier' AS barrier, COALESCE(pf.tags->>'highway' = 'traffic_signals', false) AS signal
FROM (SELECT src_node AS node_id, ST_StartPoint(geom) AS geom FROM net.edge_raw
      UNION ALL
      SELECT dst_node, ST_EndPoint(geom) FROM net.edge_raw) n
LEFT JOIN osm.point_feature pf ON pf.node_id = n.node_id
ORDER BY n.node_id;
CREATE UNIQUE INDEX ON net.node_new (node_id);

-- nodes gone from OSM are dropped unless something still points at them
DELETE FROM net.node n
WHERE NOT EXISTS (SELECT 1 FROM net.node_new w WHERE w.node_id = n.node_id)
  AND NOT EXISTS (SELECT 1 FROM net.gate x WHERE x.node_id = n.node_id)
  AND NOT EXISTS (SELECT 1 FROM hydro.sink x WHERE x.node_id = n.node_id)
  AND NOT EXISTS (SELECT 1 FROM telemetry.hpvse_alert x WHERE x.sink_id = n.node_id);

-- update in place: position and OSM tags change, hydrology columns (elev_m ... underpass) are kept
INSERT INTO net.node (node_id, geom, h3_r11, barrier, signal)
SELECT node_id, geom, h3_r11, barrier, signal FROM net.node_new
ON CONFLICT (node_id) DO UPDATE SET geom = EXCLUDED.geom, h3_r11 = EXCLUDED.h3_r11, barrier = EXCLUDED.barrier, signal = EXCLUDED.signal;
DROP TABLE net.node_new;

-- 5. edges with attributes ------------------------------------------------------------------------
INSERT INTO net.edge (osm_way_id, seg, src, dst, geom, length_m, bearing_deg, highway, highway_rank,
                      name, oneway, lanes, freeflow_kmh, h3_r9)
SELECT x.way_id, x.seg, x.src, x.dst, x.g,
       ST_Length(x.g::geography)::real,
       COALESCE(degrees(ST_Azimuth(ST_StartPoint(x.g)::geography, ST_EndPoint(x.g)::geography)), 0)::real,
       x.hw, x.rank, x.name, x.oneway, x.lanes, x.ffs,
       ARRAY(SELECT DISTINCT h3_lat_lng_to_cell(p.geom, 9)
             FROM ST_DumpPoints(ST_Segmentize(x.g::geography, 30)::geometry) AS p)
FROM (
  SELECT r.way_id, r.seg,
         CASE WHEN t.oneway_tag = '-1' THEN r.dst_node ELSE r.src_node END AS src,
         CASE WHEN t.oneway_tag = '-1' THEN r.src_node ELSE r.dst_node END AS dst,
         CASE WHEN t.oneway_tag = '-1' THEN ST_Reverse(r.geom) ELSE r.geom END AS g,
         t.hw, t.rank, t.name, t.lanes, t.ffs,
         COALESCE(t.oneway_tag IN ('yes','1','true','-1'), false) OR COALESCE(t.junction IN ('roundabout','circular'), false)
          OR t.hw IN ('motorway','motorway_link') AS oneway
  FROM net.edge_raw r
  JOIN (
    SELECT way_id,
           tags->>'highway' AS hw, tags->>'name' AS name, tags->>'oneway' AS oneway_tag, tags->>'junction' AS junction,
           LEAST(NULLIF(regexp_replace(COALESCE(tags->>'lanes',''), '[^0-9].*$', ''), '')::int, 8)::smallint AS lanes,
           CASE tags->>'highway'
             WHEN 'motorway' THEN 1 WHEN 'motorway_link' THEN 1 WHEN 'trunk' THEN 1 WHEN 'trunk_link' THEN 1
             WHEN 'primary' THEN 2 WHEN 'primary_link' THEN 2 WHEN 'secondary' THEN 3 WHEN 'secondary_link' THEN 3
             WHEN 'tertiary' THEN 4 WHEN 'tertiary_link' THEN 4 WHEN 'unclassified' THEN 5 WHEN 'residential' THEN 6
             WHEN 'living_street' THEN 7 ELSE 8 END::smallint AS rank,
           COALESCE(NULLIF(regexp_replace(COALESCE(tags->>'maxspeed',''), '[^0-9].*$', ''), '')::real,
             CASE tags->>'highway'
               WHEN 'motorway' THEN 80 WHEN 'motorway_link' THEN 50 WHEN 'trunk' THEN 60 WHEN 'trunk_link' THEN 40
               WHEN 'primary' THEN 50 WHEN 'primary_link' THEN 35 WHEN 'secondary' THEN 45 WHEN 'secondary_link' THEN 30
               WHEN 'tertiary' THEN 40 WHEN 'tertiary_link' THEN 30 WHEN 'unclassified' THEN 35 WHEN 'residential' THEN 30
               WHEN 'living_street' THEN 20 ELSE 20 END)::real AS ffs
    FROM osm.highway
  ) t USING (way_id)
) x;

-- 6. cell -> edge index (frac: equal share per cell; replaced by length share when Module 3 lands)
INSERT INTO net.edge_h3 (h3_r9, edge_id, frac)
SELECT c, e.edge_id, 1.0 / cardinality(e.h3_r9)
FROM net.edge e, unnest(e.h3_r9) AS c
ON CONFLICT DO NOTHING;

-- 7. dimensions and static exclusion mask ----------------------------------------------------------
INSERT INTO net.edge_dim (edge_id, width_m, width_src, clearance_m, max_gvw_t, surface_class, median,
                          filterable, alley, layout_private, speed_breakers, static_mask)
SELECT e.edge_id,
       w.width_m, w.width_src,
       net.parse_width(t.tags->>'maxheight'),
       NULLIF(regexp_replace(COALESCE(t.tags->>'maxweight',''), '[^0-9.].*$', ''), '')::real,
       CASE COALESCE(t.tags->>'surface', 'asphalt')
            WHEN 'asphalt' THEN 0 WHEN 'concrete' THEN 1 WHEN 'paving_stones' THEN 2 ELSE 3 END,
       COALESCE(e.lanes >= 4 AND e.oneway, false),
       (COALESCE(e.lanes, 1) >= 2 AND w.width_m >= 6.5 AND COALESCE(t.tags->>'motorcycle', 'yes') <> 'no'),
       w.width_m < 2.5,
       (COALESCE(t.tags->>'access','') IN ('private','residents','permit')
        OR EXISTS (SELECT 1 FROM geo.gated_layout g WHERE ST_Within(e.geom, g.geom))),
       (SELECT count(*) FROM osm.point_feature pf
         WHERE pf.tags ? 'traffic_calming' AND ST_DWithin(pf.geom, e.geom, 0.00003))::smallint,
       0
FROM net.edge e
JOIN osm.highway t ON t.way_id = e.osm_way_id
CROSS JOIN LATERAL (
  SELECT COALESCE(net.parse_width(t.tags->>'width'),
                  e.lanes * 3.0 + 0.5,
                  CASE e.highway
                    WHEN 'motorway' THEN 14.0 WHEN 'trunk' THEN 14.0 WHEN 'primary' THEN 10.5 WHEN 'secondary' THEN 9.0
                    WHEN 'tertiary' THEN 7.0 WHEN 'unclassified' THEN 5.0 WHEN 'residential' THEN 5.5
                    WHEN 'living_street' THEN 3.5 WHEN 'service' THEN 3.0 ELSE 5.0 END)::real AS width_m,
         CASE WHEN t.tags ? 'width' AND net.parse_width(t.tags->>'width') IS NOT NULL THEN 'OSM_TAG'
              WHEN e.lanes IS NOT NULL THEN 'LANES_INFER' ELSE 'CLASS_DEFAULT' END AS width_src
) w;

UPDATE net.edge_dim d SET access_mask = net.access_mask(t.tags)
FROM net.edge e JOIN osm.highway t ON t.way_id = e.osm_way_id WHERE d.edge_id = e.edge_id;

UPDATE net.edge_dim d
SET static_mask = net.static_mask(e.oneway, d.width_m, d.clearance_m, d.max_gvw_t, d.layout_private) & d.access_mask
FROM net.edge e WHERE e.edge_id = d.edge_id;

-- gate cut sets onto the new edge ids
UPDATE net.gate g SET cut_set = COALESCE((
  SELECT array_agg(DISTINCT e.edge_id ORDER BY e.edge_id) FROM net.gate_cut_old o JOIN net.edge e USING (osm_way_id, seg)
  WHERE o.node_id = g.node_id), '{}');
DROP TABLE net.gate_cut_old;

-- Barrier nodes that are gates/bollards: edges touching them are closed to 4W+ by default (CLOSED_REPORTED)
-- until a field check says otherwise. Bollards and cycle barriers keep 2W only.
INSERT INTO net.edge_dynamic (edge_id)
SELECT edge_id FROM net.edge;

UPDATE net.edge_dynamic y
SET gate_state = 'UNKNOWN', p_open_now = 0.5, two_wheeler_gap = (n.barrier IN ('bollard','cycle_barrier','block'))
FROM net.edge e JOIN net.node n ON n.node_id IN (e.src, e.dst)
WHERE y.edge_id = e.edge_id AND n.barrier IN ('gate','lift_gate','swing_gate','bollard','cycle_barrier','block')
  AND e.highway_rank >= 6;

ANALYZE net.node; ANALYZE net.edge; ANALYZE net.edge_dim; ANALYZE net.edge_h3; ANALYZE net.edge_dynamic;

-- 8. report ------------------------------------------------------------------------------------------
SELECT 'ways skipped (vertex/node mismatch)' AS what, count(*) FROM osm.highway h
WHERE NOT EXISTS (SELECT 1 FROM osm.way_ok o WHERE o.way_id = h.way_id)
UNION ALL SELECT 'nodes', count(*) FROM net.node
UNION ALL SELECT 'edges', count(*) FROM net.edge
UNION ALL SELECT 'edges open to 2W',  count(*) FROM net.edge_dim WHERE static_mask & 1 > 0
UNION ALL SELECT 'edges open to SUV', count(*) FROM net.edge_dim WHERE static_mask & 8 > 0
UNION ALL SELECT 'edges open to HCV', count(*) FROM net.edge_dim WHERE static_mask & 32 > 0
UNION ALL SELECT 'edges at unknown gates', count(*) FROM net.edge_dynamic WHERE gate_state = 'UNKNOWN';
