-- Modules 3, 4, 5: subscriptions, valve, hydrology, telemetry windows, gates.
-- Created now so the schema is complete; the demo fills client.* in weeks 5-6 and hydro/telemetry in weeks 7-8.

-- ---------- client (Module 5.1, WhatsApp / Telegram corridors) ----------
CREATE TABLE IF NOT EXISTS client.subscriber (
  sub_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel              TEXT NOT NULL CHECK (channel IN ('WHATSAPP','TELEGRAM','WEB')),
  channel_id_hmac      BYTEA NOT NULL,                 -- HMAC-SHA256(wa_id or chat id, pepper); pepper lives outside the DB
  channel_id_enc       BYTEA NOT NULL,                 -- pgp_sym_encrypt(id) for sending; key from the KMS
  profile              TEXT NOT NULL CHECK (profile IN ('2W','4W','BOTH')),
  lang                 TEXT NOT NULL DEFAULT 'en',
  consent_at           TIMESTAMPTZ NOT NULL,           -- DPDP: explicit, logged, revocable
  consent_text_version TEXT NOT NULL,
  age_confirmed        BOOLEAN NOT NULL DEFAULT false,
  stopped_at           TIMESTAMPTZ,
  UNIQUE (channel, channel_id_hmac)
);

CREATE TABLE IF NOT EXISTS client.corridor (
  corr_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sub_id      BIGINT NOT NULL REFERENCES client.subscriber ON DELETE CASCADE,
  label       TEXT NOT NULL,                           -- 'home-work', 'work-home', 'custom-1'
  origin_enc  BYTEA NOT NULL,                          -- encrypted pins; decrypted only in the corridor-build job
  dest_enc    BYTEA NOT NULL,
  bearing_deg REAL NOT NULL,
  cells_r9    H3INDEX[] NOT NULL,
  windows     INT4RANGE[] NOT NULL,                    -- minutes of day
  dows        SMALLINT[] NOT NULL DEFAULT '{1,2,3,4,5}',
  built_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS corridor_cells ON client.corridor USING GIN (cells_r9);

CREATE TABLE IF NOT EXISTS client.alert_sent (
  sub_id    BIGINT NOT NULL REFERENCES client.subscriber ON DELETE CASCADE,
  zone_id   BIGINT NOT NULL REFERENCES geo.zone_of_avoidance ON DELETE CASCADE,
  corr_id   BIGINT NOT NULL,
  sent_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  channel   TEXT NOT NULL,
  provider_msg_id TEXT,
  token     TEXT,                                      -- short-link token
  opened_at TIMESTAMPTZ,
  PRIMARY KEY (sub_id, zone_id)
);

CREATE TABLE IF NOT EXISTS client.short_link (
  token      TEXT PRIMARY KEY,
  target_url TEXT NOT NULL,                            -- server-generated Google Maps URL only
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
);

CREATE TABLE IF NOT EXISTS client.trigger_grid (        -- fleet SDK geofences (res 7)
  h3_r7              H3INDEX PRIMARY KEY,
  city_id            SMALLINT NOT NULL DEFAULT 1,
  active_zones       INT NOT NULL DEFAULT 0,
  active_bottlenecks INT NOT NULL DEFAULT 0,
  center             GEOMETRY(POINT, 4326) NOT NULL,
  radius_m           REAL NOT NULL DEFAULT 1370,
  version            BIGINT NOT NULL DEFAULT 1,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- flow (Module 3) ----------
CREATE TABLE IF NOT EXISTS flow.h3_cell (
  h3_r9      H3INDEX PRIMARY KEY,
  city_id    SMALLINT NOT NULL DEFAULT 1,
  cap_veh_h  REAL NOT NULL,
  zone_class TEXT NOT NULL CHECK (zone_class IN ('ARTERIAL','RESIDENTIAL','GATED','INDUSTRIAL')),
  lambda     REAL NOT NULL,
  t0_s       REAL NOT NULL,
  alley_only BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS flow.cell_override (
  h3_r9  H3INDEX NOT NULL REFERENCES flow.h3_cell,
  lambda REAL NOT NULL,
  dow    SMALLINT[] NOT NULL,
  hours  INT4RANGE NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flow.cell_load (
  h3_r9    H3INDEX NOT NULL,
  tick_ts  TIMESTAMPTZ NOT NULL,
  n_obs    INT NOT NULL,
  phi      REAL NOT NULL,
  load_est REAL NOT NULL,
  PRIMARY KEY (h3_r9, tick_ts)
);
SELECT create_hypertable('flow.cell_load', 'tick_ts', chunk_time_interval => INTERVAL '6 hours', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS flow.bottleneck (
  bn_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id       SMALLINT NOT NULL DEFAULT 1,
  zone_id       BIGINT REFERENCES geo.zone_of_avoidance,
  cut_edges     BIGINT[] NOT NULL,
  direction_deg REAL NOT NULL,
  catchment_r8  H3INDEX[] NOT NULL,
  sink_r9       H3INDEX[] NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('ACTIVE','DRAINING','CLOSED')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS flow.corridor (
  corr_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bn_id         BIGINT NOT NULL REFERENCES flow.bottleneck ON DELETE CASCADE,
  profile_class TEXT NOT NULL CHECK (profile_class IN ('2W','4W')),
  cells         H3INDEX[] NOT NULL,
  t0_s          REAL NOT NULL,
  active        BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS flow.quota (
  bn_id BIGINT NOT NULL, tick_ts TIMESTAMPTZ NOT NULL, corr_id BIGINT NOT NULL,
  demand REAL NOT NULL, cap_tick REAL NOT NULL, quota REAL NOT NULL, mu REAL NOT NULL,
  PRIMARY KEY (bn_id, tick_ts, corr_id)
);
CREATE TABLE IF NOT EXISTS flow.assignment (
  vehicle_id TEXT NOT NULL, bn_id BIGINT NOT NULL, corr_id BIGINT NOT NULL,
  hash_r REAL NOT NULL, assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (vehicle_id, bn_id)
);

-- ---------- hydro (Module 4) ----------
CREATE TABLE IF NOT EXISTS hydro.sink (
  node_id     BIGINT PRIMARY KEY REFERENCES net.node ON DELETE CASCADE,
  pond_geom   GEOMETRY(POLYGON) NOT NULL,              -- city UTM SRID
  a_pond_m2   REAL NOT NULL,
  v_max_m3    REAL NOT NULL,
  spill_m     REAL NOT NULL,
  a_catch_m2  REAL NOT NULL,
  psi         REAL NOT NULL DEFAULT 0.85,
  q_drain_m3s REAL NOT NULL,
  kappa       REAL NOT NULL DEFAULT 1.0,
  stage_curve REAL[] NOT NULL,
  v_now_m3    REAL NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hydro.gauge (
  gauge_id SMALLINT PRIMARY KEY, city_id SMALLINT NOT NULL DEFAULT 1,
  operator TEXT NOT NULL CHECK (operator IN ('KSNDMC','TSDPS','IMD_AWS','PRIVATE','MANUAL')),
  geom GEOMETRY(POINT, 4326) NOT NULL, h3_r8 H3INDEX NOT NULL, elev_m REAL
);
CREATE TABLE IF NOT EXISTS hydro.rain_obs (
  gauge_id SMALLINT NOT NULL, ts TIMESTAMPTZ NOT NULL, mm_15 REAL NOT NULL, PRIMARY KEY (gauge_id, ts));
SELECT create_hypertable('hydro.rain_obs', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE TABLE IF NOT EXISTS hydro.rain_cell (
  h3_r8 H3INDEX NOT NULL, ts TIMESTAMPTZ NOT NULL,
  i_mm_h REAL NOT NULL, acc_1h REAL NOT NULL, acc_3h REAL NOT NULL, api_72h REAL NOT NULL,
  n_gauges SMALLINT NOT NULL, PRIMARY KEY (h3_r8, ts));
SELECT create_hypertable('hydro.rain_cell', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

-- ---------- telemetry (Modules 4, 5) ----------
CREATE TABLE IF NOT EXISTS telemetry.ping (
  vehicle_id   TEXT NOT NULL,
  fleet_id     TEXT NOT NULL,
  profile      TEXT NOT NULL,
  ts           TIMESTAMPTZ NOT NULL,
  geom         GEOMETRY(POINT, 4326) NOT NULL,
  speed_kmh    REAL NOT NULL,
  heading_deg  REAL,
  hdop         REAL,
  ignition     BOOLEAN,
  accel_peak_g REAL,
  edge_id      BIGINT,                                 -- map-matched
  sink_id      BIGINT,
  sink_dist_m  REAL,
  PRIMARY KEY (vehicle_id, ts)
);
SELECT create_hypertable('telemetry.ping', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ping_geom ON telemetry.ping USING GIST (geom);

CREATE TABLE IF NOT EXISTS telemetry.edge_window (
  edge_id BIGINT NOT NULL, window_end TIMESTAMPTZ NOT NULL,
  recommended INT NOT NULL, expected REAL NOT NULL, observed INT NOT NULL, observed_2w INT NOT NULL,
  reversals INT NOT NULL, PRIMARY KEY (edge_id, window_end));
SELECT create_hypertable('telemetry.edge_window', 'window_end', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS telemetry.hpvse_alert (
  alert_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sink_id    BIGINT NOT NULL REFERENCES net.node,
  first_ts   TIMESTAMPTZ NOT NULL,
  fired_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  n_stalled  INT NOT NULL, u_turn_ratio REAL NOT NULL, downstream_ratio REAL NOT NULL,
  cluster_radius_m REAL NOT NULL, p_submerge REAL NOT NULL, depth_p90 REAL NOT NULL,
  severity   REAL NOT NULL, delivered JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------- gates (Module 5.2) ----------
CREATE TABLE IF NOT EXISTS net.gate (
  node_id   BIGINT PRIMARY KEY REFERENCES net.node ON DELETE CASCADE,
  layout_id BIGINT REFERENCES geo.gated_layout,
  kind      TEXT NOT NULL CHECK (kind IN ('OSM_BARRIER','INFERRED','FIELD')),
  cut_set   BIGINT[] NOT NULL                          -- edges reachable from the arterial network only via this node
);
CREATE TABLE IF NOT EXISTS net.gate_schedule (
  node_id BIGINT NOT NULL REFERENCES net.gate ON DELETE CASCADE,
  how SMALLINT NOT NULL CHECK (how BETWEEN 0 AND 167),
  alpha_open REAL NOT NULL DEFAULT 1, beta_closed REAL NOT NULL DEFAULT 1,
  PRIMARY KEY (node_id, how)
);
CREATE TABLE IF NOT EXISTS net.gate_state_history (
  node_id BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL, state TEXT NOT NULL,
  expected REAL NOT NULL, observed INT NOT NULL, observed_2w INT NOT NULL, reversals INT NOT NULL,
  trigger TEXT NOT NULL, PRIMARY KEY (node_id, ts)
);
