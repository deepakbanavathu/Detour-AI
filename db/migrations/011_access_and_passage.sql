-- Fixes for three findings of the v0.8 verification.
--  1. OSM access tags close edges per vehicle (V03-V05).
--  2. Fit is physical, single file: a vehicle fits a lane at least its own width plus 0.4 m, one-way or two-way.
--     Passing an oncoming vehicle is a cost (routing.py), not a ban; the old rule barred scooters from every
--     two-way lane under 3 m, including the gate gaps they actually use (V07).
--  3. (import/build_network.sql) a re-import keeps nodes, so hand-mapped gates, sinks and alert history survive (I05).

ALTER TABLE net.edge_dim ADD COLUMN IF NOT EXISTS access_mask SMALLINT NOT NULL DEFAULT 127;   -- bit i: OSM access allows profile i

-- First tag present wins, most specific first. Only 'no' closes: private, destination, permissive and the rest stay
-- open, because gated layouts (access=private) are handled by the layout rule and the gate cost, not by a ban.
CREATE OR REPLACE FUNCTION net.mode_open(tags JSONB, VARIADIC keys TEXT[]) RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE((SELECT v <> 'no'
                   FROM (SELECT lower(btrim(tags->>k)) AS v, i FROM unnest(keys) WITH ORDINALITY AS u(k, i)) q
                   WHERE v IS NOT NULL AND v <> '' ORDER BY i LIMIT 1), true)
$$;

CREATE OR REPLACE FUNCTION net.access_mask(tags JSONB) RETURNS SMALLINT LANGUAGE sql IMMUTABLE AS $$
  SELECT (  CASE WHEN net.mode_open(tags, 'motorcycle', 'motor_vehicle', 'vehicle', 'access') THEN 1 ELSE 0 END    -- 2W
          | CASE WHEN net.mode_open(tags, 'motor_vehicle', 'vehicle', 'access')               THEN 2 ELSE 0 END    -- 3W (no OSM key of its own)
          | CASE WHEN net.mode_open(tags, 'motorcar', 'motor_vehicle', 'vehicle', 'access')   THEN 12 ELSE 0 END   -- hatch, SUV
          | CASE WHEN net.mode_open(tags, 'goods', 'motor_vehicle', 'vehicle', 'access')      THEN 16 ELSE 0 END   -- LCV
          | CASE WHEN net.mode_open(tags, 'hgv', 'motor_vehicle', 'vehicle', 'access')        THEN 32 ELSE 0 END   -- HCV
          | CASE WHEN net.mode_open(tags, 'bus', 'psv', 'motor_vehicle', 'vehicle', 'access') THEN 64 ELSE 0 END   -- bus
         )::smallint
$$;

-- Single-file fit. The oneway argument stays for callers but no longer changes the answer.
CREATE OR REPLACE FUNCTION net.static_mask(oneway BOOLEAN, width REAL, clearance REAL, gvw REAL, priv BOOLEAN)
RETURNS SMALLINT LANGUAGE sql STABLE AS $$
  SELECT COALESCE(bit_or(1 << p.profile_id), 0)::smallint
  FROM net.vehicle_profile p
  WHERE width >= p.width_m + 0.4
    AND (clearance IS NULL OR clearance >= p.height_m + 0.15)
    AND (gvw IS NULL OR gvw >= p.gvw_t)
    AND NOT (priv AND p.profile_id >= 4)                                      -- LCV/HCV/BUS never enter gated layouts
$$;

-- Recompute masks on a database that already has a network, so the fixes apply without a re-import.
-- (motor_vehicle, vehicle, goods, psv and bus tags reach osm.highway only after the next import with the new detour.lua.)
DO $$
BEGIN
  IF to_regclass('osm.highway') IS NOT NULL THEN
    UPDATE net.edge_dim d SET access_mask = net.access_mask(t.tags)
    FROM net.edge e JOIN osm.highway t ON t.way_id = e.osm_way_id WHERE d.edge_id = e.edge_id;
  END IF;
  UPDATE net.edge_dim d SET static_mask = net.static_mask(e.oneway, d.width_m, d.clearance_m, d.max_gvw_t, d.layout_private) & d.access_mask
  FROM net.edge e WHERE d.edge_id = e.edge_id;
END $$;
