-- Subscriber trips as live route pages. Each corridor gets an unguessable token; the page at /go/<token> is that
-- person's own route card. The pack is cached on the corridor row, like team-route packs.
ALTER TABLE client.corridor ADD COLUMN IF NOT EXISTS token TEXT;
UPDATE client.corridor SET token = encode(gen_random_bytes(16), 'hex') WHERE token IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS corridor_token ON client.corridor (token);
ALTER TABLE client.corridor ALTER COLUMN token SET DEFAULT encode(gen_random_bytes(16), 'hex');
ALTER TABLE client.corridor ADD COLUMN IF NOT EXISTS pack JSONB;
ALTER TABLE client.corridor ADD COLUMN IF NOT EXISTS pack_edges JSONB;
ALTER TABLE client.corridor ADD COLUMN IF NOT EXISTS pack_built_at TIMESTAMPTZ;

-- alerts now open the live route page first; the open is recorded like the Maps and Waze links
ALTER TABLE client.alert_sent ADD COLUMN IF NOT EXISTS card_token TEXT;
ALTER TABLE client.alert_sent DROP CONSTRAINT IF EXISTS alert_sent_opened_in_check;
ALTER TABLE client.alert_sent ADD CONSTRAINT alert_sent_opened_in_check CHECK (opened_in IN ('maps','waze','card'));
