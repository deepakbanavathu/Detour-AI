# Closure feed for Waze and Google Maps

DetourAI publishes its confident, active zones in Waze's CIFS format (Closure and Incident Feed Specification).
A traffic authority that joins **Waze for Cities** registers the feed address once; Waze then fetches it and shows
the closures and hazards to Waze and Google Maps drivers, attributed to the authority. The programme is free and
open to agencies that manage traffic or public infrastructure, so the registration is theirs to make, not DetourAI's.

## Addresses

| Address | What it is |
| --- | --- |
| `<PUBLIC_BASE_URL>/v1/feeds/cifs.json` | the feed, JSON |
| `<PUBLIC_BASE_URL>/v1/feeds/cifs.xml` | the same feed, XML |
| `<PUBLIC_BASE_URL>/v1/feeds/cifs/preview` | counts, a sample, and what was left out and why (also on the console's Feeds tab) |

## What goes into it

- Never zones built from TomTom incidents: licensed traffic data is used for routing here and is not passed on.
- Only zones that are **active** and at least **75% sure** (`CIFS_MIN_CONFIDENCE`). A crowd report stays out until
  someone confirms it; a police advisory read with normal confidence goes in at once.
- One entry **per street**, because a Waze closure may cover only one street. The line is the stretch of that street
  running along the zone, with a point about every 80 m.
- **`ROAD_CLOSED`** only for a full closure, diversion or VIP movement that has a line to close. Waterlogging,
  partial restrictions, accidents and anything known only as a point are published as **`HAZARD`** or
  **`ACCIDENT`**, which Waze shows without changing routes.
- A one-direction zone is published `ONE_DIRECTION`, with the points ordered along the affected direction of travel.
- The id (`dz<zone>-r<road>-<part>`) stays the same for the life of the zone, the start time never changes, and the
  description is under 40 characters, as the specification requires.

## Handing it to an authority

1. Give the stack a public https address and set `PUBLIC_BASE_URL`.
2. Open `/v1/feeds/cifs/preview` during a day with real advisories; check the sample against the map and read the
   skipped list. `verify\verify.bat` runs the same check (criterion F01), including that each line lies on a road
   carrying the street name it claims.
3. The authority applies at waze.com/wazeforcities with its official email, then adds the feed address in the
   Partner Hub. Waze validates the file and emails any problems.
4. Street names come from OpenStreetMap. Where Waze's name for a road differs, Waze may fail to match the closure;
   their Partner Hub reports which entries failed. Fix those names in the gazetteer aliases or in OSM.

## Not verified

Whether Waze accepts a feed an authority registers but a company operates; whether any Hyderabad commissionerate is
already a partner; how well OSM street names match Waze's map in Hyderabad. The feed has been checked against the
published specification, not submitted to Waze.
