"""Free-mail inbox fallback and extra directory filters."""

from types import SimpleNamespace

import pytest

import harvey.collectors.discover as D
from tests.test_company_inbox import make_scout


def config(accept=True):
    email = SimpleNamespace(accept_freemail_inbox=accept, provider="gmail", require_approval=True)
    return SimpleNamespace(channels=SimpleNamespace(email=email))


# ── free-mail fallback ──

@pytest.mark.asyncio
async def test_mailto_freemail_address_is_found():
    pages = {"https://acme.com": '<a href="mailto:HappyPaws.Grooming@Gmail.com">Email</a>'}
    assert await make_scout(pages)._find_freemail_address("acme.com") == "happypaws.grooming@gmail.com"


@pytest.mark.asyncio
async def test_plain_text_freemail_on_contact_page_is_found():
    pages = {"https://acme.com": "<p>hi</p>", "https://acme.com/contact": "Call or write: amysdogs@hotmail.com"}
    assert await make_scout(pages)._find_freemail_address("acme.com") == "amysdogs@hotmail.com"


@pytest.mark.asyncio
async def test_mailto_is_preferred_over_loose_text():
    pages = {
        "https://acme.com": '<a href="mailto:real@gmail.com">x</a> other@gmail.com',
    }
    assert await make_scout(pages)._find_freemail_address("acme.com") == "real@gmail.com"


@pytest.mark.parametrize("addr", ["example@gmail.com", "yourname@gmail.com", "ab@gmail.com", "noreply@gmail.com"])
@pytest.mark.asyncio
async def test_placeholder_freemail_is_ignored(addr):
    pages = {"https://acme.com": f'<a href="mailto:{addr}">x</a>'}
    assert await make_scout(pages)._find_freemail_address("acme.com") == ""


@pytest.mark.asyncio
async def test_non_freemail_domains_are_not_taken_by_this_fallback():
    pages = {"https://acme.com": '<a href="mailto:jane@otherfirm.com">x</a>'}
    assert await make_scout(pages)._find_freemail_address("acme.com") == ""


@pytest.mark.parametrize("domain", ["acme.co.uk", "salon.fr", "studio.de", "clinic.ie"])
@pytest.mark.asyncio
async def test_strict_consent_country_domains_are_skipped(domain):
    pages = {"https://acme.com": '<a href="mailto:owner@gmail.com">x</a>'}
    assert await make_scout(pages)._find_freemail_address(domain) == ""


# ── directories ──

@pytest.mark.parametrize("domain", [
    "whatclinic.com", "www.treatwell.co.uk", "fresha.com", "booksy.com", "yell.com",
    "checkatrade.com", "rover.com", "care.com", "vagaro.com", "bark.com",
])
def test_booking_marketplaces_and_directories_are_junk(domain):
    assert D.is_junk(domain)


@pytest.mark.parametrize("domain", ["carebears-grooming.com", "barkavenuepets.com", "yellowbirdcleaning.com"])
def test_lookalike_business_names_survive(domain):
    assert not D.is_junk(domain)
