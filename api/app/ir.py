"""Advisory text -> IR (blueprint 1.4).

Two paths with one contract:
  * regex: the template most police posts follow; instant, no model.
  * llm:   an OpenAI-compatible endpoint (vLLM / llama.cpp serving Qwen2.5-7B) with the JSON schema
           enforced; used when LLM_BASE_URL is set and the regex path finds nothing.
The IR never carries coordinates; zones.py turns anchors into geometry deterministically.
"""
from __future__ import annotations

import json
import re

import httpx

from .config import LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_S

EVENT_TYPES = ["DIVERSION", "CLOSURE", "WATERLOGGING", "VIP_MOVEMENT", "GATE_CLOSED", "ACCIDENT", "PROCESSION", "LIFTED"]
CAUSES = ["WATERLOGGING", "VIP", "ROADWORK", "ACCIDENT", "PROCESSION", "TREE_FALL", "GATE", "UNKNOWN"]
ROLES = ["AT", "FROM", "TO", "VIA", "BETWEEN_A", "BETWEEN_B"]
SCOPES = ["ALL", "2W", "3W", "4W", "LCV", "HCV"]

IR_SCHEMA = {
    "type": "object",
    "required": ["event_type", "anchors", "cause", "vehicle_scope", "confidence"],
    "additionalProperties": False,
    "properties": {
        "event_type": {"enum": EVENT_TYPES},
        "anchors": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
            "type": "object", "required": ["text", "role"], "additionalProperties": False,
            "properties": {"text": {"type": "string", "maxLength": 60}, "role": {"enum": ROLES}}}},
        "cause": {"enum": CAUSES},
        "vehicle_scope": {"type": "array", "items": {"enum": SCOPES}},
        "time_window": {"type": ["object", "null"], "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_CAUSE_WORDS = [
    ("WATERLOGGING", r"water ?logg|waterlogged|flood|inundat|submerg|rain water|stagnant water"),
    ("VIP", r"\bvip\b|vvip|convoy|movement of|cm\b|governor|president|prime minister|minister"),
    ("ROADWORK", r"road work|roadwork|repair|construction|relaying|metro work|pipeline|digging|re-?carpet"),
    ("ACCIDENT", r"accident|collision|overturn|breakdown|broke down|mishap"),
    ("PROCESSION", r"procession|rally|yatra|jatara|shobha|ganesh|immersion|festival|dharna|protest|march"),
    ("TREE_FALL", r"tree fall|tree fell|uprooted|fallen tree|branch"),
    ("GATE", r"\bgate\b|gated|colony closed|layout closed"),
]

_LIFTED = re.compile(r"\b(restor|normal|cleared|lifted|resumed|opened|reopened|free flow)\w*", re.I)
_SCOPE = [("HCV", r"heavy vehicles?|hcv|lorr|trucks?"), ("LCV", r"lcv|goods vehicles?"), ("BUS", r"\bbus(es)?\b"),
          ("2W", r"two ?wheelers?|2w|bikes?|scooters?"), ("4W", r"four ?wheelers?|4w|cars?")]

_ANCHOR = r"[a-z0-9][a-z0-9 .\-/]{2,45}?"
# an anchor ends at punctuation, end of text, or before the next connective keyword
_END = r"(?=[.,;:()\n]|$|\s+(?:due to|because of|owing to|on account of|in view of|from|between|till|until|upto|up to|for|and|towards?|to|via|through|thru|after|since)\b)"
_DIVERT = re.compile(
    rf"(?:(?:vehicles?|traffic)\s+)?(?:will be\s+|is\s+|are\s+|being\s+)?(?:diverted|divert|closed|restricted|stopped|not allowed)\s+"
    rf"(?:(?:from|at|near|on)\s+(?P<from>{_ANCHOR}){_END})?\s*"
    rf"(?:(?:towards?|to|upto|up to)\s+(?P<to>{_ANCHOR}){_END})?\s*"
    rf"(?:(?:via|through|thru)\s+(?P<via>{_ANCHOR}){_END})?\s*"
    rf"(?:(?:due to|because of|owing to|on account of|in view of)\s+(?P<cause>[a-z ]{{3,40}}?))?"
    rf"(?=[.,;:()\n]|$|\s+(?:from|between|till|until|for)\b)", re.I)
# The full police form: "Traffic from A towards B will be diverted at C towards D via E".
# The road that is shut is C -> B; D and E describe the detour and never shape the zone.
_FULL = re.compile(
    rf"(?:vehicles?|traffic|commuters)\s+(?:coming\s+|proceeding\s+|going\s+)?from\s+(?P<a>{_ANCHOR})\s+(?:towards?|to)\s+(?P<b>{_ANCHOR})\s+"
    rf"(?:will be\s+|shall be\s+|is\s+|are\s+|being\s+)?(?:diverted|divert)\s+at\s+(?P<c>{_ANCHOR}){_END}"
    rf"(?:\s+(?:towards?|to)\s+(?P<d>{_ANCHOR}){_END})?(?:\s+(?:via|through|thru)\s+(?P<e>{_ANCHOR}){_END})?", re.I)
_AT = re.compile(
    rf"(?:water ?logg\w*|flood\w*|inundat\w*|accident|tree fall\w*|closed|blocked|jam|congestion|slow moving|heavy traffic|"
    rf"restored|normal|cleared|lifted|resumed|reopened|opened|free flow)\s+"
    rf"(?:at|near|on|in front of|under|below|around)\s+(?P<at>{_ANCHOR}){_END}", re.I)
_BETWEEN = re.compile(rf"between\s+(?P<a>{_ANCHOR})\s+(?:and|&|to)\s+(?P<b>{_ANCHOR}){_END}", re.I)
_TIME = re.compile(r"(?:from|between)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|hrs)?)\s*(?:to|-|till|and)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|hrs)?)", re.I)

_STOPWORDS = ("the ", "a ", "an ")


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip(" .,-/")
    for w in _STOPWORDS:
        if s.startswith(w):
            s = s[len(w):]
    return s or None


def _cause(norm: str) -> str:
    for cause, pat in _CAUSE_WORDS:
        if re.search(pat, norm, re.I):
            return cause
    return "UNKNOWN"


def _scope(norm: str) -> list[str]:
    found = [s for s, pat in _SCOPE if re.search(pat, norm, re.I)]
    return found or ["ALL"]


def regex_ir(norm_text: str) -> dict | None:
    """Template extraction on normalized text. Returns an IR or None if no anchor is found."""
    anchors: list[dict] = []
    detour = None
    full = _FULL.search(norm_text)
    m = None if full else _DIVERT.search(norm_text)
    if full:
        anchors = [{"text": _clean(full.group("c")), "role": "FROM"}, {"text": _clean(full.group("b")), "role": "TO"}]
        detour = {"towards": _clean(full.group("d")), "via": _clean(full.group("e")), "origin": _clean(full.group("a"))}
    if m:
        for role, key in (("FROM", "from"), ("TO", "to"), ("VIA", "via")):
            v = _clean(m.group(key))
            if v:
                anchors.append({"text": v, "role": role})
    if not anchors:
        m2 = _BETWEEN.search(norm_text)
        if m2:
            anchors += [{"text": _clean(m2.group("a")), "role": "BETWEEN_A"}, {"text": _clean(m2.group("b")), "role": "BETWEEN_B"}]
    if not anchors:
        m3 = _AT.search(norm_text)
        if m3 and _clean(m3.group("at")):
            anchors.append({"text": _clean(m3.group("at")), "role": "AT"})
    if not anchors:
        return None

    cause = _cause(norm_text)
    if m and m.group("cause"):
        cause = _cause(m.group("cause")) if _cause(m.group("cause")) != "UNKNOWN" else cause
    lifted = bool(_LIFTED.search(norm_text)) and not re.search(r"diverted|closed|restricted|water ?logg", norm_text)
    if lifted:
        event = "LIFTED"
    elif cause == "WATERLOGGING" and not m:
        event = "WATERLOGGING"
    elif cause == "VIP":
        event = "VIP_MOVEMENT"
    elif cause == "GATE":
        event = "GATE_CLOSED"
    elif full or (m and re.search(r"diverted|divert", m.group(0))):
        event = "DIVERSION"
    elif m:
        event = "CLOSURE"
    elif cause == "ACCIDENT":
        event = "ACCIDENT"
    elif cause == "PROCESSION":
        event = "PROCESSION"
    else:
        event = "CLOSURE"

    tw = None
    t = _TIME.search(norm_text)
    if t:
        tw = {"start": t.group(1).strip(), "end": t.group(2).strip()}
    n_roles = len({a["role"] for a in anchors})
    confidence = min(0.95, 0.55 + 0.12 * n_roles + (0.08 if cause != "UNKNOWN" else 0))
    out = {"event_type": event, "anchors": anchors, "cause": cause, "vehicle_scope": _scope(norm_text),
           "time_window": tw, "confidence": round(confidence, 2), "model_ver": "regex-1"}
    if detour:
        out["detour"] = detour
        out["confidence"] = min(0.95, out["confidence"] + 0.05)      # the full form leaves the least to guess
    return out


_SYSTEM = (
    "You extract a traffic advisory into JSON. Output only JSON matching the schema. "
    "Anchors are place names exactly as written in the text; never invent coordinates or places. "
    "Roles: FROM/TO/VIA for a diversion, AT for a single location, BETWEEN_A/BETWEEN_B for a stretch. "
    "If the text says traffic is restored or cleared, event_type is LIFTED."
)


def llm_ir(raw_text: str) -> dict | None:
    """Constrained JSON extraction on an OpenAI-compatible server. Returns None when LLM_BASE_URL is unset or the call fails."""
    if not LLM_BASE_URL:
        return None
    body = {
        "model": LLM_MODEL,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": raw_text[:1500]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "advisory_ir", "schema": IR_SCHEMA, "strict": True}},
        "guided_json": IR_SCHEMA,                       # vLLM extra; ignored by servers that do not know it
    }
    try:
        r = httpx.post(f"{LLM_BASE_URL.rstrip('/')}/v1/chat/completions", json=body, timeout=LLM_TIMEOUT_S)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        ir = json.loads(content)
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
        return None
    if not validate_ir(ir):
        return None
    ir["model_ver"] = LLM_MODEL
    return ir


def validate_ir(ir: dict) -> bool:
    try:
        if ir["event_type"] not in EVENT_TYPES or ir["cause"] not in CAUSES:
            return False
        if not ir["anchors"] or any(a["role"] not in ROLES or not a["text"].strip() for a in ir["anchors"]):
            return False
        if any(s not in SCOPES for s in ir.get("vehicle_scope") or ["ALL"]):
            return False
        return 0 <= float(ir["confidence"]) <= 1
    except (KeyError, TypeError, ValueError):
        return False


def extract(raw_text: str, norm_text: str) -> dict | None:
    """Regex first; LLM when the regex finds no anchor."""
    return regex_ir(norm_text) or llm_ir(raw_text)
