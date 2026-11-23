-- What happens on the live route page, with the rider's agreement: start, switch, leaving the route, arrival.
-- No continuous track is kept: a departure stores only the last point on the route and the stretch skipped next.
-- From departures of several riders at the same stretch DetourAI opens a correction by itself ('SKIPPED').

CREATE TABLE IF NOT EXISTS client.trip_event (
  event_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  corr_id    BIGINT NOT NULL REFERENCES client.corridor ON DELETE CASCADE,
  sub_id     BIGINT REFERENCES client.subscriber ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('START','SWITCH','OFF_ROUTE','ARRIVE')),
  route      TEXT CHECK (route IN ('A','B','C')),
  profile    TEXT,
  geom       GEOMETRY(Point, 4326),                -- OFF_ROUTE only: the last point on the route
  edge_id    BIGINT,                               -- OFF_ROUTE only: the stretch the rider did not take
  osm_way_id BIGINT,
  seg        INT,
  at         TIMESTAMPTZ NOT NULL,                 -- when it happened on the phone (sent later if offline)
  received   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trip_event_recent ON client.trip_event (at DESC);
CREATE INDEX IF NOT EXISTS trip_event_skip ON client.trip_event (osm_way_id, seg, at) WHERE kind = 'OFF_ROUTE';

ALTER TABLE client.route_feedback DROP CONSTRAINT IF EXISTS route_feedback_kind_check;
ALTER TABLE client.route_feedback ADD CONSTRAINT route_feedback_kind_check
  CHECK (kind IN ('BLOCKED','GATE_CLOSED','TOO_NARROW','NO_ENTRY','CHOICE','SKIPPED'));
