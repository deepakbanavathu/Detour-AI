-- Team routes: the regular trips a team wants in its daily road brief.
CREATE TABLE IF NOT EXISTS client.team_route (
  route_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id      SMALLINT NOT NULL DEFAULT 1,
  name         TEXT NOT NULL,                       -- 'Office run', 'Warehouse to Kukatpally'
  origin       GEOMETRY(POINT, 4326) NOT NULL,
  dest         GEOMETRY(POINT, 4326) NOT NULL,
  origin_label TEXT,
  dest_label   TEXT,
  profile      TEXT NOT NULL REFERENCES net.vehicle_profile (code),
  active       BOOLEAN NOT NULL DEFAULT true,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (city_id, name, profile)
);
