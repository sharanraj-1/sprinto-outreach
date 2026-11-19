"""Step 2 - Extract structured company data from cleaned page text.

Model-based extraction (L4 slide 13 option 3): survives any site layout.
Every field is nullable: null means "the page does not say", never a guess.
Each fact we might quote in an email carries the exact sentence it came from.
"""
from typing import Dict, List

from llm import LS, NB, NI, NS, call_json, obj

SCHEMA = obj({
    "company_name": NS,
    "product_summary": NS,                 # one sentence, plain English
    "who_buys_it": NS,                     # the buyer named on the site
    "business_model": {"type": "string", "enum": [
        "b2b_saas", "b2c_consumer_app", "ecommerce_store", "agency_or_services", "marketplace",
        "hardware_or_physical", "media_or_content", "nonprofit_or_government", "unclear"]},
    "business_model_evidence": NS,         # the exact phrase that decided it
    "is_b2b_saas": NB,                     # true / false / null if unclear
    "certifications_held": LS,             # ONLY ones the company says it already HAS
    "certifications_in_progress": LS,      # "SOC 2 in progress", "working towards ISO"
    "has_trust_or_security_page": NB,
    "funding_stage": NS,                   # "Seed", "Series A"... only if stated
    "funding_evidence": NS,
    "headcount_hint": NS,                  # "team of 40", "50-200 employees"
    "open_roles_count": NI,
    "security_or_compliance_roles_open": LS,
    "enterprise_tier_on_pricing": NB,
    "named_enterprise_customers": LS,
    "regulated_industries_served": LS,     # fintech, health, gov...
    "compliance_pressure_signals": LS,     # quotes that show buyers asking for security/compliance
    "hook_facts": LS,                      # specific, recent, quotable facts usable as an email opener
})

SYSTEM = """You extract facts about a company from its own website text for a B2B sales
research system. Rules:
- Only state what the text says. If the text does not say it, use null (or [] for lists).
- certifications_held: only certifications the company states it currently HOLDS
  (e.g. "We are SOC 2 Type II certified"). A logo of a framework they help OTHERS with
  does not count. "In progress" goes in certifications_in_progress.
- business_model: what the company SELLS and to WHOM.
    b2b_saas = software businesses subscribe to (incl. APIs, dev tools, open-core with paid cloud).
    agency_or_services = they sell people's time (dev shops, consultancies, outsourcing, staffing).
    ecommerce_store = they sell physical/digital goods to shoppers. marketplace = they match two sides.
    b2c_consumer_app = an app for individuals. Use "unclear" only if the text truly does not say.
  Quote the deciding phrase in business_model_evidence.
- is_b2b_saas: true only when business_model is b2b_saas; false for any other model; null if unclear.
- You also get DEEP SCAN data (meta tags, JSON-LD, embedded app data, third-party tools, job board,
  badge images, keyword hits). Use it as evidence, but a keyword hit inside JavaScript is only a hint:
  certifications_held still needs a clear statement that THIS company holds it.
- hook_facts and compliance_pressure_signals: copy short exact phrases from the text,
  each prefixed with the page it came from, e.g. "[/careers] Hiring a Security Engineer".
- open_roles_count: count distinct job titles listed, or null if no job list is present."""


def extract(pages: List[Dict], deep: str = "") -> Dict:
    joined = "\n\n".join(
        f"===== PAGE {p['requested']} =====\n{p['text']}" for p in pages if p.get("text")
    )
    if deep:
        joined += "\n\n===== DEEP SCAN (HTML structure, JS, CSS, sitemap, job board) =====\n" + deep
    if not joined.strip():
        return {}
    return call_json(SYSTEM, joined, SCHEMA, effort="low")


def merge(old: Dict, new: Dict) -> Dict:
    """Newer pages fill gaps; they never overwrite a known value with null. Lists union."""
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, list):
            out[k] = list(dict.fromkeys((old.get(k) or []) + v))
        elif v is not None and old.get(k) in (None, ""):
            out[k] = v
        elif k not in out:
            out[k] = v
    return out
