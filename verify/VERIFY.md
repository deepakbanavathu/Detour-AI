# Verifying DetourAI

This proves the stack's output is correct, judged against things outside its own code: your trips, the road itself,
an independent router (OSRM), OpenStreetMap's own quality checker (Osmose), and a person who knows the roads. One
command runs it on Windows with Docker Desktop and writes a dated JSON report with pass or fail per criterion.

## 1. Pass criteria

All thresholds live in `verify/criteria.json`, written before any check ran. `estimate` marks a number chosen by
judgement; change one only with a written reason. The rest are exact properties.

| Group | Id | Passes when | Kind |
| --- | --- | --- | --- |
| Graph | G01–G05 | no invalid lines, no single-point edges, stored length equals geodesic length within 1 m, every edge starts at its src node and ends at its dst node within 0.5 m, no duplicate edges | exact |
| | G06, G09 | under 0.1% of edges shorter than 0.5 m; under 0.1% of OSM ways skipped by the import | estimate |
| | G07, G08 | every edge has its dimension and dynamic rows; every named edge links to its gazetteer road | exact |
| | G10, G11 | largest connected piece holds ≥ 99% of arterial length and ≥ 95% of all length | estimate |
| | G12 | a car can get from anywhere to anywhere on ≥ 95% of car-usable length | estimate |
| | G13, G14 | arterial dead ends ≤ 0.5% and one-way traps ≤ 0.2% of nodes, ignoring 500 m at the extract border | estimate |
| Vehicles | V01, V02 | where OSM access allows both, no edge open to a car but closed to a scooter, none open to a truck but closed to a car | exact |
| | V03–V05 | the most specific OSM access key present (for example `motorcycle`, then `motor_vehicle`, `vehicle`, `access`) set to `no` closes the edge to that vehicle; `private` stays open, because gated layouts are handled by the layout rule and the gate cost | exact |
| | V06 | a scooter is never more than 15% slower than a car on the same pair (the 70 vs 90 km/h cap) | estimate |
| | V07, V08 | with your tape width, who-fits matches who you saw pass; no car route through a gate a car cannot pass | exact |
| Routing | R01 | every pair routes for every vehicle you listed | exact |
| | R02 | ≥ 95% of pins snap within 150 m of a usable road | estimate |
| | R03, R04 | car route length within 15% of OSRM's for ≥ 85% of pairs, and no pair more than 50% off | estimate |
| | R05 | for ≥ 70% of pairs, at least half the roads you named appear in the route | estimate |
| | R06 | for ≥ 80% of pairs, DetourAI's time is 0.35 to 1.2 times your usual time (it uses free-flow speeds) | estimate |
| | R07 | for ≥ 70% of recorded trips, ≥ 70% of DetourAI's route lies within 30 m of what you drove | estimate |
| GPX | T01 | ≥ 95% of your recorded positions lie within 25 m of a road in the graph | estimate |
| | T02, T03 | ≥ 98% of positions where you drove (or rode) are on edges open to that vehicle | estimate |
| | T04 | ≤ 1% of car positions run against a mapped one-way | estimate |
| Advisories | A01, A02 | ≥ 85% parsed; of those, ≥ 75% with every place resolved | estimate |
| | A03 | ≥ 70% of advisories that should make a zone do | estimate |
| | A04 | ≥ 80% of placed zones cover ≥ 60% of the stretch you marked, with ≤ 60% of the zone farther than 150 m from it | estimate |
| | A05, A06 | ≤ 5% of placed zones in the wrong place (< 10% coverage); ≤ 10% of no-action or cleared texts make a zone | estimate |
| Latency | L01–L03 | route p50 ≤ 500 ms, p95 ≤ 2 s; pgRouting call mean ≤ 1.5 s (pg_stat_statements) | estimate |
| Robustness | I01, I02 | every migration re-applied and committed again succeeds and changes nothing | exact |
| | I03–I05 | rebuilding the network and gazetteer gives the same fingerprint and keeps hand-mapped gates, sinks and alert history | exact |
| | I06, I07 | after the worker is killed mid-poll and restarted, every source is polled within 120 s and nothing is duplicated | exact |
| Feed | F01 | every entry in the Waze-format feed has its required fields, respects the specification's limits, has a unique id, and at least 80% of its line lies on a road with the street name it gives | exact |
| Feed | F02 | no feed entry comes from a zone built on licensed traffic data (TomTom) | exact |
| Crowd | C01, C02 | no crowd report reaches the feed unconfirmed; every report's zone lies within 40 m of its pin | exact |
| External | X03 | from your timing sheet, DetourAI had the zone no later than Waze or Google Maps showed it, for ≥ 50% of zones | estimate |
| External | X01, X02 | blind reviewer picks DetourAI's route or "both fine" for ≥ 70% of trips (estimate); never marks a DetourAI road impassable (exact) | mixed |

`verify/known_issues.json` lists failures already found and understood. They report as `known_issue`, still print in
the summary, and do not fail the run. It is empty today: the three found by the first run (access tags ignored,
scooters barred from narrow two-way gaps, re-import emptying hand-mapped gates and alert history) were fixed in v0.9,
and their checks now guard the fixes.

## 2. Inputs you collect

Everything goes in `verify\inputs\`, which git ignores: it holds your home pins and trips. `verify.bat` copies the
templates from `verify\templates\` on first run; replace the example rows.

**Pairs, `pairs.csv`, 20 to 30 rows.** Trips you make or know well, spread across the city and across road types.

```
id,from_label,from_lat,from_lng,to_label,to_lat,to_lng,vehicle,usual_minutes,usual_via,gpx
p01,Home,17.39900,78.52600,Uppal X Roads,17.40100,78.55900,both,15,Uppal Main Road,p01_2w.gpx
```

- Coordinates: on openstreetmap.org right-click the spot and choose "Show address", or long-press in Google Maps; five decimals.
- `vehicle`: `2w`, `car` or `both`. `usual_minutes`: what it takes at the time you normally go.
- `usual_via`: the roads you take, separated by `;`, as they are signed or commonly called. `gpx`: optional file name.

**GPX tracks, `gpx\*.gpx`, 5 to 10 trips.** Record with *GPS Logger* (BasicAirData, free) or *OsmAnd* (trip
recording). Logging every 1 second, no distance filter, accuracy filter 20 m. Wait for a GPS fix before you set off.
Name each file by vehicle: `p01_2w.gpx`, `p07_car.gpx`, `p09_suv.gpx`. Record at least two on a two-wheeler and two in
a car, and at least two through narrow lanes or gated colonies, because that is where the vehicle rules matter.

**Advisories, `advisories.geojson`, 50 features.** Real posts from @HYDTP, @CYBTRAFFIC, @RachakondaTP or the news
stories quoting them, from the last monsoon. Include about 10 that should make no zone (enforcement drives, greetings)
and about 5 "traffic restored" posts. Open the file at geojson.io; for each post that shut a road, draw a line along
the stretch that was actually shut, then set the properties:

```
id        a01
text      the post's text as written
source    @HYDTP
posted_at 2026-08-14T08:10:00+05:30
url       link to the post
expect    zone | none | lifted
anchors   the places it names, separated by ;   (optional, sharpens the diagnosis)
```

The texts stay on your machine and are used only to score, never to train anything.

**Gates, `gates.csv`, 3 or 4 rows.** Gated-colony gates, bollard gaps or lanes a scooter uses and a car cannot.

```
id,lat,lng,width_m,scooter_passes,car_passes,a_lat,a_lng,b_lat,b_lng,notes
g01,17.40120,78.52310,1.45,yes,no,17.40050,78.52200,17.40200,78.52420,open 6am-10pm, photo IMG_2231
```

Measure the narrowest clear width between fixed obstacles with a tape and take a photo. `scooter_passes` and
`car_passes` are what you saw. `a_` and `b_` are points on public roads on each side, 100 to 300 m away.

## 3. Running it

Once, if not done yet (from the repo folder, in Command Prompt):

```
copy .env.example .env
curl -L -o data\southern-zone-latest.osm.pbf https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf
docker compose -p detourai up -d --build db
docker compose -p detourai exec db bash /migrate.sh
docker run --rm --network detourai_default -v "%cd%":/work -w /work -e DATABASE_URL=postgresql://detour:detour@db:5432/detour debian:trixie bash -c "apt-get update -qq && apt-get install -y -qq osmium-tool osm2pgsql postgresql-client >/dev/null && bash import/import_osm.sh data/southern-zone-latest.osm.pbf"
```

Then every time:

```
verify\verify.bat
```

It starts the stack, builds OSRM from the same file and area (once), runs every check, kills and restarts the worker,
fetches Osmose, and prints a summary. It prints `FAIL` or `KNOWN` lines with a likely cause, then the report path under
`verify\reports\`. The first run on the full city takes about 30 to 60 minutes; `verify.bat --skip-reimport` skips the
network rebuild. If you imported with your own `BBOX`, set the same `BBOX` in `.env` so OSRM covers the same area.

Send me the summary and the report file.

## 4. External checks

**Osmose** (OpenStreetMap France's quality checker) runs in step 5. The report's `external.osmose` lists level 1 and 2
issues by item number for your area. Use it to separate bad map data from bad processing: a dead end or trap that
Osmose also flags is the map's fault.

**Blind review** by someone who rides or drives those roads daily, such as a delivery rider or dispatcher.

```
verify\verify.bat review
```

This writes `verify\review\<date>\review.html`, with 10 trips each showing two unlabeled routes, A and B. One is
DetourAI's and one is OSRM's, in random order. Send only `review.html`; keep `KEY-do-not-share.json`. The reviewer picks
a route per trip, ticks any road a vehicle cannot use, and presses Download answers. Then score it:

```
verify\verify.bat score verify\review\<date>\review-answers.csv verify\review\<date>\KEY-do-not-share.json
```

**Lead time against Waze and Google Maps.** For about 20 zones over a few days, note in `verify\inputs\lead_time.csv`
when DetourAI created the zone (the console shows it) and when you first saw the same closure on waze.com/live-map and
in Google Maps; leave a cell empty if an app never showed it within an hour. Look by hand: nothing reads Waze by script.

```
verify\verify.bat lead
```

The report then holds how many zones each app showed at all and the median minutes DetourAI was ahead.

**CI on clean machines.** `.github/workflows/verify.yml` runs the same script on GitHub's servers every Monday, on
pushes that touch the pipeline, and on demand from the Actions tab.

- **synthetic:** a small map with eight planted faults. The job fails if the verifier misses one or flags a clean check.
- **extract:** an 8 × 7 km piece of real Hyderabad, from Banjara Hills to Madhapur, with 15 fixed random pairs. The job fails on any failing criterion.

Reports are attached to each run as artifacts.

## 5. Telling the cause apart

Every failing criterion in the report carries `likely_cause` and `evidence`. The script works out the cause where it
can; the rest of this table is how to confirm it.

| Failure | Bad OSM data if… | The import if… | Routing weights if… | The advisory parser if… |
| --- | --- | --- | --- | --- |
| G04 endpoint mismatch | (never) | always: a split or a one-way reversal is wrong | | |
| G05 duplicate edges | the duplicates have different `ways` (two OSM ways drawn on one road) | the same way id appears twice | | |
| G10–G14 islands, dead ends, traps | Osmose flags the same spot, or the road looks unfinished on openstreetmap.org | G04 also fails, or the island begins exactly at a way split | the trap disappears for a narrower vehicle (an estimated width shut it) | |
| T01 positions with no road | `in_osm_tables` is false and the road is missing on openstreetmap.org | `in_osm_tables` is true: loaded but given no edge | | |
| T02/T03 drove where the graph forbids | `width_src` is `CLASS_DEFAULT` or `LANES_INFER`: no `width=` in OSM; add it or measure it | `width_src` is `OSM_TAG` and the mask rule rejects a real width | | |
| T04 car against a one-way | G04 passes, and the street really is two-way: fix the tag in OSM | G04 fails | | |
| R03/R04 far from OSRM | | `osrm_points_not_on_detour_graph` is high | `osrm_points_car_excluded` is high (widths), or `at_gates`, or neither: different speeds | |
| R05 your roads not in the route | your road has a different name in OSM | | the road exists, is allowed, and is avoided | |
| A01 not parsed | | | | no sentence shape matched (`ir.py`) |
| A02 not resolved | the place is missing from OSM, with no candidates | | | the gazetteer has candidates but below the threshold |
| A04/A05 wrong or partial zone | | | | anchors right but the zone is elsewhere: `zones.py`, or the `from_anchor_fallback` method |
| A06 false zone | | | | the parser made a zone from a no-action text |
| L01–L03 slow | | | the edge-cost query over a wide window; see `pg_stat_statements_top` | |

For T01, confirm in the raw file whether a road is in the `.pbf` at all:

```
docker run --rm -v "%cd%":/work -w /work debian:trixie bash -c "apt-get update -qq && apt-get install -y -qq osmium-tool >/dev/null && osmium extract -b LNG-0.0005,LAT-0.0005,LNG+0.0005,LAT+0.0005 data/southern-zone-latest.osm.pbf -o /tmp/x.pbf && osmium tags-filter /tmp/x.pbf w/highway -o /tmp/h.pbf && osmium cat /tmp/h.pbf -f opl"
```

Replace the bbox with real numbers around the point.

## 6. What has and has not been tested

Tested in a Linux sandbox against real PostgreSQL 16, PostGIS 3.4, pgRouting 3.6 and h3 4.1, on the synthetic map:

- the migrations, the import, the API and the worker
- `verify.py` end to end: every check, the worker kill and restart, the review pack and its scoring
- the review page in Chromium

The verifier catches the four planted data faults, confirms the three v0.9 fixes, and passes every clean check
(51 expectations in `verify/fixtures/expected.json`).

Not tested:

- **TimescaleDB:** stubbed in the sandbox.
- **Docker, Docker Desktop and `verify.bat` on Windows.**
- **OSRM:** a stub answered in OSRM's format, so the OSRM image tag and commands are untested.
- **Osmose:** no internet in the sandbox.
- **The full Hyderabad import:** size, time and real data.
- **The CI workflow:** not run.
- **Worker kill timing:** the sandbox kill was not reliably mid-poll.

Every threshold marked `estimate` is a starting point for judgement, to revisit after the first real run.
