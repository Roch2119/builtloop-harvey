"""Who Harvey may contact: company-level disqualification and email policy.

Pure helpers (no database): the rules live here, `state.py` applies them to
rows, and the Writer, Sender and Scout call them at every stage. The Sender's
final check is the one that cannot be skipped.

Reason strings start with a stable prefix so code can tell kinds apart:
  company_disqualified: ...   a person said the whole company is not a fit
  email_blocked: ...          the address is not allowed by the sending policy
"""

from __future__ import annotations

import re

COMPANY_DISQUALIFIED = "company_disqualified"
EMAIL_BLOCKED = "email_blocked"

# Explicit "this business is not a fit" wording in a comment. Deliberately
# narrow: a comment that merely mentions a business must never disqualify it.
_NO_FIT = re.compile(
    r"\b(?:no fit|not a (?:good |real )?fit|(?<!not a )(?<!not )(?:wrong|bad|poor) fit"
    r"|disqualif(?:y|ied|ication)|not a (?:real )?prospect)\b",
    re.IGNORECASE,
)
# Entity types whose comment is about one business (a campaign comment is not).
NO_FIT_ENTITY_TYPES = frozenset({"company", "contact", "prospect"})

# Mailbox providers: an address here says nothing about which company it is.
SHARED_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "msn.com",
})


def is_no_fit(comment: str | None) -> bool:
    """True when the comment explicitly says the business is not a fit."""
    return bool(_NO_FIT.search(comment or ""))


def domain_of(value: str | None) -> str:
    """Normalised site key from a URL, a domain or an email address."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    return v.strip(".")


def company_domains(domain: str | None, website: str | None = "") -> set[str]:
    """Every domain key that identifies a company record."""
    return {d for d in (domain_of(domain), domain_of(website)) if d}


def email_block_reason(email: str, email_status: str, allow_risky: bool) -> str:
    """Why this address may not be written to or sent to, or '' if it may.

    Mirrors what the Sender's final gate enforces, so the Writer never spends
    a draft on mail the gate would refuse.
    """
    status = (email_status or "").strip().lower()
    if not (email or "").strip():
        return f"{EMAIL_BLOCKED}: no email address"
    if status == "verified":
        return ""
    if status == "risky":
        if allow_risky:
            return ""
        return (f"{EMAIL_BLOCKED}: catch-all address (status 'risky'); "
                "channels.email.send_to_risky is off")
    if status == "invalid":
        return f"{EMAIL_BLOCKED}: address failed verification (invalid)"
    if status == "guess":
        return f"{EMAIL_BLOCKED}: address is a pattern guess, never verified"
    return f"{EMAIL_BLOCKED}: address verification status unknown ('{status or 'none'}')"


def disqualified_text(reason: str) -> str:
    """The stored reason, always carrying the stable prefix."""
    reason = (reason or "").strip() or "marked not a fit"
    return reason if reason.startswith(COMPANY_DISQUALIFIED) else f"{COMPANY_DISQUALIFIED}: {reason}"
