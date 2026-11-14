-- Optional: net.turn from edge pairs at each node, with OSM no_*/only_* restrictions applied.
-- The v0 pgRouting route ignores turns; GraphHopper's DimensionalWeighting.calcTurnWeight reads this table.
-- Cost: roughly k^2 rows per node of degree k (a few million rows for a metro); run once, after build_network.sql.

\timing on
SET work_mem = '512MB';
TRUNCATE net.turn;

INSERT INTO net.turn (in_edge, via_node, out_edge, deflection_deg, avail_radius_m, allowed_mask, osm_restriction)
SELECT p.in_edge, p.via_node, p.out_edge, p.d,
       LEAST(di.width_m, dout.width_m) * k.f,
       (SELECT COALESCE(bit_or(1 << v.profile_id), 0)::smallint FROM net.vehicle_profile v
         WHERE v.turn_radius_m <= LEAST(di.width_m, dout.width_m) * k.f),
       NULL
FROM (
  -- every (in, via, out) with the real travel direction through via
  SELECT i.edge_id AS in_edge, n.node_id AS via_node, o.edge_id AS out_edge,
         abs(((( CASE WHEN o.src = n.node_id THEN o.bearing_deg ELSE o.bearing_deg + 180 END)
              - (CASE WHEN i.dst = n.node_id THEN i.bearing_deg ELSE i.bearing_deg + 180 END)
              + 540)::int % 360) - 180)::real AS d
  FROM net.node n
  JOIN net.edge i ON (i.dst = n.node_id) OR (i.src = n.node_id AND NOT i.oneway)
  JOIN net.edge o ON ((o.src = n.node_id) OR (o.dst = n.node_id AND NOT o.oneway)) AND o.edge_id <> i.edge_id
) p
JOIN net.edge_dim di   ON di.edge_id   = p.in_edge
JOIN net.edge_dim dout ON dout.edge_id = p.out_edge
CROSS JOIN LATERAL (SELECT CASE WHEN p.d <= 60 THEN 1.15 WHEN p.d <= 100 THEN 1.0 WHEN p.d <= 140 THEN 0.75 ELSE 0.5 END AS f) k;

-- OSM restriction relations: members [{type:'w',ref,role:'from'},{type:'n',ref,role:'via'},{type:'w',ref,role:'to'}]
WITH r AS (
  SELECT rel.relation_id,
         COALESCE(rel.tags->>'restriction', rel.tags->>'restriction:motorcar') AS kind,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'from' AND m->>'type' = 'w' LIMIT 1) AS from_way,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'via'  AND m->>'type' = 'n' LIMIT 1) AS via_node,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'to'   AND m->>'type' = 'w' LIMIT 1) AS to_way
  FROM osm.restriction rel
),
pairs AS (
  SELECT r.kind, t.in_edge, t.via_node, t.out_edge
  FROM r
  JOIN net.edge ei ON ei.osm_way_id = r.from_way AND r.via_node IN (ei.src, ei.dst)
  JOIN net.edge eo ON eo.osm_way_id = r.to_way   AND r.via_node IN (eo.src, eo.dst)
  JOIN net.turn t ON t.in_edge = ei.edge_id AND t.via_node = r.via_node AND t.out_edge = eo.edge_id
  WHERE r.from_way IS NOT NULL AND r.via_node IS NOT NULL AND r.to_way IS NOT NULL
)
UPDATE net.turn t SET allowed_mask = 0, osm_restriction = p.kind
FROM pairs p
WHERE t.in_edge = p.in_edge AND t.via_node = p.via_node AND t.out_edge = p.out_edge AND p.kind LIKE 'no\_%';

-- only_* : forbid every other exit from the same in-edge at that node
WITH r AS (
  SELECT COALESCE(rel.tags->>'restriction', rel.tags->>'restriction:motorcar') AS kind,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'from' AND m->>'type' = 'w' LIMIT 1) AS from_way,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'via'  AND m->>'type' = 'n' LIMIT 1) AS via_node,
         (SELECT (m->>'ref')::bigint FROM jsonb_array_elements(rel.members) m WHERE m->>'role' = 'to'   AND m->>'type' = 'w' LIMIT 1) AS to_way
  FROM osm.restriction rel
),
allowed AS (
  SELECT r.kind, ei.edge_id AS in_edge, r.via_node, eo.edge_id AS out_edge
  FROM r
  JOIN net.edge ei ON ei.osm_way_id = r.from_way AND r.via_node IN (ei.src, ei.dst)
  JOIN net.edge eo ON eo.osm_way_id = r.to_way   AND r.via_node IN (eo.src, eo.dst)
  WHERE r.kind LIKE 'only\_%'
)
UPDATE net.turn t SET allowed_mask = 0, osm_restriction = a.kind
FROM allowed a
WHERE t.in_edge = a.in_edge AND t.via_node = a.via_node AND t.out_edge <> a.out_edge;

ANALYZE net.turn;
SELECT 'turns' AS what, count(*) FROM net.turn
UNION ALL SELECT 'turns closed by OSM restrictions', count(*) FROM net.turn WHERE osm_restriction IS NOT NULL;
