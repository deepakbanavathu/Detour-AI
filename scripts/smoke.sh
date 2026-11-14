#!/usr/bin/env bash
# 2W vs 4W_SUV between two Hyderabad points, then a pasted advisory resolved (dry run).
set -euo pipefail
API=${API:-http://127.0.0.1:8080}
FROM=${FROM:-17.4486,78.3908}
TO=${TO:-17.4126,78.4480}

curl -fsS "$API/healthz"; echo
curl -fsS "$API/v1/route/compare?from=$FROM&to=$TO&profiles=2W,4W_SUV" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for k,v in d.items():
    if "error" in v: print(k, "ERROR", v["error"]); continue
    print(f"{k:8s} {v["distance_m"]/1000:6.2f} km  {v["duration_s"]/60:5.1f} min  snap {v["snap_from"]["dist_m"]} / {v["snap_to"]["dist_m"]} m  only-this-profile edges: {len(v.get("edges_only_this_profile",[]))}")
    print("         ", v["maps_url"])
'
curl -fsS -X POST "$API/v1/advisories/paste" -H "Content-Type: application/json" \
  -d '{"text":"Traffic diverted from Jubilee Hills Check Post towards Madhapur via Road No 36 due to waterlogging","commit":false}' \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("advisory ok" if d.get("ok") else "advisory NOT resolved:", d.get("reason",""))
if d.get("ok"):
    print(" ", d["ir"]["event_type"], d["ir"]["cause"], [ (a["text"], a["canonical"], a["feature_class"]) for a in d["anchors"] ])
    print("  zone method", d["zone"]["method"], "cells", d["zone"]["h3_cells"])
'
