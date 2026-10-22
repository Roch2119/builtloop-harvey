"""Run the qualification assessment for a small, named set of companies.

Never broad: the caller names the companies (or the stored pilot list). Reads
only the company's own site (bounded pages), saves every page attempt, then
saves three separate decisions. It sends nothing and searches for no contacts.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from harvey import assess
from harvey.site_evidence import SiteReader

MAX_COMPANIES = 25   # hard ceiling for one run; the pilot is 12


async def assess_company(state, config, company_id: str, reader: SiteReader | None = None,
                         pages: list[dict] | None = None) -> dict:
    """Fetch (unless `pages` given), assess, store. Returns a summary dict."""
    company = await state.get_company(company_id)
    if company is None:
        return {"ok": False, "error": "no such company"}
    qcfg = config.qualification
    run_id = uuid.uuid4().hex[:12]
    if pages is None:
        async with (reader or SiteReader(qcfg.max_pages, qcfg.request_delay_seconds)) as r:
            records = await r.read(company.domain)
        pages = [asdict(p) for p in records]
    await state.save_page_evidence(company_id, run_id, pages)

    cdict = {"name": company.name, "domain": company.domain, "industry": company.industry,
             "location": company.location}
    identity = assess.assess_identity(cdict, pages, qcfg)
    fit = assess.assess_offer_fit(cdict, pages, identity, datetime.now(timezone.utc).year)
    # The facts a person may approve for the writer: identity facts plus
    # offer-fit facts flagged writer_usable. Observed only, never inferences.
    fit["candidate_facts"] = assess.identity_facts(identity) + [
        f for f in fit.get("observed_facts", []) if f.get("writer_usable") and f.get("polarity") == "present"]
    prospects = [p.model_dump() for p in await state.get_contacts_for_company(company_id)]
    contact = assess.assess_contact(
        prospects, bool(getattr(config.channels.email, "send_to_risky", False)))
    contact["routes"] = assess.contact_routes(pages)     # published routes, for the owner to read
    proposed = assess.propose_state(identity, fit)
    gate = await state.record_assessment_outcome(
        company_id, run_id, identity, fit, contact, proposed, auto_qualify=qcfg.auto_qualify)
    return {"ok": True, "company_id": company_id, "run_id": run_id, "proposed": proposed,
            "state": gate, "identity": identity, "offer_fit": fit, "contact": contact}


async def run(state, config, company_ids: list[str]) -> list[dict]:
    if len(company_ids) > MAX_COMPANIES:
        raise ValueError(f"qualify runs at most {MAX_COMPANIES} companies at a time")
    out = []
    for cid in company_ids:
        out.append(await assess_company(state, config, cid))
    return out
