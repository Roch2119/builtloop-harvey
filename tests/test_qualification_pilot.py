"""Qualification pilot: evidence, three separate decisions, and the gates.

Websites are canned (httpx MockTransport): nothing here touches the network,
sends email or searches for contacts.
"""

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from harvey import assess, qualify_run
from harvey import qualification as qual
from harvey.agents.writer import Writer
from harvey.config import QualificationConfig
from harvey.models.company import Company
from harvey.site_evidence import SiteReader
from harvey.state import MIGRATIONS, StateManager
from tests.test_disqualification import company, contact, writer_config
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign, state  # noqa: F401
from tests.test_writer_retry import GOOD, FakeBrain

HOME = """<html><head><title>Northside Dog Grooming | Manchester</title>
<meta name="viewport" content="width=device-width"></head><body>
<h1>Northside Dog Grooming</h1>
<p>Professional dog grooming and dog washing at 12 High Street, Manchester M1 1AA.
Call us on 0161 555 0100 to book your dog in. We have groomed dogs across Manchester for ten years and
love every breed that comes through our door, big or small, fluffy or short coated.</p>
<a href="/about">About</a><a href="/contact">Contact</a><a href="/services">Services</a><a href="/shop">Shop</a>
</body></html>"""
SHOP = ("<html><head><title>Shop courses</title></head><body><h1>Courses</h1><ul><li>15-16 January 2023 Masterclass "
        "£833.33 ADD TO CART</li><li>Basic course six days £3,958.33 ADD TO CART</li></ul>" + "More text. " * 40 + "</body></html>")
ABOUT = "<html><head><title>About</title></head><body><h1>About us</h1><p>Family run dog groomers in Manchester since 2014. " + "We care. " * 30 + "</p></body></html>"


def site(handlers, default_status=404):
    def handler(request: httpx.Request) -> httpx.Response:
        h = handlers.get(str(request.url))
        if h is None:
            return httpx.Response(default_status, text="nope")
        return h if isinstance(h, httpx.Response) else httpx.Response(200, text=h)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def cfg(**kw):
    return SimpleNamespace(
        qualification=QualificationConfig(enforce=True, request_delay_seconds=0, **kw),
        channels=SimpleNamespace(email=SimpleNamespace(
            provider="gmail", require_approval=True, send_to_risky=False)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )


async def grooming(state, domain="northsidegrooming.test"):
    return await state.add_company(Company(
        name="Dog Groomers Manchester", domain=domain, industry="dog groomer", location="Manchester"))


async def assess_with(state, config, cid, handlers, domain="northsidegrooming.test"):
    reader = SiteReader(7, 0, client=site(handlers))
    return await qualify_run.assess_company(state, config, cid, reader=reader)


GOOD_SITE = {f"https://northsidegrooming.test": HOME,
             "https://northsidegrooming.test/about": ABOUT,
             "https://northsidegrooming.test/contact": ABOUT,
             "https://northsidegrooming.test/services": ABOUT,
             "https://northsidegrooming.test/shop": SHOP}


# ── evidence, uncertainty ──

@pytest.mark.asyncio
async def test_confirmed_identity_is_kept_apart_from_the_query(state):
    cid = await grooming(state)
    r = await assess_with(state, cfg(), cid, GOOD_SITE)
    ident = r["identity"]
    assert ident["query_derived"]["name"] == "Dog Groomers Manchester"      # from the search
    assert ident["name"]["confirmed"] == "Northside Dog Grooming"            # from the site
    assert ident["decision"] == "pass"
    assert all(f["url"].startswith("https://northsidegrooming.test") for f in r["offer_fit"]["candidate_facts"])
    assert all(f["quote"] and f["observed_at"] for f in r["offer_fit"]["candidate_facts"])
    assert await state.get_page_evidence(cid)                                 # pages stored


@pytest.mark.asyncio
@pytest.mark.parametrize("handlers", [
    {},                                                                       # nothing loads
    {"https://northsidegrooming.test": httpx.Response(
        302, headers={"location": "https://elsewhere.test/"}),
     "https://elsewhere.test/": HOME},                                        # redirected away
    {"https://northsidegrooming.test": "<html><body><div id=app></div><script src=a.js></script></body></html>"},
])
async def test_failed_redirected_or_unrendered_sites_go_to_review_not_out(state, handlers):
    cid = await grooming(state)
    r = await assess_with(state, cfg(), cid, handlers)
    assert r["proposed"] == "needs_review"
    assert r["state"] == "needs_review"
    assert (await state.get_company(cid)).disqualified_at is None
    assert r["identity"]["uncertainty"] or r["identity"]["decision"] != "pass"


@pytest.mark.asyncio
async def test_an_undetected_feature_is_never_a_claim_or_a_pass(state):
    cid = await grooming(state)
    r = await assess_with(state, cfg(), cid, GOOD_SITE)
    fit = r["offer_fit"]
    assert not [f for f in fit["candidate_facts"] if f.get("polarity") == "not_detected"]
    assert not [f for f in fit["candidate_facts"] if "booking" in f["text"].lower() and "no " in f["text"].lower()]
    # Only not-detected facts => review, never a pass or a writer fact.
    only_nd = assess.assess_offer_fit({"name": "x"}, [], r["identity"])
    assert only_nd["decision"] != "pass"


@pytest.mark.asyncio
async def test_only_a_configured_mismatch_excludes_automatically(state):
    directory = await state.add_company(Company(name="Whatclinic", domain="whatclinic.com", industry="clinic"))
    r = await assess_with(state, cfg(), directory, {}, domain="whatclinic.com")
    assert r["proposed"] == "disqualified"
    assert (await state.get_company(directory)).disqualified_at is not None

    # Franchise / nonprofit / agency-built wording is not a reason to exclude.
    html = HOME.replace("</body>", "<footer>Franchise location. Website by Acme Web Agency. Registered charity.</footer></body>")
    cid = await grooming(state)
    r2 = await assess_with(state, cfg(), cid, {**GOOD_SITE, "https://northsidegrooming.test": html})
    assert r2["proposed"] != "disqualified"
    assert (await state.get_company(cid)).disqualified_at is None


@pytest.mark.asyncio
async def test_excluded_domains_come_from_config(state):
    cid = await grooming(state, "listings.test")
    r = await assess_with(state, cfg(excluded_domains=["listings.test"]), cid, {}, domain="listings.test")
    assert r["proposed"] == "disqualified"


# ── states and owner approval ──

@pytest.mark.asyncio
async def test_harvey_proposes_but_only_a_person_qualifies(state):
    cid = await grooming(state)
    r = await assess_with(state, cfg(), cid, GOOD_SITE)
    assert r["proposed"] == "qualified"
    assert await state.company_gate_state(cid) == "needs_review"
    res = await state.approve_qualification(cid, note="looks right")
    assert res["ok"] and await state.company_gate_state(cid) == "qualified"
    assert await state.get_approved_evidence(cid)


@pytest.mark.asyncio
async def test_approval_needs_an_assessment_and_a_rerun_does_not_overturn_it(state):
    cid = await grooming(state)
    assert not (await state.approve_qualification(cid))["ok"]
    await assess_with(state, cfg(), cid, GOOD_SITE)
    await state.approve_qualification(cid)
    await assess_with(state, cfg(), cid, {})                 # site now unreachable
    assert await state.company_gate_state(cid) == "qualified"
    await state.hold_qualification(cid)
    assert await state.company_gate_state(cid) == "needs_review"
    assert await state.get_approved_evidence(cid) == []


@pytest.mark.asyncio
async def test_approval_cannot_reopen_a_disqualified_company(state):
    cid = await grooming(state)
    await assess_with(state, cfg(), cid, GOOD_SITE)
    await state.disqualify_company(cid, "not a fit", source="cli")
    assert not (await state.approve_qualification(cid))["ok"]


# ── the gates ──

@pytest.mark.asyncio
async def test_unqualified_companies_get_no_contact_search_and_no_draft(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    assert await state.companies_needing_prospects(qualified_only=True) == []
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, cfg(), None).run()
    assert brain.calls == 0
    p = await state.get_prospect(pid)
    assert p.status == "ineligible" and p.status_reason.startswith(qual.QUALIFICATION_PENDING)


@pytest.mark.asyncio
async def test_approval_releases_the_parked_contact_and_it_can_progress(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    await Writer(FakeBrain(reply=GOOD), state, cfg(), None).run()
    assert (await state.get_prospect(pid)).status == "ineligible"
    await assess_with(state, cfg(), cid, GOOD_SITE)
    await state.approve_qualification(cid)
    assert (await state.get_prospect(pid)).status == "new"
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, cfg(), None).run()
    assert (await state.get_prospect(pid)).status != "ineligible"


@pytest.mark.asyncio
async def test_final_send_check_holds_cold_email_to_an_unqualified_company(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    camp = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    sender.config.qualification = QualificationConfig(enforce=True)
    await sender._stage_campaign_native(camp)
    assert await state.get_outbox() == []                    # nothing staged for an unapproved company


@pytest.mark.asyncio
async def test_an_approved_email_to_an_unapproved_company_is_held_not_sent(state):
    from tests.test_disqualification import approved_follow_up
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test", status="contacted")
    item = await approved_follow_up(state, pid, "info@northsidegrooming.test")
    sender = make_sender(state, FakeProvider())
    sender.config.qualification = QualificationConfig(enforce=True)
    await sender._drain_due()
    assert sender.provider.sent == []
    row = await state.get_outbox_item(item)
    assert row["status"] == "pending_review" and row["error"].startswith(qual.QUALIFICATION_PENDING)
    assert row["subject"] == "following up" and row["body"] == "A plain note. Ok?"   # wording kept
    # Not enforced (the default): behaviour is unchanged.
    sender.config.qualification = QualificationConfig(enforce=False)


# ── evidence reaches the writer, and is shown beside the draft ──

class EvidenceBrain(FakeBrain):
    def __init__(self, cited):
        super().__init__(reply=None)
        self.cited, self.prompts = cited, []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        self.calls += 1
        ids = self.cited(self.prompts[-1])
        return {"subject": "dog grooming", "body": "Hi Jane, saw you groom dogs in Manchester.", "evidence_ids": ids}


@pytest.mark.asyncio
async def test_approved_evidence_is_given_to_the_writer_and_recorded(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    conf = cfg()
    await assess_with(state, conf, cid, GOOD_SITE)
    await state.approve_qualification(cid)
    approved = await state.get_approved_evidence(cid)
    first = approved[0]["id"]
    brain = EvidenceBrain(lambda prompt: [first, "deadbeef"])    # one real id, one invented
    w = Writer(brain, state, conf, None)
    w.skills = ""
    draft = await w._write_personal_email(await state.get_prospect(pid))
    assert f"[{first}]" in brain.prompts[0] and approved[0]["url"] in brain.prompts[0]
    ev = draft["evidence"]
    assert [f["id"] for f in ev["cited"]] == [first]             # invented id dropped
    assert ev["cited"][0]["quote"] and ev["cited"][0]["observed_at"]
    assert "verify the wording" in ev["note"]                    # not presented as proof


@pytest.mark.asyncio
async def test_no_evidence_is_offered_for_an_unapproved_company(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    await assess_with(state, cfg(), cid, GOOD_SITE)              # assessed, not approved
    brain = EvidenceBrain(lambda p: [])
    w = Writer(brain, state, cfg(), None)
    w.skills = ""
    draft = await w._write_personal_email(await state.get_prospect(pid))
    assert "evidence" not in (draft or {})


@pytest.mark.asyncio
async def test_outbox_row_keeps_the_evidence(state):
    cid = await grooming(state)
    pid = await contact(state, cid, "info@northsidegrooming.test")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="info@northsidegrooming.test", subject="s", body="b",
        send_at="2026-10-30T10:00:00", status="pending_review", evidence_json=json.dumps({"cited": []}))
    assert json.loads((await state.get_outbox_item(item))["evidence_json"]) == {"cited": []}


# ── migration ──

@pytest.mark.asyncio
async def test_an_existing_v12_database_upgrades_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    for script in MIGRATIONS[:12]:
        con.executescript(script)
    con.execute("PRAGMA user_version = 12")
    con.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Old Co', 'old.test')")
    con.commit(); con.close()

    st = StateManager(path)
    await st.init_db()
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) >= 13
    assert {"qualification_state", "qualification_decided_by"} <= {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    assert con.execute("SELECT qualification_state FROM companies").fetchone()[0] == "unassessed"
    assert {"page_evidence", "qualification_assessments"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert await st.company_gate_state("c1") == "unassessed"


# ── research-quality rules found by the first real capture ──

def page(**kw):
    base = {"kind": "home", "final_url": "https://pawsgroom.test/", "requested_url": "https://pawsgroom.test", "http_status": 200,
            "error": "", "needs_rendering": False, "redirected_off_domain": False, "text_len": 900,
            "has_viewport": True, "copyright_year": 0, "quotes": [], "links": [], "booking_links": [],
            "fetched_at": "2026-10-22T12:00:00", "title": "X Dog Grooming", "h1": "Dog grooming in Manchester",
            "headings": [], "text_excerpt": "dog grooming Manchester"}
    base.update(kw)
    return base


CO = {"name": "Paws Grooming", "domain": "pawsgroom.test", "industry": "dog groomer", "location": "Manchester"}


def fit_of(*pages):
    ident = assess.assess_identity(CO, list(pages))
    return assess.assess_offer_fit(CO, list(pages), ident, 2026)


def test_weak_signals_alone_never_pass():
    for kw in ({"copyright_year": 2020},
               {"links": [{"href": "https://pawsgroom.test/a", "text": "a", "same_site": True}]},
               {"has_viewport": False},
               {"quotes": [{"type": "call_to_book", "quote": "call to book"}]},
               {"quotes": [{"type": "price_on_request", "quote": "prices on request"}]}):
        fit = fit_of(page(**kw))
        assert fit["decision"] == "review", kw
        assert fit["inferred_consequences"] == []
        assert assess.propose_state({"decision": "pass"}, fit) == "needs_review"


def test_a_call_to_book_is_dropped_when_the_site_has_a_booking_path():
    quote = {"type": "call_to_book", "quote": "Book Now call to book"}
    for extra in ({"booking_links": ["https://shop.phorest.com/x"]}, {"booking_popup": True}):
        fit = fit_of(page(quotes=[quote], **extra))
        assert not [f for f in fit["observed_facts"] if f["type"] == "call_to_book"]
        assert [f for f in fit["context_facts"] if f["type"] == "call_to_book_also"]


def test_a_past_dated_item_with_a_buy_button_is_a_strong_observed_fact():
    item = {"date_text": "15-16 January 2023", "year": 2023, "month": 1, "offer_button": "ADD TO CART",
            "quote": "15-16 January 2023 Online Masterclass £833.33 ADD TO CART", "price": "£833.33"}
    fit = fit_of(page(kind="shop", dated_items=[item]))
    assert fit["decision"] == "pass"
    f = [x for x in fit["observed_facts"] if x["type"] == "dated_item"][0]
    assert f["quote"] == item["quote"] and f["writer_usable"] and f["observed_at"]
    assert "hypothesis" in fit["inferred_consequences"][0]["kind"]
    assert "stale" not in f["text"].lower() and "outdated" not in f["text"].lower()
    future = dict(item, year=2027, date_text="3 March 2027", month=3)
    assert fit_of(page(kind="shop", dated_items=[future]))["decision"] == "review"


def test_extractor_reads_dated_items_embeds_popups_and_clean_quotes():
    from harvey.site_evidence import extract_page
    html = ("<html><head><title>Shop</title></head><body><h1>Shop</h1>"
            "<iframe src='https://app.zenmaid.com/book/x'></iframe>"
            "<a href='#elementor-action:popup' role=button>BOOK YOUR TREATMENT</a>"
            "<p>15-16 January 2023 Online Permanent Makeup Masterclass £1,083.33 ADD TO CART</p>"
            "<p>Great groomers, thanks Kim S. 7/29/2023 totally recommend.</p>"
            "<footer>© 2023 by The Hairy Dog. All rights reserved. Contact us now to arrange something</footer>"
            + "<p>filler text here</p>" * 30 + "</body></html>")
    rec = extract_page(html, "https://pawsgroom.test/shop", "shop", base_host="pawsgroom.test")
    assert any("zenmaid.com" in b for b in rec.booking_links)           # embedded tool recognised
    assert rec.booking_popup
    assert [d["year"] for d in rec.dated_items] == [2023]               # the numeric review date is ignored
    assert rec.dated_items[0]["quote"].endswith("ADD TO CART") and rec.dated_items[0]["price"] == "£1,083.33"
    assert rec.copyright_year == 2023
    q = rec.copyright_quote
    assert q == q.strip() and not q.startswith(("e.", "Kim")) and "Hairy Dog" in q


def test_get_a_quote_is_not_a_price_on_request_signal():
    from harvey.site_evidence import extract_page
    rec = extract_page("<html><body><h1>The Maids</h1>" + "<a href='/q'>GET A QUOTE</a> Call now. " * 5 +
                       "filler " * 80 + "</body></html>", "https://pawsgroom.test/", "home", base_host="pawsgroom.test")
    assert not [q for q in rec.quotes if q["type"] == "price_on_request"]


@pytest.mark.asyncio
async def test_a_bot_check_page_is_an_access_limit_not_a_business_problem(state):
    cid = await grooming(state)
    blocked = httpx.Response(403, text="<html><title>Just a moment...</title>Checking your browser</html>")
    r = await assess_with(state, cfg(), cid, {f"https://northsidegrooming.test": blocked,
                                              "https://www.northsidegrooming.test": blocked,
                                              "http://northsidegrooming.test": blocked,
                                              "http://www.northsidegrooming.test": blocked})
    kinds = {u["type"] for u in r["identity"]["uncertainty"]}
    assert "access_challenge" in kinds and "fetch_failed" not in kinds
    assert r["state"] == "needs_review" and (await state.get_company(cid)).disqualified_at is None
    text = " ".join(u["text"] for u in r["identity"]["uncertainty"])
    assert "not a statement about the business" in text


@pytest.mark.asyncio
async def test_shop_and_event_pages_are_read_too(state):
    cid = await grooming(state)
    handlers = dict(GOOD_SITE)
    handlers["https://northsidegrooming.test"] = HOME.replace(
        '<a href="/shop">Shop</a>', '<a href="/shop">Shop</a><a href="/events">Events</a>')
    handlers["https://northsidegrooming.test/events"] = ABOUT
    r = await assess_with(state, cfg(), cid, handlers)
    kinds = [p["kind"] for p in await state.get_page_evidence(cid)]
    assert "shop" in kinds and "events" in kinds
    assert r["proposed"] == "qualified"                       # the dated shop item is the specific reason


def test_copyright_quote_starts_at_the_sign_and_subdomain_booking_counts():
    from harvey.site_evidence import extract_page
    html = ("<html><body><p>Great groomer. Kim S. 7/29/2023 © 2023 by The Hairy Dog. Proudly created.</p>"
            "<a href='https://app.pawsgroom.test/contact'>Book Now</a></body></html>")
    rec = extract_page(html, "https://pawsgroom.test/", "home")
    assert rec.copyright_quote.startswith("© 2023")
    assert "Kim" not in rec.copyright_quote
    assert rec.booking_links == ["https://app.pawsgroom.test/contact"]


def test_fact_ids_are_unique_and_consequences_are_not_repeated():
    q = "We offer grooming and bathing for dogs."
    a = assess._fact("service", "grooming", "https://pawsgroom.test/", q, "observed")
    b = assess._fact("service", "bathing", "https://pawsgroom.test/", q, "observed")
    assert a["id"] != b["id"]
    assert assess._sentence_quote("Home Services About Contact Book now. Manchester", 5, 13) == ""


def test_service_quotes_are_whole_sentences_not_menu_fragments():
    pg = {"http_status": 200, "final_url": "https://pawsgroom.test/", "title": "", "meta_description": "", "h1": "", "headings": [],
          "text_excerpt": "Home About Treatments Microblading Aquarelle Lips Powder Brows Eyeliner. "
                          "We offer microblading for natural looking brows in Manchester."}
    out = assess._services([pg], "brow/pmu studio", {})
    mb = [f for f in out if f["term"] == "microblading"]
    assert mb and mb[0]["quote"].startswith("We offer microblading")
