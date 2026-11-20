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
    assert p.inner_text("#review-count") == "3" and p.is_visible("#review-count")
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
    assert p.locator(".tab").all_inner_texts() == ["Routes", "Advisories", "Review3", "Roads", "Feeds"]
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
