"""UI automation: the console, the road brief and the route card, driven in a real browser on preview data."""
import pytest

pytestmark = pytest.mark.ui


# ---------------------------------------------------------------- console

def test_console_loads_in_preview_with_routes_drawn(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    assert p.is_visible("#preview")
    assert p.locator(".plate").count() == 2
    assert "Two-wheeler" in p.inner_text("#plates") and "Open in Google Maps" in p.inner_text("#plates")
    assert p.inner_text("#n-edges") == "15,20,000", "numbers use Indian grouping"
    assert p.errors == []


def test_console_place_search_suggests_and_fills(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.fill("#to", "")
    p.type("#to", "kuk", delay=30)
    p.wait_for_selector("#sg-to li")
    assert p.inner_text("#sg-to li >> nth=0").startswith("Kukatpally")
    p.keyboard.press("ArrowDown")
    p.keyboard.press("Enter")
    assert p.input_value("#to") == "Kukatpally" and p.get_attribute("#to", "data-ll") == "17.49480,78.39960"
    assert p.is_hidden("#sg-to")


def test_console_unknown_place_is_refused_with_a_clear_message(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.fill("#to", "zzzz nowhere")
    p.click("#compare")
    p.wait_for_selector("#toast:not([hidden])")
    assert "Choose the destination from the list" in p.inner_text("#toast")


def test_console_vehicle_chips_drive_the_legend_and_require_one(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click('#vehicles .chip[data-code="4W_SUV"]')
    assert "SUV" in p.inner_text("#legend")
    for code in ("2W", "4W_HATCH", "4W_SUV"):
        p.click(f'#vehicles .chip[data-code="{code}"]')
    p.click("#compare")
    p.wait_for_selector("#toast:not([hidden])")
    assert "at least one vehicle" in p.inner_text("#toast")


def test_console_advisory_reading_is_in_plain_language(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-adv")
    p.click("#check")
    p.wait_for_selector("#reading .read")
    text = p.inner_text("#reading")
    assert "What it says" in text and "Places found" in text and "Jubilee Hills Check Post" in text
    assert "Nothing saved yet" in text
    assert p.locator("#zonelist .board").count() == 1


def test_console_preview_refuses_changes(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-adv")
    p.click("#add")
    p.wait_for_selector("#toast:not([hidden])")
    assert "Preview data cannot be changed" in p.inner_text("#toast")


def test_console_roads_tab_inspects_on_map_click(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-roads")
    p.mouse.click(900, 420)
    p.wait_for_selector("#roadlist .road")
    assert "Road No 36" in p.inner_text("#roadlist") and "Fits" in p.inner_text("#roadlist")


def test_console_phone_layout_puts_the_map_first(open_page):
    p = open_page("console.html", width=390, height=800)
    p.wait_for_selector(".plate")
    m, panel = p.locator(".mapwrap").bounding_box(), p.locator(".panel").bounding_box()
    assert m["y"] < panel["y"] and m["width"] >= 380


# ---------------------------------------------------------------- road brief

def test_brief_renders_summary_zones_and_routes(open_page):
    p = open_page("brief.html")
    p.wait_for_selector(".plate")
    assert p.inner_text("#title") == "Hyderabad road brief"
    assert "2 zones to avoid" in p.inner_text("#lead")
    assert p.locator(".board").count() == 2 and p.locator(".plate").count() == 3
    assert "No way through" in p.inner_text("#routes") and "On its usual road" in p.inner_text("#routes")
    assert p.locator("#routes a").count() == 1, "only the affected route links to Maps"
    assert p.errors == []


def test_brief_copy_for_whatsapp_puts_text_on_the_clipboard(open_page):
    p = open_page("brief.html")
    p.wait_for_selector(".plate")
    p.click("#wa")
    p.wait_for_selector("#toast:not([hidden])")
    clip = p.evaluate("navigator.clipboard.readText()")
    assert clip.startswith("*Hyderabad road brief*") and "*Your routes*" in clip


def test_brief_dark_mode_keeps_text_readable(open_page):
    p = open_page("brief.html", scheme="dark")
    p.wait_for_selector(".plate")
    bg = p.evaluate("getComputedStyle(document.body).backgroundColor")
    fg = p.evaluate("getComputedStyle(document.querySelector('.lead')).color")
    assert bg == "rgb(35, 39, 43)" and fg == "rgb(236, 238, 239)"


# ---------------------------------------------------------------- route card (offline guidance)

def test_card_shows_routes_and_first_instruction(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.locator(".rt").count() == 2 and "Preview data" in p.inner_text("#state")
    assert p.inner_text("#what") == "Start on Hitec City Main Road"
    assert p.get_attribute('.rt[data-r="A"]', "aria-pressed") == "true"
    assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "nothing spills past the phone's width"
    assert p.is_visible("#go") and p.is_visible("#state")
    assert p.errors == []


def test_card_hash_selects_the_route_as_an_sms_link_would(open_page):
    p = open_page("card.html", query="#B", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.get_attribute('.rt[data-r="B"]', "aria-pressed") == "true"
    p.evaluate("location.hash = '#A'")
    p.wait_for_function("document.querySelector('.rt[data-r=\"A\"]').getAttribute('aria-pressed') === 'true'")


def test_card_guides_along_the_route_to_arrival(open_page):
    p = open_page("card.html", query="?sim=1&tick=20&step=400", width=390, height=800)
    p.wait_for_selector(".rt")
    p.wait_for_function("spoken.has('arrive')", timeout=15000)              # set when the last 40 m are reached
    assert p.inner_text("#what") == "Arrive" and p.inner_text("#dist").endswith(" m")


def test_card_position_updates_next_turn_and_distance(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.evaluate("onPosition(78.3935, 17.4469)")                    # halfway down the first leg of route A
    assert p.inner_text("#what") == "Turn right onto Road No 1 Banjara Hills"
    assert p.inner_text("#dist").endswith(" km")
    p.evaluate("onPosition(78.4300, 17.4600)")                    # well off every route
    assert p.inner_text("#what").startswith("Off Route A")


def test_card_crawl_rule_proposes_the_alternate_with_no_network(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.is_hidden("#alert")
    # five minutes ago the phone was 40 m back: under 8 km/h, and route B's turn-off is about 600 m ahead
    p.evaluate("track = [{t: Date.now() - 5 * 60e3, along: 60}]; crawlCheck(100)")
    p.wait_for_selector("#alert:not([hidden])")
    assert "Traffic is crawling. Switch to Route B at Road No 36." == p.inner_text("#alert-text")
    p.click("#alert-do")
    assert p.get_attribute('.rt[data-r="B"]', "aria-pressed") == "true" and p.is_hidden("#alert")


def test_card_crawl_rule_stays_quiet_when_moving_or_past_the_turn_off(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.evaluate("track = [{t: Date.now() - 5 * 60e3, along: 0}]; crawlCheck(3000)")        # 36 km/h
    assert p.is_hidden("#alert")
    p.evaluate("track = [{t: Date.now() - 5 * 60e3, along: 2960}]; crawlCheck(3000)")     # crawling, but B's turn-off is behind
    assert p.is_hidden("#alert")


# ---------------------------------------------------------------- console: review queue and feeds

def test_review_tab_shows_count_reasons_and_candidates(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    assert p.inner_text("#review-count") == "6" and p.is_visible("#review-count")          # 3 unplaced advisories + 3 road corrections
    p.click("#t-review")
    p.wait_for_selector("#reviewlist .road")
    cards = p.locator("#reviewlist .road")
    assert cards.count() == 3
    text = p.inner_text("#reviewlist")
    assert "“chowrasta” is not a place DetourAI knows yet" in text
    assert "“road no 1” could be more than one place" in text
    assert "No place name was found in this sentence" in text
    assert cards.nth(1).locator("[data-gaz]").count() == 3, "ambiguous item offers its candidates"
    assert cards.nth(2).locator("[data-search]").count() == 0, "an item with no anchor has nothing to search for"
    assert "Not an advisory" in cards.nth(2).inner_text() and "Skip this one" in cards.nth(0).inner_text()


def test_review_search_offers_places_and_preview_refuses_to_save(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-review")
    box = p.locator("#reviewlist .road").nth(0).locator("[data-search]")
    box.type("mehd", delay=30)
    assert p.locator("#reviewlist .road").nth(0).locator(".suggest li").count() == 0      # not in the preview list
    box.fill("")
    box.type("begum", delay=30)
    p.wait_for_selector("#reviewlist .suggest li")
    p.click("#reviewlist .suggest li")
    p.wait_for_selector("#toast:not([hidden])")
    assert "Preview data cannot be changed" in p.inner_text("#toast")


def test_feeds_tab_lists_each_source_with_its_state(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-feeds")
    p.wait_for_selector("#feedlist .road")
    assert p.locator("#feedlist .road").count() == 5
    pills = p.locator("#feedlist .pill").all_inner_texts()
    assert pills == ["Working", "Needs a key", "Working", "Working", "Failing"]
    text = p.inner_text("#feedlist")
    assert "Add X_BEARER_TOKEN to .env" in text and "404 Not Found" in text
    assert "2,376 rain readings" in text and "124 items, 9 zones" in text and "every 20 min" in text


def test_feed_form_validates_before_asking_for_admin(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-feeds")
    p.click("#feed-add")
    p.wait_for_selector("#toast:not([hidden])")
    assert "Give the feed a name" in p.inner_text("#toast")
    p.fill("#feed-name", "Siasat")
    p.fill("#feed-handle", "siasat.com/feed")
    p.click("#feed-add")
    p.wait_for_function("document.getElementById('toast').textContent.includes('full address')")
    p.select_option("#feed-kind", "X")
    assert p.inner_text("#feed-handle-l") == "Account handle" and p.input_value("#feed-cred") == "0.9"
    p.fill("#feed-handle", "not a handle!")
    p.click("#feed-add")
    p.wait_for_function("document.getElementById('toast').textContent.includes('@HYDTP')")


def test_five_tabs_fit_the_panel_and_switch(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    assert p.locator(".tab").all_inner_texts() == ["Routes", "Advisories", "Review6", "Roads", "Feeds"]
    assert p.evaluate("document.querySelector('.tabs').scrollWidth <= document.querySelector('.tabs').clientWidth")
    for tab, pane in (("#t-feeds", "#p-feeds"), ("#t-review", "#p-review"), ("#t-routes", "#p-routes")):
        p.click(tab)
        assert p.is_visible(pane)
    assert p.errors == []


def test_feeds_tab_shows_outgoing_channels_with_next_step(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-feeds")
    p.wait_for_selector("#channels .road")
    assert p.locator("#channels .road:not(#cifs)").count() == 3
    assert p.locator("#channels .road:not(#cifs) .pill").all_inner_texts() == ["No template yet", "Working", "Recording only"]
    text = p.inner_text("#channels")
    assert "212 subscribers, 9 stopped" in text and "148 delivered, 97 read" in text
    assert "set WHATSAPP_ALERT_TEMPLATE and PUBLIC_BASE_URL" in text and "SMS_PROVIDER=msg91" in text
    assert "131 of 245 alerts in the last 24 hours were opened in Google Maps" in text


def test_console_offers_waze_beside_google_maps(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    links = p.locator('#plates a:has-text("Open in Waze")')
    assert links.count() == 2
    href = links.first.get_attribute("href")
    assert href.startswith("https://waze.com/ul?ll=") and "navigate=yes" in href
    assert "destination only" in links.first.get_attribute("title")


def test_feeds_tab_shows_the_closure_feed_and_what_was_left_out(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-feeds")
    p.wait_for_selector("#cifs .pill")
    text = p.inner_text("#cifs")
    assert "Closure feed for Waze and Google Maps" in text and p.inner_text("#cifs .pill") == "Publishing"
    assert "7 entries from 9 active zones, 3 of them full closures" in text
    assert "2 left out: zone 14, no named road within 80 m of the zone" in text
    assert "/v1/feeds/cifs.json" in text and "75% sure" in text


def test_login_page_renders_and_reports_an_unreachable_server(open_page):
    p = open_page("login.html", width=420, height=700)
    assert p.inner_text("h1") == "Sign in" and p.get_attribute("#pw", "type") == "password"
    assert p.get_attribute("#pw", "autocomplete") == "current-password" and p.is_hidden("#msg") and p.is_hidden("#default")
    p.fill("#pw", "anything")
    p.click("#go")
    p.wait_for_selector("#msg:not([hidden])")
    assert "cannot be reached" in p.inner_text("#msg") and p.is_enabled("#go")
    assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ---------------------------------------------------------------- search, live map objects, Waze comparison page

def test_search_starts_at_two_letters_and_says_when_nothing_matches(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.fill("#to", "")
    p.type("#to", "ku", delay=30)
    p.wait_for_selector("#sg-to li[data-i]")
    assert p.inner_text("#sg-to li >> nth=0").startswith("Kukatpally")
    p.fill("#to", "")
    p.type("#to", "qqqq", delay=30)
    p.wait_for_selector("#sg-to li.none")
    assert "No place by that name" in p.inner_text("#sg-to") and "pin button" in p.inner_text("#sg-to")
    p.click("#sg-to li.none")                                   # the message is not a choice
    assert p.get_attribute("#to", "data-ll") is None


def test_reports_show_as_caution_signs_and_unconfirmed_ones_pulse(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".sign")
    assert p.locator(".sign").count() == 3 and p.locator(".sign.pending").count() == 2
    titles = [p.locator(".sign").nth(i).get_attribute("title") for i in range(3)]
    assert any(t.startswith("Water on the road, reported") and t.endswith("not yet confirmed") for t in titles)
    assert any(t.startswith("Road closed") and t.endswith("confirmed") and "not yet" not in t for t in titles)
    assert p.locator(".sign svg path[stroke='#C8322B']").count() == 3            # each is a red-bordered triangle
    assert "Report from the road" in p.inner_text("#legend")
    anim = p.evaluate("getComputedStyle(document.querySelector('.sign.pending'), '::after').animationName")
    assert anim == "ping"


def test_reduced_motion_stops_the_pulse(browser):
    from conftest import STATIC, _route
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    p = ctx.new_page()
    p.route("**/*", _route)
    p.goto(f"file://{STATIC / 'console.html'}")
    p.wait_for_selector(".sign.pending")
    assert p.evaluate("getComputedStyle(document.querySelector('.sign.pending'), '::after').animationName") == "none"
    ctx.close()


def test_zone_carries_a_plate_saying_what_and_until_when(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".zplate")
    assert p.inner_text(".zplate") == "Waterlogging, until 9:40 am"
    assert p.get_attribute(".zplate", "title") == "Jubilee Hills Check Post to Madhapur"
    assert p.get_attribute("#waze-link", "href") == "waze.html"


def test_waze_page_times_who_showed_a_closure_first(open_page):
    p = open_page("waze.html")
    p.wait_for_selector(".z")
    assert p.is_visible("#preview") and p.locator(".z").count() == 2
    assert p.get_attribute("#waze", "src").startswith("https://embed.waze.com/iframe?zoom=12&lat=17.43100&lon=78.41950")
    assert "Nothing from DetourAI is drawn on it" in p.inner_text(".frame p")
    first = p.locator(".z").nth(0)
    first.locator("[data-act=show]").click()
    assert "zoom=16&lat=17.39840&lon=78.41260&pin=1" in p.get_attribute("#waze", "src")
    p.locator(".z").nth(0).locator("[data-act=waze]").click()
    card = p.locator(".z").nth(0)
    assert card.locator("[data-act=waze]").inner_text().startswith("Seen on Waze at")
    assert card.locator(".lead").inner_text() in ("DetourAI had it 26 min earlier", "DetourAI had it 25 min earlier", "DetourAI had it 27 min earlier")
    assert p.inner_text("#count") == "1 zone timed"
    with p.expect_download() as dl:
        p.click("#dl")
    rows = open(dl.value.path()).read().strip().splitlines()
    assert rows[0] == "zone_id,what,detour_at,waze_at,gmaps_at,notes" and rows[1].startswith('41,"Waterlogging at Tolichowki Underpass",20')
    assert rows[1].count("T") >= 2 and rows[1].endswith(",,")           # Waze time set, Maps time empty, no notes
    p.locator(".z").nth(0).locator("[data-act=waze]").click()             # pressing again takes the time back
    assert p.inner_text("#count") == ""
    assert p.errors == []


# ---------------------------------------------------------------- live route page: staying in DetourAI

def test_card_opens_with_the_reason_and_one_clear_action(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.inner_text("#notice").startswith("Waterlogging at Jubilee Hills Check Post, until about 9:40 am.")
    assert p.inner_text("#go") == "Start route A" and "wide" in p.get_attribute("#go", "class")
    assert p.is_hidden("#eta") and p.is_hidden("#recenter") and p.is_visible("#report-open")
    more = p.inner_text("#more")
    assert more.startswith("Prefer another app? Google Maps or Waze.")
    assert p.get_attribute("#more a >> nth=1", "href").startswith("https://waze.com/ul?")
    p.click('.rt[data-r="B"]')
    assert p.inner_text("#go") == "Start route B"


def test_card_while_moving_shows_arrival_and_gives_the_map_the_room(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.click("#go")
    p.wait_for_function("started()")                                      # starting waits for the screen wake lock
    p.evaluate("clearInterval(simTimer); onPosition(78.3935, 17.4469)")
    assert p.is_hidden("#notice") and p.inner_text("#go") == "Stop"
    assert p.locator("#map").bounding_box()["height"] > 300, "the map keeps its room when the notice goes"
    eta = p.inner_text("#eta")
    assert eta.startswith("Arrive about ") and " min, " in eta and eta.endswith(" left")
    p.evaluate("map.fire('dragstart')")                                   # the person pans away
    assert p.is_visible("#recenter")
    p.click("#recenter")
    assert p.is_hidden("#recenter") and p.evaluate("follow") is True
    p.click("#go")                                                        # stop: the reason comes back
    assert p.is_visible("#notice") and p.inner_text("#go") == "Start route A"


def test_card_report_is_one_tap_and_needs_a_position(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.click("#report-open")
    assert p.is_visible("#sheet") and p.locator(".kind").all_inner_texts() == ["Water on road", "Road closed", "Accident", "Gate closed", "Too narrow", "No entry here"]
    p.keyboard.press("Escape")
    assert p.is_hidden("#sheet")
    p.click("#report-open")
    p.click('.kind[data-kind="WATER"]')
    p.wait_for_selector("#toast:not([hidden])")
    assert "Press Start first" in p.inner_text("#toast") and p.is_hidden("#sheet")
    p.evaluate("onPosition(78.3935, 17.4469)")
    p.click("#report-open")
    p.click('.kind[data-kind="CLOSED"]')
    p.wait_for_function("document.getElementById('toast').textContent.includes('Preview')")


def test_card_asks_still_blocked_once_when_passing_a_zone(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.is_hidden("#ask")
    p.evaluate("onPosition(78.3935, 17.4469)")                            # far from the zone
    assert p.is_hidden("#ask")
    p.evaluate("onPosition(78.4190, 17.4441)")                            # about 80 m from it
    assert p.is_visible("#ask") and p.inner_text("#ask-text") == "Waterlogging at Jubilee Hills Check Post. Is it still blocked?"
    p.click("#ask-no")
    assert p.is_hidden("#ask")
    p.evaluate("onPosition(78.4191, 17.4442)")                            # still there: not asked again
    assert p.is_hidden("#ask")
    assert p.errors == []


# ---------------------------------------------------------------- riders correct the route

def test_leaving_the_route_asks_why_in_one_tap(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.click("#go")
    p.wait_for_function("started()")
    p.evaluate("clearInterval(simTimer); onPosition(78.3935, 17.4469)")          # on route A
    assert p.is_hidden("#why")
    p.evaluate("onPosition(78.4300, 17.4600)")                                    # well off it
    assert p.is_hidden("#why"), "not asked on the first stray fix"
    p.evaluate("offSince = Date.now() - 20000; onPosition(78.4301, 17.4601)")     # still off after the wait
    assert p.is_visible("#why") and p.inner_text("#why-text") == "You left Route A. What happened?"
    assert p.locator("#why button").all_inner_texts() == ["Road blocked", "Gate closed", "Too narrow", "My choice"]
    p.click('#why button[data-why="GATE_CLOSED"]')
    assert p.is_hidden("#why")
    p.wait_for_function("document.getElementById('toast').textContent.includes('Preview')")
    p.evaluate("onPosition(78.4302, 17.4602)")
    assert p.is_hidden("#why"), "asked once per departure"
    p.evaluate("onPosition(78.3935, 17.4469); offSince = null; onPosition(78.4300, 17.4600); offSince = Date.now() - 20000; onPosition(78.4301, 17.4601)")
    assert p.is_visible("#why"), "a new departure asks again"


def test_route_correction_from_the_report_sheet(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.click("#report-open")
    assert "Something wrong with this route?" in p.inner_text("#sheet")
    p.click('.kind[data-fb="TOO_NARROW"]')
    p.wait_for_function("document.getElementById('toast').textContent.includes('Press Start first')")
    p.evaluate("onPosition(78.3935, 17.4469)")
    p.click("#report-open")
    p.click('.kind[data-fb="NO_ENTRY"]')
    p.wait_for_function("document.getElementById('toast').textContent.includes('Preview')")


def test_a_report_ahead_on_the_route_is_announced_once(open_page):
    p = open_page("card.html", width=390, height=800)
    p.wait_for_selector(".rt")
    p.wait_for_selector(".hz")                                                    # drawn on the map as a sign
    p.evaluate("onPosition(78.3935, 17.4469)")                                    # kilometres before it
    assert p.is_hidden("#toast")
    p.evaluate("onPosition(78.4305, 17.4346)")                                    # a few hundred metres before it
    p.wait_for_selector("#toast:not([hidden])")
    text = p.inner_text("#toast")
    assert text.startswith("An accident reported ") and text.endswith(" ahead, not yet confirmed.")
    p.evaluate("document.getElementById('toast').hidden = true; onPosition(78.4310, 17.4342)")
    assert p.is_hidden("#toast"), "said once"


def test_console_lists_rider_corrections_with_what_apply_will_do(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-review")
    p.wait_for_selector("#corrlist .road")
    cards = p.locator('#corrlist .road:not([data-kind="SKIPPED"])')
    assert cards.count() == 2
    first = cards.nth(0).inner_text()
    assert "Too narrow for a car" in first and "3 riders agree" in first and "5.5 m wide, an estimate" in first
    assert "Set the width to 2.0 m" in first
    assert "gate state unknown" in cards.nth(1).inner_text() and "1 rider" in cards.nth(1).inner_text()
    cards.nth(0).locator('[data-corr="apply"]').click()
    p.wait_for_selector("#toast:not([hidden])")
    assert "Preview data cannot be changed" in p.inner_text("#toast")


# ---------------------------------------------------------------- trip events, with the rider's agreement

def test_sharing_is_asked_once_before_the_first_trip_and_can_be_turned_off(open_page):
    p = open_page("card.html", query="?share=1", width=390, height=800)
    p.wait_for_selector(".rt")
    assert p.is_hidden("#share")
    p.click("#go")
    p.wait_for_selector("#share:not([hidden])")
    text = p.inner_text("#share")
    assert "never your track" in text and "fixed for everyone" in text
    assert p.evaluate("started()") is False, "nothing starts before the rider answers"
    p.click("#share-yes")
    p.wait_for_function("started()")
    assert p.is_hidden("#share") and p.inner_text("#share-line").startswith("Sharing trip events: on.")
    assert p.evaluate("eventQueue.map(e => e.kind)") == ["START"]
    p.evaluate("clearInterval(simTimer)")
    p.click('.rt[data-r="B"]')
    p.evaluate("onPosition(78.3935, 17.4469); onPosition(78.4300, 17.4600); offSince = Date.now() - 20000; lastOn = [78.3935, 17.4469]; onPosition(78.4301, 17.4601)")
    q = p.evaluate("eventQueue")
    assert [e["kind"] for e in q] == ["START", "SWITCH", "OFF_ROUTE"] and q[1]["route"] == "B"
    assert (q[2]["lat"], q[2]["lng"]) == (17.4469, 78.3935), "a departure carries only the last point on the route"
    assert all(set(e) <= {"kind", "route", "at", "lat", "lng"} for e in q)
    p.click("#share-toggle")
    assert p.inner_text("#share-line").startswith("Sharing trip events: off.") and p.evaluate("eventQueue.length") == 0
    p.evaluate("logEvent('ARRIVE')")
    assert p.evaluate("eventQueue.length") == 0, "nothing is recorded once turned off"


def test_declining_to_share_still_starts_the_trip_and_records_nothing(open_page):
    p = open_page("card.html", query="?share=1", width=390, height=800)
    p.wait_for_selector(".rt")
    p.click("#go")
    p.wait_for_selector("#share:not([hidden])")
    p.click("#share-no")
    p.wait_for_function("started()")
    assert p.evaluate("eventQueue.length") == 0 and p.inner_text("#share-line").startswith("Sharing trip events: off.")
    p.click("#go")                                                       # stop, and start again: not asked twice
    p.click("#go")
    p.wait_for_function("started()")
    assert p.is_hidden("#share")


def test_console_usage_card_and_deciding_a_skipped_stretch(open_page):
    p = open_page("console.html")
    p.wait_for_selector(".plate")
    p.click("#t-feeds")
    p.wait_for_selector("#usage .road")
    u = p.inner_text("#usage")
    assert "48 trips started on the live route, 81% finished on it" in u
    assert "live route 69%, Google Maps 25%, Waze 6% (88 of 131 alerts opened)" in u
    assert "Counted only for riders who agreed" in u
    p.click("#t-review")
    p.wait_for_selector("#corrlist .road")
    skipped = p.locator('#corrlist .road[data-kind="SKIPPED"]')
    assert skipped.count() == 1 and "4 riders left here" in skipped.inner_text()
    assert skipped.locator("[data-corr]").all_inner_texts() == ["Too narrow", "No entry", "Gate closed", "Nothing wrong", "Show on map"]
    skipped.locator('[data-as="NO_ENTRY"]').click()
    p.wait_for_selector("#toast:not([hidden])")
    assert "Preview data cannot be changed" in p.inner_text("#toast")
