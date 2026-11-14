#!/usr/bin/env python3
"""Parse every .sql file and every SQL block embedded in the API with PostgreSQL's own parser (no database needed)."""
import inspect
import re
import sys
from pathlib import Path

import pglast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
bad = n = 0

for f in sorted(list((ROOT / "db" / "migrations").glob("*.sql")) + list((ROOT / "import").glob("*.sql"))):
    src = "\n".join(line for line in f.read_text().splitlines() if not line.startswith("\\"))
    try:
        n += len(pglast.parse_sql(src))
    except Exception as e:                                   # noqa: BLE001
        bad += 1
        print(f"FAIL {f.relative_to(ROOT)}: {e}")


def norm(q: str) -> str:
    q = q.replace("%%", "%")
    q = re.sub(r"%\((\w+)\)s", "NULL", q).replace("%s", "NULL")
    return q.replace("{', '.join(sets)}", "updated_at = now()").replace("{col}", "home_enc")


from app import brief, gazetteer, main, packs, pipeline, profiles, routing, worker, zones  # noqa: E402
from app.channels import fanout, store  # noqa: E402

for mod in (brief, gazetteer, main, packs, pipeline, worker, zones, fanout, store):
    src = inspect.getsource(mod)
    blocks = [m.group(1) for m in re.finditer(r'"""\n(\s+(?:SELECT|INSERT|UPDATE|WITH)\b.*?)"""', src, re.S)]
    blocks += [m.group(1) for m in re.finditer(r'execute\(\s*f?"((?:SELECT|UPDATE|INSERT|WITH)[^"\n]+)"\s*[,)]', src)]
    for q in blocks:
        n += 1
        try:
            pglast.parse_sql(norm(q))
        except Exception as e:                               # noqa: BLE001
            bad += 1
            print(f"FAIL {mod.__name__}: {e} | {q.strip()[:100]}")

p = profiles.Profile(2, "4W_HATCH", 1.7, 1.55, 0.17, False, 90)
for sql in (routing._edge_sql(p, (78.3, 17.4, 78.5, 17.5)), routing._edge_sql(p, (78.3, 17.4, 78.5, 17.5), False, [1, 2], 5.0), gazetteer._RESOLVE_SQL, fanout._MATCH):
    n += 1
    try:
        pglast.parse_sql(norm(sql))
    except Exception as e:                                   # noqa: BLE001
        bad += 1
        print(f"FAIL generated SQL: {e}")

print(f"{n} SQL statements parsed, {bad} failures")
sys.exit(1 if bad else 0)
