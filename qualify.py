"""Step 3 - Qualify against Sprinto's ICP: gate -> research loop -> verification -> judgment.

L4 design rule, applied literally:
  "The model makes the judgment. Your code decides whether it has enough to make it."

  1. Homepage + DEEP SCAN (deepscan.py): visible text AND the HTML structure, JS, CSS,
     sitemap and public job board.
  2. CHECKPOINT 1 - B2B SaaS gate, right after the homepage. Not B2B SaaS -> stop immediately,
     no further pages fetched, no tokens spent on judging.
  3. Research loop: the CODE checks which Evidence Map questions are open and fetches the
     NAMED source for each. Cap: MAX_EXTRA_FETCHES beyond the homepage.
  4. VERIFICATION PHASE: its own small budget (VERIFY_MAX_FETCHES). Fetches the trust centre /
     security pages the deep scan discovered, then re-checks every checklist item against ALL
     sources (page text, JSON-LD, JS bundles, CSS badges, job board, tools) and flags conflicts.
  5. Only then does the MODEL judge fit and "why now"; the code applies the threshold.
"""
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import config
from deepscan import find_job_board, scan, summary_for_llm
from extract import extract, merge
from llm import I, LS, S, call_json, obj
from scrape import discover_path, fetch_page

COMPETITOR_TOOLS = ("Vanta", "Drata", "Secureframe", "SafeBase", "Conveyor", "Whistic", "TrustCloud", "Sprinto")


# ---------- helpers ----------------------------------------------------------

def _has_disqualifying_cert(f: Dict) -> Optional[str]:
    for c in f.get("certifications_held") or []:
        if any(d in c.lower() for d in config.DISQUALIFYING_CERTS):
            return c
    return None


def _merge_facts(old: Dict, new: Dict) -> Dict:
    out = merge(old, new)
    # a later page may settle a model the homepage left "unclear"
    if old.get("business_model") in (None, "unclear") and new.get("business_model") not in (None, "unclear"):
        out["business_model"] = new["business_model"]
        out["business_model_evidence"] = new.get("business_model_evidence")
        out["is_b2b_saas"] = new.get("is_b2b_saas")
    return out


def _apply_signals(facts: Dict, sig: Dict) -> Dict:
    """Hard data from the deep scan overrides model guesses (code, not model)."""
    j = sig.get("jobs")
    if j:
        facts["open_roles_count"] = j["count"]
        facts["security_or_compliance_roles_open"] = list(dict.fromkeys(
            (facts.get("security_or_compliance_roles_open") or []) + j["security_compliance_roles"]))
    return facts


def answered(question: str, f: Dict) -> bool:
    if question == "already_certified":
        return bool(f.get("certifications_held"))
    if question == "b2b_saas":
        return f.get("business_model") not in (None, "unclear") or f.get("is_b2b_saas") is not None
    if question == "right_stage":
        return (f.get("funding_stage") is not None
                or f.get("open_roles_count") is not None
                or f.get("enterprise_tier_on_pricing") is not None)
    raise ValueError(question)


def b2b_gate(f: Dict, sig: Dict) -> Tuple[str, str]:
    """CHECKPOINT 1. Returns ('fail'|'pass'|'unclear', reason)."""
    model = f.get("business_model") or "unclear"
    ev = f.get("business_model_evidence") or ""
    shop = "ecommerce" in sig.get("vendors", {}) and sig.get("keyword_hits", {}).get("add_to_cart")
    if model in config.NOT_B2B_MODELS or f.get("is_b2b_saas") is False:
        label = {"b2c_consumer_app": "a consumer app", "ecommerce_store": "an online store",
                 "agency_or_services": "an agency / services firm", "marketplace": "a marketplace",
                 "hardware_or_physical": "a hardware / physical-product company", "media_or_content": "a media / content business",
                 "nonprofit_or_government": "a non-profit / government body"}.get(model, "not a software subscription")
        return "fail", f"NOT B2B SaaS - this is {label}" + (f" (\"{ev[:120]}\")" if ev else "") + "."
    if shop and model != "b2b_saas":
        return "fail", f"NOT B2B SaaS - online store ({', '.join(sig['vendors']['ecommerce'])} + shopping-cart wording)."
    if model == "b2b_saas" or f.get("is_b2b_saas"):
        return "pass", f"B2B SaaS" + (f": \"{ev[:140]}\"" if ev else "")
    return "unclear", "business model not clear from the homepage"


def short_circuit(f: Dict, sig: Dict) -> Optional[Tuple[str, str]]:
    g, why = b2b_gate(f, sig)
    if g == "fail":
        return "disqualified", why
    cert = _has_disqualifying_cert(f)
    if cert:
        return "disqualified", f"Already holds '{cert}' - Sprinto's core wedge is already bought."
    stage = (f.get("funding_stage") or "").lower()
    if any(s in stage for s in config.TOO_LATE_STAGES):
        return "disqualified", f"Stage '{f['funding_stage']}' is past Series C - usually has an in-house GRC team."
    return None


def stage_fits(f: Dict) -> Tuple[bool, str]:
    """'Sufficient when' rule from the Evidence Map: any ONE of these counts."""
    stage = (f.get("funding_stage") or "").lower()
    if any(s in stage for s in config.FITTING_STAGES):
        return True, f"funding stage stated: {f['funding_stage']}"
    if (f.get("open_roles_count") or 0) >= config.MIN_OPEN_ROLES_FOR_STAGE:
        return True, f"{f['open_roles_count']} open roles (>= {config.MIN_OPEN_ROLES_FOR_STAGE}) - growing team"
    if f.get("enterprise_tier_on_pricing"):
        return True, "enterprise tier on pricing page - selling upmarket"
    return False, "no stage evidence met the threshold"


def _fetch(domain: str, target: str, pages: List[Dict], facts: Dict, sig: Dict, deep: str,
           log: List[str], tag: str) -> Tuple[Dict, Dict]:
    page = fetch_page(domain, target)
    pages.append(page)
    log.append(f"{tag} -> fetch {page['requested']} -> {page['status']}, {page['words']} words"
               + (f" ({page['note']})" if page.get("note") else ""))
    if not sig.get("jobs") and page.get("html"):
        jobs = find_job_board(page["html"], log)
        if jobs:
            sig["jobs"], sig["scanned"]["job_board"] = jobs, True
    if page.get("text"):
        facts = _merge_facts(facts, extract([page]))
    return _apply_signals(facts, sig), page


# ---------- research: homepage, deep scan, gate, loop --------------------------

def research(domain: str, log: List[str]) -> Tuple[Dict, List[Dict], Dict, set]:
    home = fetch_page(domain)
    pages = [home]
    log.append(f"fetch homepage {home['url']} -> {home['status']}, {home['words']} words"
               + (f" ({home['note']})" if home.get("note") else ""))
    if home["status"] != 200:
        return {}, pages, {}, set()

    sig = scan(domain, home["html"], home["url"], log)
    deep = summary_for_llm(sig)
    facts = _apply_signals(extract(pages, deep), sig)
    tried = {home["requested"].rstrip("/")}

    g, why = b2b_gate(facts, sig)
    log.append(f"CHECKPOINT 1 (B2B SaaS) after homepage: {g.upper()} - {why}")
    if g == "fail":
        return facts, pages, sig, tried          # stop immediately: nothing else fetched

    extra, used = 0, {q["question"]: 0 for q in config.EVIDENCE_MAP}
    while True:
        sc = short_circuit(facts, sig)
        if sc:
            log.append(f"short-circuit: {sc[1]}")
            break
        gaps = [q for q in config.EVIDENCE_MAP
                if not answered(q["question"], facts) and used[q["question"]] < q["limit"]]
        if not gaps:
            log.append("all Evidence Map questions answered or out of tries - stop fetching")
            break
        if extra >= config.MAX_EXTRA_FETCHES:
            log.append(f"hit fetch cap ({config.MAX_EXTRA_FETCHES}) - stop, decide with what we have")
            break

        q = gaps[0]
        target = None
        # prefer a page the sitemap proves exists, then the Evidence Map's guesses
        known = [urlparse(u).path.rstrip("/") for u in sig.get("sitemap_pages", [])]
        candidates = [c for c in q["where"] if c.rstrip("/") in known] + q["where"]
        for cand in candidates:
            if cand in tried:
                continue
            resolved = discover_path(home["links"], home["url"], [cand])
            tried.add(cand)
            if resolved and (resolved == cand or resolved.rstrip("/") not in tried):
                target = resolved
                break
        used[q["question"]] += 1
        if not target:
            log.append(f"gap '{q['question']}': no untried source left")
            continue
        tried.add(target.rstrip("/"))
        facts, page = _fetch(domain, target, pages, facts, sig, deep, log, f"gap '{q['question']}'")
        extra += 1
        if page["status"] != 200:
            used[q["question"]] -= 1     # a dead page answered nothing; try the next source
        if q["question"] == "b2b_saas":
            g, why = b2b_gate(facts, sig)
            log.append(f"CHECKPOINT 1 (B2B SaaS) re-check: {g.upper()} - {why}")
    return facts, pages, sig, tried


# ---------- verification phase -------------------------------------------------

def verify(domain: str, facts: Dict, pages: List[Dict], sig: Dict, tried: set, log: List[str]) -> Tuple[Dict, List[Dict]]:
    """Second pass: fetch what the deep scan discovered, then cross-check every checklist item."""
    targets = []
    if sig.get("trust_center_url"):
        targets.append(sig["trust_center_url"])
    for u in sig.get("sitemap_pages", []):
        path = urlparse(u).path.lower()
        if re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(security|trust|compliance|soc-?2|iso-?27001)(?:[/-]|$)", path):
            targets.append(u)
    fetched = 0
    for t in targets:
        if fetched >= config.VERIFY_MAX_FETCHES:
            break
        key = t.rstrip("/")
        if key in tried or urlparse(t).path.rstrip("/") in tried:
            continue
        tried.add(key)
        facts, _ = _fetch(domain, t, pages, facts, sig, "", log, "VERIFY")
        fetched += 1
    if not fetched:
        log.append("VERIFY: no new trust/security pages to fetch - cross-checking collected data")

    hits = sig.get("keyword_hits", {})
    vendors = sig.get("vendors", {})
    checks: List[Dict] = []

    # 1. B2B SaaS
    g, why = b2b_gate(facts, sig)
    b2b_support = [x for x in (
        "JSON-LD says BusinessApplication" if any("BusinessApplication" in s for s in sig.get("structured_data", [])) else "",
        "has 'book a demo / contact sales'" if hits.get("book_demo") else "",
        "has free trial / sign-up" if hits.get("free_trial") else "",
        ("B2B tools: " + ", ".join(vendors["b2b"][:4])) if vendors.get("b2b") else "",
    ) if x]
    checks.append({"id": "b2b_saas", "label": "Is a B2B SaaS company",
                   "status": {"pass": "pass", "fail": "fail", "unclear": "unknown"}[g],
                   "detail": why, "support": b2b_support})

    # 2. Not already certified
    cert = _has_disqualifying_cert(facts)
    competitor = [v for v in vendors.get("compliance", []) if any(c in v for c in COMPETITOR_TOOLS)]
    hints = []
    if sig.get("badge_images"):
        hints.append("badge images: " + ", ".join(sig["badge_images"][:3]))
    if hits.get("soc2") or hits.get("iso27001"):
        hints.append("SOC 2 / ISO 27001 wording found: " + (hits.get("soc2") or hits.get("iso27001"))[0][:140])
    if cert:
        checks.append({"id": "not_certified", "label": "Not already SOC 2 / ISO 27001 certified", "status": "fail",
                       "detail": f"Site states: {cert}", "support": hints})
    elif any("Sprinto" in c for c in competitor):
        checks.append({"id": "not_certified", "label": "Not already SOC 2 / ISO 27001 certified", "status": "fail",
                       "detail": "Sprinto is already in their code - looks like an existing Sprinto customer. Do not cold-email.",
                       "support": hints})
    elif competitor:
        checks.append({"id": "not_certified", "label": "Not already SOC 2 / ISO 27001 certified", "status": "fail",
                       "detail": f"Runs a compliance programme on {', '.join(competitor)} - already bought a Sprinto alternative.",
                       "support": hints})
    elif hints:
        checks.append({"id": "not_certified", "label": "Not already SOC 2 / ISO 27001 certified", "status": "conflict",
                       "detail": "No certification is stated, but the site's code/assets mention one - verify by hand before sending.",
                       "support": hints})
    else:
        checks.append({"id": "not_certified", "label": "Not already SOC 2 / ISO 27001 certified", "status": "pass",
                       "detail": "No certification stated in text, structured data, JS bundles, CSS badges or trust pages.",
                       "support": facts.get("certifications_in_progress") or []})

    # 3. Right stage
    fits, stage_why = stage_fits(facts)
    stage_support = [x for x in (
        f"job board ({sig['jobs']['board']}): {sig['jobs']['count']} open roles" if sig.get("jobs") else "",
        f"funding: {facts.get('funding_stage')}" if facts.get("funding_stage") else "",
        f"headcount: {facts.get('headcount_hint')}" if facts.get("headcount_hint") else "",
        ("enterprise sales tools: " + ", ".join(vendors["enterprise"][:4])) if vendors.get("enterprise") else "",
    ) if x]
    checks.append({"id": "stage", "label": "Series A-C sized", "status": "pass" if fits else "unknown",
                   "detail": stage_why, "support": stage_support})

    # 4. Compliance trigger ("why now")
    trig = []
    if facts.get("security_or_compliance_roles_open"):
        trig.append("hiring: " + ", ".join(facts["security_or_compliance_roles_open"][:3]))
    if facts.get("certifications_in_progress") or hits.get("in_progress"):
        trig.append("certification in progress")
    if hits.get("security_questionnaire"):
        trig.append("mentions security reviews / vendor assessments")
    if vendors.get("enterprise"):
        trig.append("sells to enterprises (" + ", ".join(vendors["enterprise"][:3]) + ")")
    if hits.get("sso_saml"):
        trig.append("offers SSO/SAML - enterprise buyers")
    if facts.get("regulated_industries_served"):
        trig.append("regulated buyers: " + ", ".join(facts["regulated_industries_served"][:3]))
    if facts.get("named_enterprise_customers"):
        trig.append("enterprise logos: " + ", ".join(facts["named_enterprise_customers"][:3]))
    checks.append({"id": "trigger", "label": "Compliance trigger (why now)", "status": "pass" if trig else "unknown",
                   "detail": "; ".join(trig[:2]) if trig else "no explicit trigger found", "support": trig[2:]})

    # 5. Scrape quality - did we actually see enough?
    words = sum(p.get("words", 0) for p in pages if p.get("status") == 200)
    words += sum(len(t.split()) for t in sig.get("app_state_text", []))
    blocked = [p["requested"] for p in pages if p.get("status") == "blocked_by_robots"]
    thin = [p["requested"] for p in pages if p.get("note") == "thin_or_js_rendered"]
    sc = sig.get("scanned", {})
    layers = f"HTML{' + ' + str(sc.get('js_files', 0)) + ' JS' if sc.get('js_files') else ''}" \
             f"{' + ' + str(sc.get('css_files', 0)) + ' CSS' if sc.get('css_files') else ''}" \
             f"{' + sitemap' if sc.get('sitemap') else ''}{' + job board' if sc.get('job_board') else ''}"
    if words < config.MIN_GOOD_WORDS // 2 and not sig.get("structured_data"):
        status, detail = "fail", (f"Only {words} readable words - site is JavaScript-rendered or blocking bots. "
                                  "Needs a real-browser scraper.")
    elif words < config.MIN_GOOD_WORDS or (thin and len(thin) == len([p for p in pages if p.get('status') == 200])):
        status, detail = "conflict", f"Thin data ({words} words). Decisions are less certain; a browser scraper would see more."
    else:
        status, detail = "pass", f"{words} words across {len([p for p in pages if p.get('status') == 200])} pages"
    support = [f"layers screened: {layers}"] + ([f"blocked by robots.txt: {', '.join(blocked)}"] if blocked else []) \
        + ([f"thin / JS-rendered: {', '.join(thin)}"] if thin else [])
    checks.append({"id": "scrape", "label": "Enough data collected", "status": status, "detail": detail,
                   "support": support})

    log.append("VERIFY checklist: " + " | ".join(f"{c['id']}={c['status']}" for c in checks))
    return facts, checks


# ---------- the judgment -----------------------------------------------------

JUDGE_SCHEMA = obj({
    "fit_score": I,               # 1-5
    "why_now": S,                 # the trigger: audit prep, enterprise deals, regulated buyers...
    "evidence_used": LS,          # the exact facts the score rests on
    "risks": LS,                  # reasons this could be a bad lead
})

JUDGE_SYSTEM = f"""You qualify prospects for Sprinto.
{config.SPRINTO_FACTS}
Sprinto's ICP: B2B SaaS, roughly Seed to Series C, that is EITHER preparing for a compliance
audit OR under pressure from enterprise / regulated customers to get certified.
You get the extracted facts AND a verification checklist built from every source
(page text, JSON-LD, JS bundles, CSS, sitemap, public job board, third-party tools).
Score fit_score 1-5:
 5 = explicit trigger (hiring security/compliance, "SOC 2 in progress", enterprise tier + big logos, regulated buyers)
 4 = strong indirect trigger (selling upmarket, SSO/enterprise sales tools, sensitive data, enterprise customers named)
 3 = plausible fit but trigger is inferred, not stated
 2 = weak: tiny, consumer-leaning, or no sign anyone is asking them for compliance
 1 = not a fit
Use only the facts given. Cite them in evidence_used. Be strict: a generic SaaS with no trigger is a 2.
If the checklist shows a conflict, name it in risks."""

MIN_FIT_SCORE = 3


def _signals_for_ui(sig: Dict) -> Dict:
    return {
        "vendors": sig.get("vendors", {}),
        "jobs": sig.get("jobs"),
        "structured_data_blocks": len(sig.get("structured_data", [])),
        "app_data_blocks": len(sig.get("app_state_text", [])),
        "sitemap_pages_total": sig.get("sitemap_total", 0),
        "badge_images": sig.get("badge_images", []),
        "trust_center_url": sig.get("trust_center_url"),
        "scanned": sig.get("scanned", {}),
        "site_builder": sig.get("site_builder"),
    }


def qualify(domain: str) -> Dict:
    log: List[str] = []
    facts, pages, sig, tried = research(domain, log)
    result = {"domain": domain, "facts": facts, "research_log": log, "flags": [], "checklist": [],
              "signals": _signals_for_ui(sig) if sig else {}}

    def finish():
        result["pages"] = [{k: p.get(k) for k in ("requested", "url", "status", "words", "note")} for p in pages]
        return result

    if not facts:
        result.update(status="manual_review", reason="Could not read the website (blocked, down or empty).",
                      checklist=[{"id": "scrape", "label": "Enough data collected", "status": "fail",
                                  "detail": "Homepage did not load for a plain request - needs a real-browser scraper.",
                                  "support": [f"{p['requested']}: {p['status']}" for p in pages]}])
        return finish()

    g, why = b2b_gate(facts, sig)
    if g == "fail":                                   # CHECKPOINT 1 - flagged immediately
        result.update(status="disqualified", reason=why, not_b2b=True,
                      checklist=[{"id": "b2b_saas", "label": "Is a B2B SaaS company", "status": "fail",
                                  "detail": why, "support": []}])
        result["flags"].append("NOT B2B SaaS")
        return finish()

    facts, checks = verify(domain, facts, pages, sig, tried, log)
    result["facts"], result["checklist"] = facts, checks
    by = {c["id"]: c for c in checks}

    for cid in ("b2b_saas", "not_certified"):
        if by[cid]["status"] == "fail":
            result.update(status="disqualified", reason=by[cid]["detail"])
            if cid == "b2b_saas":
                result["not_b2b"] = True
                result["flags"].append("NOT B2B SaaS")
            return finish()
    stage = (facts.get("funding_stage") or "").lower()
    if any(s in stage for s in config.TOO_LATE_STAGES):
        result.update(status="disqualified",
                      reason=f"Stage '{facts['funding_stage']}' is past Series C - usually has an in-house GRC team.")
        return finish()
    if by["b2b_saas"]["status"] == "unknown":
        result.update(status="manual_review", reason="Could not confirm B2B SaaS after the allowed fetches.")
        return finish()
    if by["scrape"]["status"] == "fail":
        result.update(status="manual_review", reason="Scrape incomplete: " + by["scrape"]["detail"])
        result["flags"].append("SCRAPE INCOMPLETE - needs browser scraping")
        return finish()

    if by["not_certified"]["status"] == "conflict":
        result["flags"].append("possible certification found in site code - verify before sending")
    elif not facts.get("certifications_held"):
        result["flags"].append("certification status from site only - treated as NOT certified")
    if by["scrape"]["status"] == "conflict":
        result["flags"].append("thin data - lower confidence")
    fits, stage_why = stage_fits(facts)
    if not fits:
        result["flags"].append(f"stage unconfirmed ({stage_why}) - qualified at lower rank")

    checklist_txt = "\n".join(f"- {c['label']}: {c['status'].upper()} - {c['detail']}"
                              + (f" [{'; '.join(c['support'])}]" if c["support"] else "") for c in checks)
    judgment = call_json(JUDGE_SYSTEM, "FACTS:\n" + _facts_text(facts) + "\n\nVERIFICATION CHECKLIST:\n" + checklist_txt,
                         JUDGE_SCHEMA, effort="medium")
    result["judgment"] = judgment
    result["stage_evidence"] = stage_why

    if judgment["fit_score"] < MIN_FIT_SCORE:
        result.update(status="disqualified",
                      reason=f"Fit score {judgment['fit_score']}/5 - no compliance trigger strong enough. {judgment['why_now']}")
    else:
        result.update(status="qualified_flagged" if result["flags"] else "qualified",
                      reason=f"Fit {judgment['fit_score']}/5. Why now: {judgment['why_now']}")
    penalty = (0 if fits else 1) + (1 if by["not_certified"]["status"] == "conflict" else 0)
    result["rank_score"] = judgment["fit_score"] - penalty
    return finish()


def _facts_text(f: Dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in f.items() if v not in (None, [], ""))
