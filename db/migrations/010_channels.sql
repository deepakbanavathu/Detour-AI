-- Commuter channel: conversation state, outbox with delivery status, fan-out bookkeeping.

ALTER TABLE client.subscriber ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;   -- opens WhatsApp's 24 h free-form window

CREATE TABLE IF NOT EXISTS client.chat_state (
  channel         TEXT  NOT NULL CHECK (channel IN ('WHATSAPP','TELEGRAM')),
  channel_id_hmac BYTEA NOT NULL,
  step            TEXT  NOT NULL,
  data            JSONB NOT NULL DEFAULT '{}'::jsonb,     -- vehicle and windows only; pins live encrypted in client.corridor
  home_enc        BYTEA,                                  -- held only until the corridor is built
  work_enc        BYTEA,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (channel, channel_id_hmac)
);

CREATE TABLE IF NOT EXISTS client.outbox (
  out_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sub_id          BIGINT REFERENCES client.subscriber ON DELETE CASCADE,
  channel         TEXT NOT NULL CHECK (channel IN ('WHATSAPP','TELEGRAM','SMS')),
  kind            TEXT NOT NULL CHECK (kind IN ('ALERT','CLEARED','SMS_FALLBACK')),
  zone_id         BIGINT REFERENCES geo.zone_of_avoidance ON DELETE SET NULL,
  provider_msg_id TEXT,
  status          TEXT NOT NULL CHECK (status IN ('SENT','DELIVERED','READ','FAILED','NOT_SENT','FALLBACK_SENT')),
  detail          TEXT,                                   -- why NOT_SENT / FAILED
  sms_text        TEXT,                                   -- what the fallback would say; no pins, no phone number
  fallback_due_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_msg ON client.outbox (provider_msg_id) WHERE provider_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_fallback ON client.outbox (fallback_due_at) WHERE status = 'SENT' AND fallback_due_at IS NOT NULL;

ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS fanned_out_at TIMESTAMPTZ;
ALTER TABLE geo.zone_of_avoidance ADD COLUMN IF NOT EXISTS cleared_sent_at TIMESTAMPTZ;
-- zones that existed before the channel was switched on are never announced retroactively
UPDATE geo.zone_of_avoidance SET fanned_out_at = now() WHERE fanned_out_at IS NULL;

-- Meta redelivers webhooks; a message id seen once is not processed twice.
CREATE TABLE IF NOT EXISTS client.inbound_seen (
  provider_msg_id TEXT PRIMARY KEY,
  seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
