# Commuter channel setup: WhatsApp, Telegram, SMS fallback

The channel code runs with nothing configured and simply sends nothing. Switch each part on as its account is ready.
Do them in this order: Telegram (ten minutes, free), WhatsApp test number (an hour), WhatsApp template (days, Meta's
review), SMS (days to weeks, DLT).

## 0. Before any real subscriber

1. Set `PII_KEY` and `PII_PEPPER` in `.env` to two different random strings:
   `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Phone numbers and pins are encrypted with the key;
   chat ids are matched through an HMAC with the pepper. **Changing either later makes existing subscribers unreadable.**
2. Give the API a public https address (a Cloudflare Tunnel hostname) and set `PUBLIC_BASE_URL` to it, with no trailing slash.
3. `docker compose up -d --build` then `docker compose exec db bash /migrate.sh` (migration 010 adds the channel tables).

## 1. Telegram

1. In Telegram, talk to **@BotFather**: `/newbot`, choose a name, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Put any random string in `TELEGRAM_WEBHOOK_SECRET`.
3. Restart the API, then register the webhook (one command, replace the three values):

   ```
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=<PUBLIC_BASE_URL>/webhooks/telegram" -d "secret_token=<SECRET>" -d "allowed_updates=[\"message\",\"callback_query\"]"
   ```
4. Open the bot, press Start, and go through the questions. `/v1/channels` should show one Telegram subscriber.

## 2. WhatsApp test number

1. developers.facebook.com: create an app of type Business, add the **WhatsApp** product. The API setup page gives a
   test number, a **phone number id** and a temporary **access token**; add up to five recipient numbers there.
2. `.env`: `WHATSAPP_PHONE_ID`, `WHATSAPP_TOKEN`, `WHATSAPP_APP_SECRET` (App settings, Basic, App secret) and any
   random string as `WHATSAPP_VERIFY_TOKEN`. Restart the API.
3. WhatsApp, Configuration, Webhook: callback URL `<PUBLIC_BASE_URL>/webhooks/whatsapp`, verify token = your
   `WHATSAPP_VERIFY_TOKEN`; subscribe to the **messages** field.
4. From a verified phone, send "hi" to the test number and finish the questions. Alerts now reach that phone for
   24 hours after its last message, with no template.

The temporary token expires in a day. For anything longer, create a system user in Business settings and generate
a permanent token with `whatsapp_business_messaging` permission.

## 3. WhatsApp template (alerts to people who have not written in the last 24 hours)

Submit these in WhatsApp Manager, Message templates. Category **Utility**; language English.

**`corridor_alert_v1`**

```
Body:    {{1}}: {{2}}. It touches your {{5}} trip, until about {{3}}. Suggested way round: via {{4}}. Suggested only: follow signs and police on the road.
Samples: Waterlogging | Jubilee Hills Check Post to Madhapur | 9:40 am | Road No 36, Road No 45 | morning
Button:  Visit website, "Open in Google Maps", dynamic URL  <PUBLIC_BASE_URL>/r/{{1}}   (sample: abc123)
Footer:  Reply STOP to unsubscribe
```

**`corridor_cleared_v1`**

```
Body:    Cleared: {{1}} {{2}}. Your usual route is open again.
Samples: Waterlogging | at Tolichowki Underpass
```

When Meta approves them, set `WHATSAPP_ALERT_TEMPLATE=corridor_alert_v1` and
`WHATSAPP_CLEARED_TEMPLATE=corridor_cleared_v1`. If Meta files the alert under Marketing, do not use it: reword and
resubmit, because the cost is about seven times higher and the plan's numbers assume Utility.

Moving from the test number to your own number needs Meta Business verification; start it now, it is the slow step.

## 4. SMS fallback

`SMS_PROVIDER=log` records what would be sent (the console shows "Recording only"), which is enough to watch the
fallback logic work. Sending needs, in order: DLT entity registration, a sender id, an approved template with one
variable for the message text and the short-link domain whitelisted, then `SMS_PROVIDER=msg91`, `MSG91_AUTHKEY` and
`MSG91_TEMPLATE_ID`. The MSG91 adapter follows their documented flow call but has not been run against a live
account; check the first send in their dashboard.

## What happens once it is on

- A person says hi, accepts the notice, picks a vehicle, shares a start pin and an end pin, and picks travel times.
  DetourAI routes the trip, keeps the corridor cells and the encrypted pins, and says it is set.
- When a zone becomes active, the worker finds corridors it touches (cells, direction, vehicle, travel window, day),
  sends one message per person with the cause, the stretch, how long, the way round and a Google Maps button.
  At most four alerts per person in 20 hours, one per zone.
- When that zone is refuted or expires, the same people get one cleared message.
- A WhatsApp alert with no delivery receipt after 60 seconds goes out again as an SMS that can be followed on its own.
- STOP, START, SHOW, RESET, DELETE and HELP work at any point. DELETE removes the subscriber, corridors, alerts and outbox rows.

`/v1/channels` and the console's Feeds tab show what is switched on, subscriber counts, and the last day's delivery.
