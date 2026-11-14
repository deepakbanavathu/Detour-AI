import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://detour:detour@127.0.0.1:5432/detour")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")
CITY_ID = int(os.environ.get("CITY_ID", "1"))

# Routing search window: bbox around origin/destination, expanded by this fraction of the
# straight-line distance and never less than MIN_PAD_DEG (about 2 km).
BBOX_PAD_FRACTION = 0.35
MIN_PAD_DEG = 0.02

# Weighting constants (blueprint Module 2.5)
ZONE_SOFT_MULT = 8.0
SUBMERGE_MULT = 4.0
UNKNOWN_GATE_COST_S = 120.0
FILTER_SPEED_CAP_KMH = 25.0

# Optional LLM path for the IR stage: an OpenAI-compatible server (vLLM / llama.cpp) on the home box.
# Leave LLM_BASE_URL empty to run regex-only.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5-7b-instruct-q4")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "20"))

# Routing retries: widen the search window this many times when no path is found
ROUTE_BBOX_RETRIES = 2

# Commuter channel. Every value is optional: with none set, the channel code runs but sends nothing.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")      # e.g. https://d.example.in ; used for short links
PII_KEY = os.environ.get("PII_KEY", "dev-only-key-change-me")            # pgcrypto key for phone numbers and pins
PII_PEPPER = os.environ.get("PII_PEPPER", "dev-only-pepper-change-me")   # HMAC pepper for channel ids
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v25.0")
WHATSAPP_ALERT_TEMPLATE = os.environ.get("WHATSAPP_ALERT_TEMPLATE", "")      # approved utility template name
WHATSAPP_CLEARED_TEMPLATE = os.environ.get("WHATSAPP_CLEARED_TEMPLATE", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "")                            # '' = no fallback, 'log' = record only, 'msg91'
MSG91_AUTHKEY = os.environ.get("MSG91_AUTHKEY", "")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "")
SMS_FALLBACK_AFTER_S = int(os.environ.get("SMS_FALLBACK_AFTER_S", "60"))
ALERTS_PER_DAY = int(os.environ.get("ALERTS_PER_DAY", "4"))                  # 2 per window, two windows
