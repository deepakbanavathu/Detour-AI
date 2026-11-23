-- The Waze link in an alert goes through the same short-link redirector as the Google Maps link,
-- so it is counted, expires with the alert, and can only lead to Waze's own navigation address.
ALTER TABLE client.alert_sent ADD COLUMN IF NOT EXISTS waze_token TEXT;
ALTER TABLE client.alert_sent ADD COLUMN IF NOT EXISTS opened_in TEXT;   -- the allowed values are set in 016, so re-applying this file never narrows them
