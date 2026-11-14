-- Route packs: up to three pre-computed alternates per saved team route, cached for the offline route card.
CREATE TABLE IF NOT EXISTS client.route_pack (
  route_id  BIGINT PRIMARY KEY REFERENCES client.team_route ON DELETE CASCADE,
  version   TEXT NOT NULL,                 -- hash of the three edge lists; the card refetches when it changes
  built_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  pack      JSONB NOT NULL,                -- what the phone receives
  edges     JSONB NOT NULL                 -- {"A": [[edge_id, length_m], ...], ...} kept server-side for status checks
);
