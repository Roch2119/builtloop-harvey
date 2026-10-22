"""Qualification: three separate questions about a company.

  identity       Is this a real business of the kind we target, in the place we
                 target? Name, services and location are CONFIRMED from the
                 business's own pages; what came from the search query is kept
                 apart and never counts as confirmation.
  offer_fit      Is there a specific reason a builtloop offer could help?
                 Observed facts (with URL, quote and date) are kept apart from
                 the inferred consequence and the proposed offer.
  contact        Is there someone we may write to (existing email policy)?

Decisions: identity  pass | review | fail
           offer_fit pass | review
           contact   pass | review | blocked

Only a configured, clear mismatch can fail identity (a directory, marketplace,
booking platform or social domain). Franchises, nonprofits, agency-built sites
and article-like titles are noted and routed to review, never excluded.
Anything unreadable, redirected, or needing JavaScript is uncertainty, and
uncertainty goes to review. A feature that was not detected is never recorded
as absent: it is stored as "not detected in pages checked" and cannot, alone,
make an offer-fit pass or be stated to the prospect.

Pure functions: pages in, assessment out. No network, no model.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from harvey.collectors.discover import JUNK_DOMAIN_PATTERNS
from harvey.qualification import domain_of, email_block_reason

# What each niche's own site would plausibly say. Matching is a way to find a
# QUOTE to show a person, not a verdict; a niche missing here is "unconfirmed".
DEFAULT_NICHE_TERMS: dict[str, tuple[str, ...]] = {
    "aesthetics clinic": ("aesthetic", "botox", "filler", "injectable", "skin", "facial", "laser",
                          "peel", "microneedling", "anti-wrinkle", "dermal"),
    "med spa": ("med spa", "medspa", "botox", "filler", "laser", "facial", "skin", "aesthetic"),
    "dog groomer": ("groom", "bath", "haircut", "trim", "de-shed", "deshed"),
    "dog trainer": ("training", "trainer", "obedience", "puppy", "behavio", "reactiv"),
    "house cleaning service": ("cleaning", "cleaner", "maid", "housekeep", "deep clean", "move-out", "move out"),
    "beauty salon": ("salon", "hair", "beauty", "nails", "colour", "color", "styling"),
    "nail salon": ("nail", "manicure", "pedicure", "gel"),
    "brow and permanent makeup studio": ("brow", "microblading", "permanent makeup", "pmu", "lash"),
    "brow/pmu studio": ("brow", "microblading", "permanent makeup", "pmu", "lash"),
    "lash studio": ("lash", "extension", "lift"),
}

_NONPROFIT_RE = re.compile(
    r"\b(?:donate|donation|registered charity|charity number|501\s?\(c\)\s?\(?3\)?|non-?profit|"
    r"adopt(?:ion)?|volunteer)\b", re.I)
_FRANCHISE_RE = re.compile(
    r"\b(?:franchise|independently owned and operated|find a location|our locations|locations near you|"
    r"nationwide|branches)\b", re.I)
_ARTICLE_TITLE_RE = re.compile(r"\b(?:guide|how much|cost of|costs?\b.*\bin\b|best\b.*\bin\b|top \d+)\b", re.I)
_SERVICE_AREA_CUES = re.compile(r"serv(?:ing|e|ice area)|areas?\s+(?:we\s+)?(?:serve|cover)|we\s+cover|covering|coverage", re.I)
_ADDRESS_CUES = re.compile(r"address|located|find us|visit us|our (?:clinic|studio|salon|shop)|based in", re.I)
_COMMON_NAME_NOISE = re.compile(r"^(home|welcome|homepage|home page|official site|official website)$", re.I)


def fact_id(ftype: str, url: str, quote: str) -> str:
    """Stable id for a fact: same finding => same id across re-assessments."""
    return hashlib.sha1(f"{ftype}|{url}|{quote}".encode()).hexdigest()[:8]


def _fact(ftype: str, text: str, url: str, quote: str, observed_at: str, *,
          polarity: str = "present", writer_usable: bool = False, scope: str = "") -> dict:
    return {"id": fact_id(ftype, url, f"{quote}||{text}"), "type": ftype, "text": text, "url": url,
            "quote": quote, "observed_at": observed_at, "polarity": polarity,
            "writer_usable": writer_usable, "scope": scope}


def configured_mismatch(domain: str, extra_domains: list[str] | tuple = ()) -> str:
    """Why this DOMAIN is a clear mismatch (directory, marketplace, social,
    booking platform, institution), or ''. Only the domain: names never count."""
    d = domain_of(domain)
    if not d:
        return ""
    for pattern in JUNK_DOMAIN_PATTERNS:
        if pattern.search(d):
            return f"domain {d} matches the configured directory/marketplace/platform list"
    for extra in extra_domains or ():
        e = domain_of(extra)
        if e and (d == e or d.endswith("." + e)):
            return f"domain {d} is in qualification.excluded_domains"
    return ""


def _norm_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1}


_NAV_WORDS = re.compile(r"\b(?:home|services?|about|contact|faq|gallery|book|menu|reviews?|blog|shop|supplies|"
                        r"appointments?|skip to content|login|log in|sign in)\b", re.I)


def _sentence_quote(text: str, start: int, end: int, limit: int = 160) -> str:
    """The sentence around a match, cut on word boundaries, or "" when what
    surrounds it is menu/navigation text (a poor thing to quote as evidence)."""
    lefts = [text.rfind(sep, 0, start) for sep in (". ", "? ", "! ")]
    a = max(lefts) + 2 if max(lefts) >= 0 else 0
    rights = [i for i in (text.find(sep, end) for sep in (". ", "? ", "! ")) if i >= 0]
    b = min(rights) + 1 if rights else len(text)
    if b - a > limit:
        a, b = max(a, start - 70), min(b, end + 80)
        while a < start and a > 0 and not text[a - 1].isspace():
            a += 1
        while b > end and b < len(text) and not text[b].isspace():
            b -= 1
    q = re.sub(r"\s+", " ", text[a:b]).strip()
    words = q.split()
    title_case = sum(1 for w in words if w[:1].isupper())
    if len(_NAV_WORDS.findall(q)) >= 3 or (len(words) >= 6 and title_case / len(words) > 0.6):
        return ""             # a menu or a run of headings, not a sentence
    # The words right around the match must read as prose. In a menu or a
    # title/slogan they are Capitalised, in a sentence mostly lower case.
    near = text[max(0, start - 60):start].split()[-4:] + text[end:end + 60].split()[:4]
    if sum(1 for w in near if w[:1].islower()) < 3:
        return ""
    # Drop leftovers at the front: a state code ("AZ The Hairy Dog…") or a menu
    # word ("Our Clinic BLOG Insights…").
    toks = q.split()
    if len(toks) > 3 and toks[0].isupper() and len(toks[0]) <= 3 and toks[1][:1].isupper():
        toks = toks[1:]
    for i, t in enumerate(toks[:6]):
        if _NAV_WORDS.fullmatch(t.strip(".,:;|-")) and i + 1 < len(toks):
            toks = toks[i + 1:]
    q = " ".join(toks)
    return q if text[start:end].lower() in q.lower() else ""


def _good_pages(pages: list[dict]) -> list[dict]:
    return [p for p in pages if p.get("http_status") == 200 and not p.get("error")]


def _name_candidates(pages: list[dict]) -> list[dict]:
    """Business-name candidates read from the site itself, best source first."""
    cands: list[dict] = []
    for p in _good_pages(pages):
        url = p.get("final_url") or p.get("requested_url") or ""
        for entry in p.get("jsonld") or []:
            if entry.get("name"):
                local = any(("business" in t.lower() or "clinic" in t.lower() or "store" in t.lower()
                             or "salon" in t.lower() or "organization" in t.lower()
                             or "service" in t.lower()) for t in entry.get("type", []))
                if local:
                    cands.append({"source": "structured data", "name": entry["name"], "url": url})
        if p.get("site_name"):
            cands.append({"source": "og:site_name", "name": p["site_name"], "url": url})
        if p.get("title"):
            for seg in re.split(r"\s+[|\-–—·•]\s+|\s*[|•·]\s*|:\s+", p["title"]):
                seg = seg.strip()
                if seg and not _COMMON_NAME_NOISE.match(seg):
                    cands.append({"source": "page title", "name": seg, "url": url})
                    break
        if p.get("h1") and p.get("kind") == "home":
            cands.append({"source": "main heading", "name": p["h1"], "url": url})
    return cands


def _confirm_name(cands: list[dict], query_name: str) -> dict:
    if not cands:
        return {"confirmed": "", "sources": [], "query_derived": query_name, "agreement": 0}
    best = cands[0]
    bt = _norm_tokens(best["name"])
    agree = [c for c in cands[1:] if bt and len(bt & _norm_tokens(c["name"])) / len(bt) >= 0.5]
    return {"confirmed": best["name"][:120], "sources": [best] + agree[:3],
            "query_derived": query_name, "agreement": len(agree)}


def _services(pages: list[dict], industry: str, extra_terms: dict) -> list[dict]:
    terms = list(DEFAULT_NICHE_TERMS.get((industry or "").strip().lower(), ()))
    for key, vals in (extra_terms or {}).items():
        if key.strip().lower() == (industry or "").strip().lower():
            terms += list(vals)
    if not terms:
        return []
    found: list[dict] = []
    seen_terms: set[str] = set()
    for p in _good_pages(pages):
        url = p.get("final_url") or p.get("requested_url") or ""
        spots = [("title", p.get("title", "")), ("description", p.get("meta_description", "")),
                 ("main heading", p.get("h1", ""))] + [("heading", h) for h in p.get("headings") or []]
        spots.append(("page text", p.get("text_excerpt", "")))
        for where, text in spots:
            low = (text or "").lower()
            for term in terms:
                if term in seen_terms:
                    continue
                i = low.find(term)
                if i >= 0:
                    if where == "page text":
                        quote, j = "", i
                        while j >= 0 and not quote:      # the first mention may be in a menu
                            quote = _sentence_quote(text, j, j + len(term))
                            j = low.find(term, j + 1)
                    else:       # a title, description or heading is already a clean unit
                        quote = re.sub(r"\s+", " ", text).strip()[:160]
                    if not quote:
                        continue        # only menu text here: try a later spot
                    seen_terms.add(term)
                    found.append({"term": term, "where": where, "url": url, "quote": quote})
    return found[:8]


def _location(pages: list[dict], target: str, aliases: dict) -> dict:
    city = (target or "").split(",")[0].strip().lower()
    names = {city} | {a.lower() for a in (aliases or {}).get(target, [])} | \
            {a.lower() for a in (aliases or {}).get(city, [])}
    names.discard("")
    out = {"target": target, "basis": "none", "quote": "", "url": ""}
    if not names:
        return out
    order = {"address": 0, "service_area": 1, "title": 2, "none": 9}
    for p in _good_pages(pages):
        url = p.get("final_url") or p.get("requested_url") or ""
        for entry in p.get("jsonld") or []:
            addr = " ".join((entry.get("address") or {}).values()).lower()
            area = (entry.get("area_served") or "").lower()
            if any(n in addr for n in names):
                cand = {"basis": "address", "quote": f"structured data address: {addr}"[:200], "url": url}
            elif any(n in area for n in names):
                cand = {"basis": "service_area", "quote": f"structured data areaServed: {area}"[:200], "url": url}
            else:
                continue
            if order[cand["basis"]] < order[out["basis"]]:
                out.update(cand)
        text = p.get("text_excerpt") or ""
        low = text.lower()
        for n in names:
            for m in re.finditer(re.escape(n), low):
                window = text[max(0, m.start() - 110): m.end() + 110]
                quote = _sentence_quote(text, m.start(), m.end())
                if not quote:
                    continue          # only menu text around it: not usable as evidence
                if _SERVICE_AREA_CUES.search(window):
                    basis = "service_area"
                elif _ADDRESS_CUES.search(window):
                    basis = "address"
                else:
                    continue
                if order[basis] < order[out["basis"]]:
                    out.update({"basis": basis, "quote": quote, "url": url})
                break
        head = " ".join([p.get("title", ""), p.get("meta_description", ""), p.get("h1", "")])
        if out["basis"] == "none" and any(n in head.lower() for n in names):
            out.update({"basis": "title", "quote": re.sub(r"\s+", " ", head).strip()[:200], "url": url})
    return out


def _attributes(pages: list[dict]) -> list[dict]:
    """Things worth a human's attention that never exclude on their own."""
    notes: list[dict] = []
    for p in _good_pages(pages):
        url = p.get("final_url") or p.get("requested_url") or ""
        text = p.get("text_excerpt") or ""
        for kind, rx, label in (
            ("nonprofit", _NONPROFIT_RE, "wording typical of a charity/nonprofit"),
            ("franchise_or_chain", _FRANCHISE_RE, "wording typical of a franchise or multi-location brand"),
        ):
            m = rx.search(text)
            if m and not any(n["type"] == kind for n in notes):
                notes.append({"type": kind, "text": label, "url": url,
                              "quote": re.sub(r"\s+", " ", text[max(0, m.start() - 60): m.end() + 60]).strip()})
        if p.get("built_by") and not any(n["type"] == "agency_built" for n in notes):
            notes.append({"type": "agency_built", "url": url, "quote": p["built_by"],
                          "text": "footer credits a web provider (neutral: not a sign of fit or misfit)"})
        if p.get("kind") == "home" and _ARTICLE_TITLE_RE.search(p.get("title", "")) and \
                not any(n["type"] == "article_like_title" for n in notes):
            notes.append({"type": "article_like_title", "url": url, "quote": p["title"][:160],
                          "text": "page title reads like an article; the site may still be a real business"})
    return notes


def uncertainties(pages: list[dict]) -> list[dict]:
    """Everything that stops us seeing the site properly."""
    out: list[dict] = []
    for p in pages:
        url = p.get("final_url") or p.get("requested_url") or ""
        if p.get("access_challenge"):
            out.append({"type": "access_challenge", "url": p.get("requested_url", ""),
                        "text": f"{p.get('kind')} page not read: the site served a bot-check page (HTTP "
                                f"{p.get('http_status')}). This is an access limit of Harvey's plain fetch, "
                                "not a statement about the business or its site; look at it in a browser."})
        elif p.get("error"):
            out.append({"type": "fetch_failed", "url": p.get("requested_url", ""),
                        "text": f"{p.get('kind')} page not read: {p['error']}"})
        elif p.get("redirected_off_domain"):
            out.append({"type": "redirect_off_domain", "url": url,
                        "text": f"{p.get('requested_url')} redirected to a different site: {url}"})
        if p.get("needs_rendering"):
            out.append({"type": "needs_rendering", "url": url,
                        "text": "little text in the HTML; the content probably needs JavaScript to appear, "
                                "so nothing can be said about what is or is not on this page"})
    return out


def assess_identity(company: dict, pages: list[dict], cfg=None) -> dict:
    extra_excl = list(getattr(cfg, "excluded_domains", []) or [])
    terms = dict(getattr(cfg, "niche_terms", {}) or {})
    aliases = dict(getattr(cfg, "city_aliases", {}) or {})
    domain = company.get("domain", "")
    observed = max([p.get("fetched_at", "") for p in pages] or [""]) or datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    mismatch = configured_mismatch(domain, extra_excl)
    base = {"query_derived": {"name": company.get("name", ""), "industry": company.get("industry", ""),
                              "location": company.get("location", ""),
                              "note": "from the search that found it; NOT confirmed"},
            "observed_at": observed}
    if mismatch:
        return {**base, "decision": "fail", "reason": f"Configured mismatch: {mismatch}.",
                "evidence_urls": [f"https://{domain}"], "uncertainty": [], "notes": [],
                "name": {}, "services": [], "location": {}}
    unc = uncertainties(pages)
    good = _good_pages(pages)
    home = next((p for p in good if p.get("kind") == "home"), None)
    urls = list(dict.fromkeys((p.get("final_url") or p.get("requested_url") or "") for p in good))
    if home is None:
        return {**base, "decision": "review",
                "reason": "The site could not be read, so the business was not confirmed (this says nothing "
                          "about whether the site or the business is good).",
                "evidence_urls": urls, "uncertainty": unc, "notes": [], "name": {}, "services": [], "location": {}}

    name = _confirm_name(_name_candidates(pages), company.get("name", ""))
    services = _services(pages, company.get("industry", ""), terms)
    loc = _location(pages, company.get("location", ""), aliases)
    notes = _attributes(pages)

    problems: list[str] = []
    if any(u["type"] == "redirect_off_domain" for u in unc):
        problems.append("the site redirects to a different domain; confirm it is the same business")
    if home.get("needs_rendering"):
        problems.append("the homepage needs JavaScript, so its content could not be read")
    if not name["confirmed"]:
        problems.append("no business name could be read from the site")
    if not services:
        known = (company.get("industry") or "").strip().lower() in DEFAULT_NICHE_TERMS or \
            any(k.strip().lower() == (company.get("industry") or "").strip().lower() for k in terms)
        problems.append("none of the services expected for this niche appear on the pages read" if known
                        else f"no service vocabulary is configured for '{company.get('industry','')}'")
    if loc["basis"] == "none":
        problems.append(f"{loc['target'] or 'the target location'} was not found as an address, service area "
                        "or heading on the pages read")
    for n in notes:
        if n["type"] in ("nonprofit", "franchise_or_chain"):
            problems.append(f"{n['text']} (a person should confirm it is an eligible business)")
    if problems:
        decision, reason = "review", "Needs a human look: " + "; ".join(problems) + "."
    else:
        decision = "pass"
        reason = (f"Confirmed from the business's own pages: named “{name['confirmed']}”, offers "
                  f"{', '.join(s['term'] for s in services[:3])}, location confirmed by {loc['basis'].replace('_', ' ')}.")
    return {**base, "decision": decision, "reason": reason, "name": name, "services": services,
            "location": loc, "notes": notes, "uncertainty": unc, "evidence_urls": urls}


_OFFERS = {
    "dated_item": ("a page showing which dated courses or events are currently running, with a clear enquiry "
                   "or waitlist step",
                   "a visitor may not be able to tell whether a dated course or event is still running, so some "
                   "may not enquire or book"),
    "call_to_book": ("an online booking request form on their own site",
                     "customers can only book by calling or messaging in working hours, so enquiries outside "
                     "those hours may go unanswered"),
    "social_booking": ("a booking request form on their own site",
                       "bookings run through a social app thread, which is easy to lose track of"),
    "price_on_request": ("a clear services-and-prices page",
                         "visitors cannot compare or decide before they contact the business"),
    "not_https": ("a secure, modern rebuild of the site",
                  "browsers may show a 'not secure' warning to visitors"),
    "no_viewport": ("a mobile-friendly rebuild",
                    "the page may be hard to use on a phone (needs a visual check to confirm)"),
    "stale_copyright": ("a refresh of the site",
                        "the site may look unmaintained to a new visitor (needs a visual check to confirm)"),
    "single_page": ("a fuller site with services, prices and booking pages",
                    "there is little for a visitor to read before deciding to get in touch"),
}
# A fit "pass" needs a specific, observed fact. Weak signals (an old copyright
# year, few links, no viewport tag) and common calls to action (call to book,
# "get a quote") are shown to the reviewer but cannot qualify a company alone.
STRONG_FACTS = frozenset({"dated_item", "not_https"})
_FIT_ORDER = ("dated_item", "call_to_book", "social_booking", "price_on_request", "not_https", "no_viewport",
              "single_page", "stale_copyright")


def assess_offer_fit(company: dict, pages: list[dict], identity: dict, today_year: int | None = None) -> dict:
    """Offer-fit assessment. When pages could not be read and nothing strong was
    found, the reason says the check is incomplete rather than "no reason found"."""
    out = _assess_offer_fit(company, pages, identity, today_year)
    unread = [u for u in out.get("uncertainty", []) if u["type"] in ("fetch_failed", "access_challenge")]
    if unread and out["decision"] == "review":
        kinds = ", ".join(sorted({u["text"].split(" page ")[0] for u in unread}))
        out["reason"] = (f"Incomplete: {len(unread)} page(s) could not be read ({kinds}), so this result may "
                         "be missing evidence. " + out["reason"])
    return out


def _assess_offer_fit(company: dict, pages: list[dict], identity: dict, today_year: int | None = None) -> dict:
    """A specific reason an offer could help, or an honest 'not established'."""
    now = datetime.now(timezone.utc)
    year = today_year or now.year
    today_month = now.month if year == now.year else 12
    good = _good_pages(pages)
    if identity.get("decision") == "fail" or not good:
        return {"decision": "review", "reason": "Not assessed: the site could not be read or the company "
                "failed the identity check.", "offer": "", "observed_facts": [], "inferred_consequences": [],
                "not_detected": [], "uncertainty": uncertainties(pages), "observed_at": identity.get("observed_at", "")}
    home = next((p for p in good if p.get("kind") == "home"), good[0])
    observed = max(p.get("fetched_at", "") for p in good)
    renders = not any(p.get("needs_rendering") for p in good)
    facts: list[dict] = []
    # A phone or message CTA says nothing about a missing booking system when
    # the site also links to, embeds or pops up a booking tool.
    has_booking_path = any(p.get("booking_links") or p.get("booking_popup") for p in good)
    suppressed: list[str] = []
    for p in good:
        url = p.get("final_url") or p.get("requested_url") or ""
        for q in p.get("quotes") or []:
            if q["type"] not in _OFFERS:
                continue
            if q["type"] in ("call_to_book", "social_booking") and has_booking_path:
                suppressed.append(q["quote"])
                continue
            facts.append(_fact(q["type"], f"The {p['kind']} page says: “{q['quote']}”", url, q["quote"],
                               p.get("fetched_at", observed), writer_usable=True))
        for d in p.get("dated_items") or []:
            past = (d["year"], d.get("month") or 12) < (year, today_month)
            if not past:
                continue
            facts.append(_fact("dated_item",
                               f"The {p['kind']} page lists “{d['quote']}” (dated {d['date_text']}) with a "
                               f"“{d['offer_button']}” button.", url, d["quote"],
                               p.get("fetched_at", observed), writer_usable=True))
    if (home.get("final_url") or "").startswith("http://"):
        facts.append(_fact("not_https", "The homepage loaded over http:// (no HTTPS).", home["final_url"],
                           home["final_url"], home.get("fetched_at", observed)))
    if renders and home.get("text_len", 0) >= 300 and not home.get("has_viewport"):
        facts.append(_fact("no_viewport", "The homepage HTML declares no mobile viewport.", home["final_url"],
                           "<meta name=viewport> not present in the homepage HTML", home.get("fetched_at", observed)))
    same_site_pages = {l["href"].split("#")[0].rstrip("/") for l in home.get("links", []) if l.get("same_site")}
    if renders and len(same_site_pages) <= 3 and home.get("text_len", 0) >= 300:
        facts.append(_fact("single_page", f"The homepage links to only {len(same_site_pages)} other page(s) on the site.",
                           home["final_url"], f"{len(same_site_pages)} internal link(s)", home.get("fetched_at", observed)))
    cy = home.get("copyright_year") or 0
    if cy and year - cy >= 3:
        facts.append(_fact("stale_copyright", f"The footer says “{home.get('copyright_quote')}”.", home["final_url"],
                           home.get("copyright_quote", ""), home.get("fetched_at", observed)))

    context: list[dict] = []
    for p in good:
        for link in p.get("booking_links") or []:
            context.append(_fact("booking_tool_link", "The site links to a third-party booking tool.", link, link,
                                 p.get("fetched_at", observed), writer_usable=False))
    for p in good:
        if p.get("booking_popup"):
            context.append(_fact("booking_popup", "A booking button on the site opens an on-page form.",
                                 p.get("final_url", ""), "", p.get("fetched_at", observed), writer_usable=False))
            break
    for qtext in suppressed[:2]:
        context.append(_fact("call_to_book_also", "The site also mentions calling to book, alongside a booking tool.",
                             home.get("final_url", ""), qtext, observed, writer_usable=False))
    not_detected: list[dict] = []
    scope = (f"Checked {len(good)} page(s): " + ", ".join(p.get('final_url', '') for p in good) +
             ". Static HTML only, nothing rendered.")
    if renders:
        if not any(p.get("booking_links") or p.get("booking_popup")
                   or any(q["type"] in ("call_to_book", "social_booking") for q in p.get("quotes", []))
                   for p in good):
            not_detected.append(_fact("booking_path_not_detected",
                                      "No booking link, widget wording or booking page was detected.",
                                      home["final_url"], "", observed, polarity="not_detected", scope=scope))
        if not any(p.get("price_text_seen") for p in good):
            not_detected.append(_fact("prices_not_detected", "No prices were detected in the text of the pages read.",
                                      home["final_url"], "", observed, polarity="not_detected", scope=scope))

    ranked = sorted(facts, key=lambda f: _FIT_ORDER.index(f["type"]) if f["type"] in _FIT_ORDER else 99)
    strong = [f for f in ranked if f["type"] in STRONG_FACTS]
    if strong:
        top = strong[:3]
        consequences, seen_inf = [], {}
        for f in top:
            inf = _OFFERS[f["type"]][1]
            if inf in seen_inf:
                seen_inf[inf]["fact_ids"].append(f["id"])
                continue
            seen_inf[inf] = {"fact_id": f["id"], "fact_ids": [f["id"]], "offer": _OFFERS[f["type"]][0],
                             "inference": inf, "kind": "inferred, not observed (hypothesis)"}
            consequences.append(seen_inf[inf])
        reason = ("Observed: " + " ".join(f["text"] for f in top) + " Inferred (not observed): " +
                  " ".join(c["inference"] + "." for c in consequences))
        return {"decision": "pass", "reason": reason, "offer": consequences[0]["offer"],
                "observed_facts": ranked, "inferred_consequences": consequences,
                "context_facts": context, "not_detected": not_detected,
                "uncertainty": uncertainties(pages), "observed_at": observed}
    if ranked:
        # Only weak or common signals: shown to the person, never a pass.
        return {"decision": "review",
                "reason": "Only weak or common signals were observed (" +
                          "; ".join(f["type"].replace("_", " ") for f in ranked) +
                          "). On their own they are not a reason for an offer; a person should look at the site.",
                "offer": "", "observed_facts": ranked, "inferred_consequences": [], "context_facts": context,
                "not_detected": not_detected, "uncertainty": uncertainties(pages), "observed_at": observed}
    if not_detected:
        return {"decision": "review",
                "reason": "No positively observed gap. Possible gaps were NOT DETECTED in the pages read "
                          "(" + "; ".join(f["text"] for f in not_detected) + ") but that does not show they are "
                          "absent; a person should look at the site before this counts as a reason.",
                "offer": "", "observed_facts": [], "inferred_consequences": [], "context_facts": context,
                "not_detected": not_detected, "uncertainty": uncertainties(pages), "observed_at": observed}
    return {"decision": "review", "reason": "No specific reason for an offer was found in the pages read.",
            "offer": "", "observed_facts": [], "inferred_consequences": [], "context_facts": context,
            "not_detected": [], "uncertainty": uncertainties(pages), "observed_at": observed}


def identity_facts(identity: dict) -> list[dict]:
    """Quote-backed identity facts a writer may use (name, a service, location)."""
    out: list[dict] = []
    if identity.get("decision") == "fail":
        return out
    name = identity.get("name") or {}
    if name.get("confirmed") and name.get("sources"):
        s = name["sources"][0]
        out.append(_fact("business_name", f"The site names the business “{name['confirmed']}” ({s['source']}).",
                         s["url"], name["confirmed"], identity.get("observed_at", ""), writer_usable=True))
    for s in (identity.get("services") or [])[:2]:
        out.append(_fact("service", f"The site mentions “{s['term']}”.", s["url"], s["quote"],
                         identity.get("observed_at", ""), writer_usable=True))
    loc = identity.get("location") or {}
    if loc.get("basis") not in (None, "", "none"):
        out.append(_fact("location", f"The site places the business in {loc.get('target')} "
                         f"({loc['basis'].replace('_', ' ')}).", loc["url"], loc["quote"],
                         identity.get("observed_at", ""), writer_usable=True))
    return out


def assess_contact(prospects: list[dict], allow_risky: bool) -> dict:
    """Contact eligibility under the existing email policy (nothing is changed)."""
    rows = []
    ok = 0
    for p in prospects:
        reason = email_block_reason(p.get("email", ""), p.get("email_status", ""), allow_risky)
        rows.append({"prospect_id": p.get("id"), "email": p.get("email"), "email_status": p.get("email_status"),
                     "status": p.get("status"), "eligible": not reason, "reason": reason})
        ok += 0 if reason else 1
    if not prospects:
        return {"decision": "review", "reason": "No contact found yet (contact search only runs after qualification).",
                "contacts": rows}
    if ok:
        return {"decision": "pass", "reason": f"{ok} contact(s) pass the email policy.", "contacts": rows}
    return {"decision": "blocked", "reason": "Every known contact is blocked by the email policy; they keep their "
            "existing verification results.", "contacts": rows}


def propose_state(identity: dict, fit: dict) -> str:
    if identity.get("decision") == "fail":
        return "disqualified"
    if identity.get("decision") == "pass" and fit.get("decision") == "pass":
        return "qualified"
    return "needs_review"
