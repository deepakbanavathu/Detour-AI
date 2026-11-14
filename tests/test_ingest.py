"""Parser tests for the pollers. No network, no database: fixtures follow each provider's documented payload shape."""
from datetime import datetime, timezone

from app.ingest import openmeteo, rss, tomtom, xapi
from app.ingest.text import advisory_sentences, html_to_text, relevant

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Hyderabad</title>
<item><title>Traffic curbs announced for Sadar Utsav Mela in Hyderabad</title><link>https://example.org/a1</link><guid>a1</guid>
<pubDate>Tue, 10 Nov 2026 14:05:00 +0530</pubDate>
<description><![CDATA[<p>Traffic from Kachiguda X Roads towards Post Office Junction will be diverted at Lingampally X Roads towards Tourist Junction via Barkatpura Chaman.</p><p>Commuters have been advised to plan their journeys.</p>]]></description></item>
<item><title>Gold prices dip</title><link>https://example.org/a2</link><guid>a2</guid><description>Markets.</description></item>
</channel></rss>"""


def test_rss_parse_and_relevance():
    items = rss.parse_feed(FEED)
    assert [i.ext_id for i in items] == ["a1", "a2"]
    assert items[0].ts == datetime(2026, 11, 10, 8, 35, tzinfo=timezone.utc)
    assert relevant(items[0].title + items[0].text) and not relevant(items[1].title + items[1].text)
    sents = advisory_sentences(items[0].text)
    assert len(sents) == 1 and sents[0].startswith("Traffic from Kachiguda")


def test_html_to_text_drops_chrome():
    t = html_to_text("<nav>Home</nav><p>Road closed at X.</p><script>var a=1</script><p>Use Y.</p>")
    assert t.splitlines() == ["Road closed at X.", "Use Y."]


def test_x_timeline():
    posts, newest = xapi.parse_timeline({"data": [{"id": "190", "created_at": "2026-11-14T03:10:00.000Z", "text": "Water logging at Tolichowki underpass."}],
                                         "meta": {"newest_id": "190", "result_count": 1}})
    assert newest == "190" and posts[0].ts.tzinfo is not None and "Tolichowki" in posts[0].text
    assert xapi.parse_timeline({"meta": {"result_count": 0}}) == ([], None)


def test_tomtom_incidents():
    payload = {"incidents": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[78.40, 17.43], [78.41, 17.432]]},
         "properties": {"id": "abc", "iconCategory": 8, "events": [{"description": "Closed", "code": 401}],
                        "startTime": "2026-11-14T02:00:00Z", "endTime": None, "from": "Jubilee Hills Check Post", "to": "Road No 36"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[78.45, 17.40], [78.46, 17.40]]},
         "properties": {"id": "jam", "iconCategory": 6, "events": [{"description": "Queuing traffic"}], "startTime": "2026-11-14T02:00:00Z"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [78.39, 17.39]},
         "properties": {"id": "fl", "iconCategory": 11, "events": [{"description": "Flooding"}], "startTime": "2026-11-14T02:30:00Z", "endTime": "2026-11-14T06:00:00Z"}}]}
    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    inc = tomtom.parse_incidents(payload, now)
    assert [i.ext_id for i in inc] == ["abc", "fl"]                   # the jam is not a zone
    assert inc[0].penalty == 1.0 and inc[0].event_type == "CLOSURE" and inc[1].cause == "WATERLOGGING"
    assert tomtom.valid_to(inc[0], now, 1200) == datetime(2026, 11, 14, 3, 40, tzinfo=timezone.utc)
    assert tomtom.valid_to(inc[1], now, 1200) == datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)


def test_open_meteo_aggregate_and_grid():
    times = [f"2026-11-{d:02d}T{h:02d}:00" for d in (11, 12, 13, 14) for h in range(24)]
    rain = [0.0] * len(times)
    k = times.index("2026-11-14T03:00")
    rain[k], rain[k - 1], rain[k - 2], rain[k - 3], rain[k - 30] = 4.0, 6.0, 2.0, 1.0, 10.0
    pts = openmeteo.aggregate([{"latitude": 17.425, "longitude": 78.425, "hourly": {"time": times, "precipitation": rain}}],
                              datetime(2026, 11, 14, 3, 20, tzinfo=timezone.utc))
    p = pts[0]
    assert (p.i_mm_h, p.acc_1h, p.acc_3h) == (4.0, 6.0, 9.0)
    assert 9.0 + 10.0 * 0.85 ** (30 / 24) - 0.6 < p.api_72h < 9.0 + 10.0
    g = openmeteo.grid((78.20, 17.20, 78.75, 17.65), 0.05)
    assert len(g) == 11 * 9 and g[0] == (17.225, 78.225)
