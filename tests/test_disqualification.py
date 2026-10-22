"""Company-level disqualification and email eligibility.

A person saying "no fit" about a business must stop every later email to
anyone at it, including follow-ups approved earlier. And the Writer must not
spend a model call on an address the sending policy would refuse.
"""

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from harvey import qualification as qual
from harvey.agents.writer import Writer
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import MIGRATIONS, StateManager
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, state,
)
from tests.test_writer_retry import GOOD, FakeBrain


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def writer_config(send_to_risky=False):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            provider="instantly", require_approval=False, send_to_risky=send_to_risky)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )


async def company(state, name="Whatclinic", domain="whatclinic.com"):
    return await state.add_company(Company(name=name, domain=domain))


async def contact(state, company_id, email, status="new", estatus="verified", name="Jane"):
    return await state.add_prospect(Prospect(
        company_id=company_id, first_name=name, last_name="Doe", title="Owner",
        company="Co", email=email, email_status=estatus,
        email_verified=(estatus == "verified"), status=status))


async def approved_follow_up(state, pid, to, step=2):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="following up", body="A plain note. Ok?",
        send_at=_now(), status="approved", campaign_id="c1", step=step)


# ── 1. Disqualification blocks an already approved follow-up ──


@pytest.mark.asyncio
async def test_no_fit_feedback_cancels_the_approved_follow_up(state):
    cid = await company(state)
    pid = await contact(state, cid, "info@whatclinic.com", status="contacted")
    item = await approved_follow_up(state, pid, "info@whatclinic.com")
    await state.add_feedback("contact", pid, "no fit")

    row = await state.get_outbox_item(item)
    assert row["status"] == "cancelled"
    assert row["error"].startswith(qual.COMPANY_DISQUALIFIED)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_the_final_send_check_blocks_even_if_nothing_was_cancelled(state):
    # The company is flagged by some other path and the approved item is still
    # sitting there: the Sender's last check must still refuse it.
    cid = await company(state)
    pid = await contact(state, cid, "info@whatclinic.com", status="contacted")
    item = await approved_follow_up(state, pid, "info@whatclinic.com")
    con = sqlite3.connect(state.db_path)
    con.execute("UPDATE companies SET disqualified_at = CURRENT_TIMESTAMP, "
                "disqualified_reason = 'directory' WHERE id = ?", (cid,))
    con.commit(); con.close()

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    row = await state.get_outbox_item(item)
    assert row["status"] == "cancelled" and "directory" in row["error"]


@pytest.mark.asyncio
async def test_disqualifying_keeps_the_history_and_other_companies_still_send(state):
    bad = await company(state)
    good = await company(state, "Eye and Brow Clinic", "eyeandbrowclinic.com")
    p_bad = await contact(state, bad, "info@whatclinic.com", status="contacted")
    p_good = await contact(state, good, "info@eyeandbrowclinic.com", status="contacted")
    await approved_follow_up(state, p_bad, "info@whatclinic.com")
    await approved_follow_up(state, p_good, "info@eyeandbrowclinic.com")
    await state.disqualify_company(bad, "directory, not a business", source="manual")

    # Nothing deleted: the company, the contact (still 'contacted') and the item.
    assert (await state.get_prospect(p_bad)).status == "contacted"
    assert len(await state.get_outbox(status="cancelled")) == 1
    log = [a for a in await _actions(state) if a["action_type"] == "company_disqualified"]
    assert log

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["to"] for m in provider.sent] == ["info@eyeandbrowclinic.com"]


async def _actions(state):
    con = sqlite3.connect(state.db_path); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM actions")]
    con.close()
    return rows


# ── 2. A new contact at a disqualified company cannot restart outreach ──


@pytest.mark.asyncio
async def test_a_new_contact_at_a_disqualified_company_cannot_restart_outreach(state):
    cid = await company(state)
    await contact(state, cid, "info@whatclinic.com", status="contacted")
    await state.disqualify_company(cid, "directory", source="manual")

    # A newly found, fully verified colleague at the same company...
    fresh = await contact(state, cid, "ceo@whatclinic.com", name="Sam")
    p = await state.get_prospect(fresh)
    assert p.status == "ineligible" and p.status_reason.startswith(qual.COMPANY_DISQUALIFIED)
    # ...and one filed under a duplicate company record or none at all.
    dup = await state.add_company(Company(name="Whatclinic UK", domain="www.whatclinic.com"))
    via_dup = await contact(state, dup, "sales@whatclinic.com", name="Pat")
    no_company = await contact(state, "", "press@whatclinic.com", name="Kim")
    for pid in (via_dup, no_company):
        assert (await state.get_prospect(pid)).status == "ineligible"

    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, writer_config(), None).run()
    assert brain.calls == 0
    assert await state.companies_needing_prospects() == []

    # A campaign someone staged for them anyway still puts nothing in the outbox.
    camp = await seed_campaign(state, [fresh, via_dup, no_company])
    await make_sender(state, FakeProvider())._stage_campaign_native(camp)
    assert await state.get_outbox() == []


@pytest.mark.asyncio
async def test_only_a_deliberate_requalify_reopens_the_company(state):
    cid = await company(state)
    pid = await contact(state, cid, "info@whatclinic.com", status="contacted")
    follow_up = await approved_follow_up(state, pid, "info@whatclinic.com")
    await state.add_feedback("contact", pid, "no fit")
    fresh = await contact(state, cid, "ceo@whatclinic.com", name="Sam")

    # Later praise, a re-run, or a Writer pass does not undo it.
    await state.add_feedback("contact", pid, "actually a great fit")
    await Writer(FakeBrain(reply=GOOD), state, writer_config(), None).run()
    assert (await state.get_prospect(fresh)).status == "ineligible"
    assert (await state.disqualification_for(company_id=cid)) is not None

    result = await state.requalify_company(cid, note="owner confirmed it is a clinic")
    assert result["changed"] and result["prospects_released"] == 1
    assert (await state.get_prospect(fresh)).status == "new"
    assert (await state.get_prospect(pid)).status == "contacted"
    # The follow-up cancelled earlier stays cancelled: a person decides again.
    assert (await state.get_outbox_item(follow_up))["status"] == "cancelled"
    assert any(a["action_type"] == "company_requalified" for a in await _actions(state))


@pytest.mark.asyncio
async def test_only_explicit_no_fit_on_a_business_disqualifies(state):
    cid = await company(state)
    pid = await contact(state, cid, "info@whatclinic.com")
    for text in ("great fit", "not a bad fit", "love their site", "no fit"):
        await state.add_feedback("campaign", cid, text)   # about a campaign, not them
    assert await state.disqualification_for(company_id=cid) is None
    for text in ("great fit", "not a bad fit", "needs a call first"):
        await state.add_feedback("contact", pid, text)
    assert await state.disqualification_for(company_id=cid) is None
    await state.add_feedback("company", cid, "Not a fit - it's a directory")
    assert (await state.disqualification_for(company_id=cid))["id"] == cid


@pytest.mark.asyncio
async def test_gmail_contacts_are_never_swept_up_by_a_shared_mail_domain(state):
    bad = await company(state, "Dirty Dogs", "dirtydogs.test")
    other = await company(state, "Fresh Paws", "freshpaws.test")
    p_bad = await contact(state, bad, "dirtydogs@gmail.com", name="A")
    p_other = await contact(state, other, "freshpaws@gmail.com", name="B")
    await state.disqualify_company(bad, "wrong fit", source="manual")
    assert (await state.get_prospect(p_bad)).status == "ineligible"
    assert (await state.get_prospect(p_other)).status == "new"


# ── 3. Blocked email statuses do not trigger drafting ──


@pytest.mark.asyncio
@pytest.mark.parametrize("estatus,expect", [
    ("invalid", "invalid"), ("guess", "pattern guess"),
    ("", "unknown"), ("risky", "catch-all"),
])
async def test_blocked_email_statuses_get_a_reason_and_no_draft(state, estatus, expect):
    cid = await company(state, "Acme Clinic", "acmeclinic.test")
    pid = await contact(state, cid, "info@acmeclinic.test", estatus=estatus)
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, writer_config(send_to_risky=False), None).run()
    p = await state.get_prospect(pid)
    assert brain.calls == 0
    assert p.status == "ineligible" and p.status_reason.startswith(qual.EMAIL_BLOCKED)
    assert expect in p.status_reason
    assert await state.get_campaigns_by_status("draft") == []


@pytest.mark.asyncio
async def test_a_catch_all_address_is_drafted_only_when_the_policy_allows_it(state):
    cid = await company(state, "Acme Clinic", "acmeclinic.test")
    pid = await contact(state, cid, "info@acmeclinic.test", estatus="risky")
    await Writer(FakeBrain(reply=GOOD), state, writer_config(False), None).run()
    assert (await state.get_prospect(pid)).status == "ineligible"
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, writer_config(True), None).run()   # policy changed
    assert brain.calls >= 1
    assert (await state.get_prospect(pid)).status == "queued"


@pytest.mark.asyncio
async def test_a_queued_prospect_the_gate_refused_is_not_left_queued(state):
    # Drafted while the old rule allowed catch-alls; the gate then refused it.
    cid = await company(state, "Acme Clinic", "acmeclinic.test")
    pid = await contact(state, cid, "info@acmeclinic.test", status="queued", estatus="risky")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="info@acmeclinic.test", subject="s", body="b",
        send_at=_now(), status="approved", campaign_id="c1", step=1)
    await state.update_outbox_item(
        item, status="failed", error="gate: email_status 'risky' is not deliverable")
    counts = await state.apply_eligibility_policy(allow_risky=False)
    assert counts["stuck_queued"] == 1
    p = await state.get_prospect(pid)
    assert p.status == "ineligible" and "catch-all" in p.status_reason


# ── 4. An otherwise eligible prospect still progresses ──


@pytest.mark.asyncio
async def test_an_eligible_prospect_is_drafted_staged_approved_and_sent(state):
    bad = await company(state)
    good = await company(state, "Eye and Brow Clinic", "eyeandbrowclinic.com")
    await contact(state, bad, "info@whatclinic.com", status="contacted")
    await state.disqualify_company(bad, "directory", source="manual")
    pid = await contact(state, good, "info@eyeandbrowclinic.com")

    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, writer_config(), None).run()
    assert brain.calls >= 1
    assert (await state.get_prospect(pid)).status == "queued"

    camp = (await state.get_campaigns_by_status("draft"))[0]
    sender = make_sender(state, FakeProvider())
    await sender._stage_campaign_native(camp)
    items = await state.get_outbox(status="pending_review")
    assert items and all(i["to_email"] == "info@eyeandbrowclinic.com" for i in items)
    await state.approve_outbox()
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["to"] for m in provider.sent] == ["info@eyeandbrowclinic.com"]


# ── Upgrade path: an existing v11 database gains the new columns ──


@pytest.mark.asyncio
async def test_an_existing_v11_database_upgrades_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    for script in MIGRATIONS[:11]:
        con.executescript(script)
    con.execute("PRAGMA user_version = 11")
    con.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Old Co', 'old.test')")
    con.commit(); con.close()

    state = StateManager(path)
    await state.init_db()
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    cols = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    assert {"disqualified_at", "disqualified_reason", "disqualified_source"} <= cols
    assert "status_reason" in {r[1] for r in con.execute("PRAGMA table_info(prospects)")}
    assert con.execute("SELECT disqualified_at FROM companies").fetchone()[0] is None
    con.close()
