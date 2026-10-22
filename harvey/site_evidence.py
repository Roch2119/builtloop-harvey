"""Read a business's own website into source-backed evidence.

Fetches the homepage and a small, bounded set of relevant pages (About,
Services, Contact, Booking) that the homepage itself links to, and records for
each page what it actually said: title, headline, structured data, a text
excerpt, quotes that match booking/price wording, and what went wrong.

Everything here is a record of what was seen, with the URL and time. Nothing
here decides anything: that is `harvey/assess.py`. A page that failed, was
redirected somewhere else, or needs JavaScript to show its content is recorded
as such, so "not seen" is never mistaken for "not there".

Free: plain HTTP GETs through `harvey.public_http` (the same boundary Harvey's
profile collector uses). No paid provider, no model call.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from harvey.collectors.profile import TIMEOUT, USER_AGENTS
from harvey.public_http import public_http_client
from harvey.qualification import domain_of

MAX_BYTES = 1_500_000
TEXT_EXCERPT_CHARS = 3000
MAX_LINKS = 120
KINDS = ("about", "services", "contact", "booking", "shop", "events")

# What a link says (path or text) about the page behind it.
_KIND_WORDS = {
    "about": ("about", "our-story", "our story", "who-we-are", "who we are", "meet-", "meet the", "the-team", "our team"),
    "services": ("service", "treatment", "menu", "price", "pricing", "what-we-do", "what we do",
                 "grooming", "training", "cleaning", "packages", "rates", "classes"),
    "contact": ("contact", "find-us", "find us", "visit", "location", "get-in-touch", "get in touch"),
    "booking": ("book", "appointment", "reserve", "schedule", "consultation"),
    # Secondary revenue lines (courses, memberships, events) often hold the
    # most specific evidence, so they are read too (still bounded).
    "shop": ("shop", "store", "course", "academy", "membership", "packages", "gift-card", "gift card"),
    "events": ("event", "workshop", "masterclass", "calendar", "schedule-of"),
}
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3)(\?|$)", re.I)

# Third-party booking tools: a link to one is a positive fact about how they
# take bookings today.
BOOKING_HOSTS = (
    "fresha.com", "treatwell.", "square.site", "squareup.com", "acuityscheduling.com", "as.me",
    "calendly.com", "glossgenius.com", "vagaro.com", "mindbodyonline.com", "setmore.com",
    "booksy.com", "gingrapp.com", "gingr.app", "moego.pet", "getjobber.com", "housecallpro.com",
    "timetap.com", "simplybook.", "schedulicity.com", "styleseat.com", "phorest.com",
    "pabau.com", "cliniko.com", "jane.app", "zenoti.com", "nookal.com", "bookwhen.com",
    "zenmaid.com", "launch27.com", "bookingkoala.com", "maidcentral.com", "daysmartsoftware.com",
    "mangomint.com", "boulevard.io", "tidycal.com", "cal.com", "youcanbook.me",
)

# Wording quoted from the page itself (never inferred). Each: (type, regex).
_QUOTE_PATTERNS = (
    ("call_to_book", re.compile(
        r"(?:call|phone|text|whatsapp|ring)\s+(?:us\s+)?(?:now\s+)?to\s+(?:book|schedule|arrange|make an? (?:appointment|booking))"
        r"|book\s+(?:by|via|over the|through)\s+(?:phone|calling|text|telephone)"
        r"|to\s+book\s+(?:an?\s+)?(?:appointment|consultation|visit|session|service)?[,:\s]*(?:please\s+)?(?:call|phone|text|ring|whatsapp)",
        re.I)),
    ("social_booking", re.compile(
        r"(?:dm|message|msg)\s+(?:us\s+)?(?:on\s+\w+\s+)?to\s+book"
        r"|book\s+(?:via|through|on)\s+(?:instagram|facebook|whatsapp|messenger)",
        re.I)),
    ("price_on_request", re.compile(
        r"prices?\s+(?:are\s+)?(?:on|upon)\s+request"
        r"|(?:contact|call|message|email)\s+(?:us\s+)?for\s+(?:a\s+)?(?:pricing|prices|rates)",
        re.I)),
)
_PRICE_RE = re.compile(r"(?:[£$€]\s?\d[\d,.]*|\bfrom\s+[£$€]?\d[\d,.]+|\d[\d,.]*\s?(?:gbp|usd|eur)\b)", re.I)
_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:\(c\)\s*)?((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?", re.I)
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{8,16}\d)")
_BUILT_BY_RE = re.compile(
    r"(?:website|web\s?site|site|design(?:ed)?|built|developed|powered)\s+(?:design\s+)?by\s+([A-Z][\w&.\- ]{2,40})", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _snippet(text: str, start: int, end: int, pad: int = 70) -> str:
    """Text around a match, cut on word boundaries so a quote never starts or
    ends in the middle of a word."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    while a > 0 and a < start and not text[a - 1].isspace():
        a += 1
    while b < len(text) and b > end and not text[b].isspace():
        b -= 1
    return _clean(text[a:b])


@dataclass
class PageRecord:
    kind: str                       # home | about | services | contact | booking
    requested_url: str
    final_url: str = ""
    http_status: int = 0
    redirected_off_domain: bool = False
    needs_rendering: bool = False
    error: str = ""
    title: str = ""
    meta_description: str = ""
    h1: str = ""
    headings: list[str] = field(default_factory=list)
    site_name: str = ""             # og:site_name
    jsonld: list[dict] = field(default_factory=list)
    text_excerpt: str = ""
    text_len: int = 0
    has_viewport: bool = False
    generator: str = ""
    copyright_year: int = 0
    copyright_quote: str = ""
    phones: list[str] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)       # {type, quote}
    booking_links: list[str] = field(default_factory=list)
    built_by: str = ""
    booking_popup: bool = False                            # a "book" button that opens an on-page form
    dated_items: list[dict] = field(default_factory=list)  # {date_text, year, month, quote, price, offer_button}
    access_challenge: bool = False                         # served a bot-check page, not the site
    price_text_seen: bool = False
    links: list[dict] = field(default_factory=list)        # {href, text, same_site}
    fetched_at: str = ""

    def to_row(self) -> dict:
        d = self.__dict__.copy()
        return d


def _jsonld_entries(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if "@graph" in node:
                walk(node["@graph"])
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            types = [str(t) for t in types if t]
            if types:
                addr = node.get("address")
                if isinstance(addr, str):
                    addr = {"streetAddress": addr}
                out.append({
                    "type": types,
                    "name": str(node.get("name") or "")[:120],
                    "address": {k: str(v)[:100] for k, v in (addr or {}).items()
                                if k in ("streetAddress", "addressLocality", "addressRegion",
                                         "postalCode", "addressCountry")} if isinstance(addr, dict) else {},
                    "telephone": str(node.get("telephone") or "")[:40],
                    "area_served": json.dumps(node.get("areaServed"))[:200] if node.get("areaServed") else "",
                    "url": str(node.get("url") or "")[:200],
                })

    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            walk(json.loads(tag.string or tag.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return out[:12]


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_DATE_RE = re.compile(
    r"(?:\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?(?:st|nd|rd|th)?\s+)?\b"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(20\d{2})\b", re.I)
_OFFER_BUTTON_RE = re.compile(
    r"add to cart|buy now|book now|book your (?:place|seat|spot|ticket)s?|enrol(?:l)? now|register now|"
    r"get tickets|buy tickets|sign up now", re.I)
_CHALLENGE_RE = re.compile(
    r"just a moment|checking your browser|attention required|cf-chl|cf_chl|enable javascript and cookies|"
    r"verify you are human|captcha|access denied", re.I)


def _month_number(name: str) -> int:
    key = name.lower()[:3]
    return next((i + 1 for i, m in enumerate(_MONTHS) if m.startswith(key)), 0)


def _dated_items(text: str) -> list[dict]:
    """Dated things shown with a purchase/booking button: "15 January 2023 ...
    ADD TO CART". Recorded as seen; whether they are past is decided later."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _DATE_RE.finditer(text):
        tail = text[m.start(): m.end() + 170]
        btn = _OFFER_BUTTON_RE.search(tail, m.end() - m.start())
        if not btn:
            continue
        quote = _clean(tail[: btn.end()])
        if len(quote) > 190 or quote in seen:
            continue
        seen.add(quote)
        price = _PRICE_RE.search(quote)
        out.append({"date_text": _clean(m.group(0)), "year": int(m.group(2)),
                    "month": _month_number(m.group(1)), "quote": quote,
                    "price": price.group(0) if price else "", "offer_button": _clean(btn.group(0))})
        if len(out) >= 8:
            break
    return out


def extract_page(html: str, final_url: str, kind: str, requested_url: str = "",
                 http_status: int = 200, base_host: str = "") -> PageRecord:
    """Turn fetched HTML into a PageRecord. Pure: no network."""
    rec = PageRecord(kind=kind, requested_url=requested_url or final_url, final_url=final_url,
                     http_status=http_status, fetched_at=now_iso())
    if base_host and domain_of(final_url) != domain_of(base_host):
        rec.redirected_off_domain = True
    soup = BeautifulSoup(html or "", "html.parser")

    rec.title = _clean(soup.title.get_text()) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    rec.meta_description = _clean(md.get("content", "")) if md else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    rec.site_name = _clean(og.get("content", "")) if og else ""
    gen = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    rec.generator = _clean(gen.get("content", ""))[:80] if gen else ""
    rec.has_viewport = bool(soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}))
    rec.jsonld = _jsonld_entries(soup)

    h1 = soup.find("h1")
    rec.h1 = _clean(h1.get_text()) if h1 else ""
    rec.headings = [_clean(h.get_text()) for h in soup.find_all(["h2", "h3"])][:20]
    rec.headings = [h for h in rec.headings if h][:14]

    script_count = len(soup.find_all("script"))
    noscript_text = " ".join(n.get_text(" ") for n in soup.find_all("noscript")).lower()
    links = []
    base = final_url or requested_url
    for a in soup.find_all("a", href=True)[:600]:
        href = urljoin(base, a["href"].strip())
        if not href.startswith(("http://", "https://")):
            continue
        host = domain_of(href)
        if any(h in host for h in BOOKING_HOSTS):
            if href not in rec.booking_links:
                rec.booking_links.append(href[:200])
        links.append({"href": href[:300], "text": _clean(a.get_text())[:80],
                      "same_site": host == domain_of(base)})
    rec.links = links[:MAX_LINKS]
    # Booking tools embedded in a frame or loaded by script are positive facts
    # too (a link is not the only way a site takes bookings).
    for tag in soup.find_all(["iframe", "script"]):
        src = urljoin(base, (tag.get("src") or tag.get("data-src") or "").strip())
        if src.startswith(("http://", "https://")) and any(h in domain_of(src) for h in BOOKING_HOSTS):
            if src not in rec.booking_links:
                rec.booking_links.append(src[:200])
    for a in soup.find_all(["a", "button"]):
        label = _clean(a.get_text())
        href = (a.get("href") or "")
        if re.search(r"\bbook\b|appointment|reserve", label, re.I) and (
                href.startswith("#") and len(href) > 1 or a.name == "button"):
            rec.booking_popup = True
            break

    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = _clean(soup.get_text(" "))
    rec.text_len = len(text)
    rec.text_excerpt = text[:TEXT_EXCERPT_CHARS]
    root_shell = bool(soup.find(id=re.compile(r"^(root|app|__next|__nuxt)$")))
    rec.needs_rendering = (
        rec.text_len < 300 and (script_count >= 3 or root_shell)
    ) or ("enable javascript" in noscript_text and rec.text_len < 800)

    for qtype, pattern in _QUOTE_PATTERNS:
        for m in pattern.finditer(text):
            rec.quotes.append({"type": qtype, "quote": _snippet(text, m.start(), m.end())})
            break
    rec.price_text_seen = bool(_PRICE_RE.search(text))
    rec.dated_items = _dated_items(text)
    years = [(int(m.group(2) or m.group(1)), _snippet(text, m.start(), m.end(), 20))
             for m in _COPYRIGHT_RE.finditer(text)]
    if years:
        rec.copyright_year, rec.copyright_quote = max(years)
    rec.phones = list(dict.fromkeys(_clean(m.group(0)) for m in _PHONE_RE.finditer(text)))[:4]
    bm = _BUILT_BY_RE.search(text[-1500:])
    rec.built_by = _clean(bm.group(1))[:50] if bm else ""
    return rec


def select_pages(home: PageRecord, max_pages: int) -> list[tuple[str, str]]:
    """Which pages to read next: (kind, url), at most max_pages - 1 of them.

    Only links the homepage itself offers, one per kind, in a fixed order.
    Nothing is guessed (/about, /services are not tried blind).
    """
    chosen: list[tuple[str, str]] = []
    seen = {home.final_url.rstrip("/")}
    for kind in KINDS:
        for link in home.links:
            if not link["same_site"] or _SKIP_EXT.search(link["href"]):
                continue
            url = link["href"].split("#", 1)[0]
            if not url or url.rstrip("/") in seen:
                continue
            haystack = (urlparse(url).path + " " + link["text"]).lower()
            if any(w in haystack for w in _KIND_WORDS[kind]):
                chosen.append((kind, url))
                seen.add(url.rstrip("/"))
                break
        if len(chosen) >= max(0, max_pages - 1):
            break
    return chosen


class SiteReader:
    """Reads one business's site. Every attempt becomes a PageRecord."""

    def __init__(self, max_pages: int = 5, delay: float = 1.0, client: httpx.AsyncClient | None = None):
        self.max_pages = max(1, int(max_pages))
        self.delay = max(0.0, float(delay))
        self._client = client
        self._own_client = client is None

    async def __aenter__(self):
        if self._client is None:
            self._client = public_http_client(
                timeout=TIMEOUT, headers={"User-Agent": USER_AGENTS[0], "Accept": "text/html,*/*;q=0.8"})
        return self

    async def __aexit__(self, *exc):
        if self._own_client and self._client is not None:
            await self._client.aclose()

    async def _get(self, url: str) -> tuple[httpx.Response | None, str]:
        try:
            resp = await self._client.get(url)
        except (httpx.HTTPError, ValueError) as e:
            return None, f"{type(e).__name__}: {str(e)[:120]}"
        return resp, ""

    async def read(self, domain: str) -> list[PageRecord]:
        """Homepage, then up to max_pages-1 relevant linked pages."""
        records: list[PageRecord] = []
        home: PageRecord | None = None
        for base in (f"https://{domain}", f"https://www.{domain}",
                     f"http://{domain}", f"http://www.{domain}"):
            resp, err = await self._get(base)
            if resp is None:
                records.append(PageRecord(kind="home", requested_url=base, error=err, fetched_at=now_iso()))
                continue
            rec = self._record(resp, "home", base, domain)
            records.append(rec)
            if not rec.error:
                home = rec
                break
            await asyncio.sleep(self.delay)
        if home is None:
            return records
        for kind, url in select_pages(home, self.max_pages):
            await asyncio.sleep(self.delay)
            resp, err = await self._get(url)
            if resp is None:
                records.append(PageRecord(kind=kind, requested_url=url, error=err, fetched_at=now_iso()))
                continue
            records.append(self._record(resp, kind, url, domain))
        return records

    @staticmethod
    def _record(resp: httpx.Response, kind: str, requested: str, domain: str) -> PageRecord:
        final = str(resp.url)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            challenged = resp.status_code in (403, 429, 503) and (
                resp.headers.get("cf-mitigated", "").lower() == "challenge"
                or bool(_CHALLENGE_RE.search(resp.text[:4000] if resp.text else "")))
            return PageRecord(kind=kind, requested_url=requested, final_url=final,
                              http_status=resp.status_code, access_challenge=challenged,
                              error=(f"HTTP {resp.status_code}: the site served a bot-check page instead of its content"
                                     if challenged else f"HTTP {resp.status_code}"),
                              fetched_at=now_iso(),
                              redirected_off_domain=domain_of(final) != domain_of(domain))
        if "html" not in ctype and "text" not in ctype:
            return PageRecord(kind=kind, requested_url=requested, final_url=final, http_status=200,
                              error=f"not a web page ({ctype or 'no content type'})", fetched_at=now_iso())
        body = resp.text[:MAX_BYTES]
        return extract_page(body, final, kind, requested_url=requested,
                            http_status=200, base_host=domain)
