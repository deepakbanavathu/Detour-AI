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
