-- A counter that moves whenever anything a route depends on changes: zone penalties, gate states, water depth,
-- widths and masks, or the network itself. The router caches results per counter value, so a repeated request is
-- answered from memory until the next change and never after it.
CREATE TABLE IF NOT EXISTS net.dyn_epoch (
  one   BOOLEAN PRIMARY KEY DEFAULT true CHECK (one),
  epoch BIGINT NOT NULL DEFAULT 0
);
INSERT INTO net.dyn_epoch (one, epoch) VALUES (true, 0) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION net.bump_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE net.dyn_epoch SET epoch = epoch + 1;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS dyn_epoch_change ON net.edge_dynamic;
CREATE TRIGGER dyn_epoch_change AFTER INSERT OR UPDATE OR DELETE ON net.edge_dynamic FOR EACH STATEMENT EXECUTE FUNCTION net.bump_epoch();
DROP TRIGGER IF EXISTS dyn_epoch_truncate ON net.edge_dynamic;
CREATE TRIGGER dyn_epoch_truncate AFTER TRUNCATE ON net.edge_dynamic FOR EACH STATEMENT EXECUTE FUNCTION net.bump_epoch();
DROP TRIGGER IF EXISTS dim_epoch_change ON net.edge_dim;
CREATE TRIGGER dim_epoch_change AFTER INSERT OR UPDATE OR DELETE ON net.edge_dim FOR EACH STATEMENT EXECUTE FUNCTION net.bump_epoch();
