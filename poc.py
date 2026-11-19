"""Step 4 - Find the right point of contact.

Source order (L4: "data provider before scraping"):
  1. contacts.csv  - Option B, contacts you looked up by hand in Apollo (always wins: a human chose them)
  2. Hunter.io     - Option A, Domain Search API (free tier: 25 searches/month, 10 emails each)
The ranking is CODE, not the model, so "why this person" has a one-line, checkable answer:
the most senior person who owns security/compliance; if the company has none, the CTO;
if neither, a founder/CEO. Low-confidence Hunter emails are dropped.

Failure table (L6): Hunter timeout = transient -> SDK-free retry once, then skip to CSV;
no contact found = permanent, a human can supply one -> hand to the Growth Lead.
"""
import csv
import json
import os
from pathlib import Path
import re
import time
from typing import Dict, List, Optional

import requests

import config


def _from_csv(domain: str) -> List[Dict]:
    if not os.path.exists(config.CONTACTS_CSV):
        return []
    out = []
    with open(config.CONTACTS_CSV, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("domain", "").strip().lower() == domain.lower():
                out.append({
                    "first_name": row.get("first_name", "").strip(),
                    "last_name": row.get("last_name", "").strip(),
                    "position": row.get("title", "").strip(),
                    "email": row.get("email", "").strip(),
                    "confidence": 100,
                    "source": "apollo_manual_csv",
                    "linkedin": row.get("linkedin", "").strip(),
                })
    return out


def _from_hunter(domain: str, log: List[str]) -> List[Dict]:
    if not config.HUNTER_API_KEY:
        log.append("hunter: no HUNTER_API_KEY set - skipped")
        return []
    cache = Path(config.CACHE_ROOT) / "hunter" / f"{domain}.json"
    if cache.exists():                       # free tier = 25 searches/month: never pay twice
        people = json.loads(cache.read_text())
        log.append(f"hunter: {len(people)} people (cached, no search used)")
        return people
    people = _hunter_search(domain, log)
    if people:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(people))
    return people


def _hunter_search(domain: str, log: List[str]) -> List[Dict]:
    params = {"domain": domain, "api_key": config.HUNTER_API_KEY, "limit": 10, "type": "personal"}
    for attempt in (1, 2):
        try:
            r = requests.get("https://api.hunter.io/v2/domain-search", params=params, timeout=20)
        except requests.RequestException as e:
            log.append(f"hunter attempt {attempt}: {type(e).__name__}")
            time.sleep(2)
            continue
        if r.status_code == 429:
            log.append("hunter: rate limited / monthly quota used up")
            return []
        if r.status_code != 200:
            log.append(f"hunter: HTTP {r.status_code} {r.text[:120]}")
            return []
        emails = r.json().get("data", {}).get("emails", [])
        log.append(f"hunter: {len(emails)} people returned")
        return [{
            "first_name": e.get("first_name") or "",
            "last_name": e.get("last_name") or "",
            "position": e.get("position") or "",
            "email": e.get("value") or "",
            "confidence": e.get("confidence") or 0,
            "seniority": e.get("seniority"),
            "department": e.get("department"),
            "source": "hunter",
            "linkedin": e.get("linkedin") or "",
        } for e in emails]
    return []


def _role_bucket(title: str) -> Optional[int]:
    t = title.lower()
    for i, (_, keys) in enumerate(config.POC_TITLE_PRIORITY):
        # word boundaries: "cto" must not match "direCTOr"
        if any(re.search(rf"\b{re.escape(k)}\b", t) for k in keys):
            return i
    return None


def find_poc(domain: str, log: List[str]) -> Dict:
    people = _from_csv(domain)
    if people:
        log.append(f"contacts.csv: {len(people)} hand-picked contact(s) for {domain}")
    else:
        people = _from_hunter(domain, log)

    ranked = []
    for p in people:
        if not p["first_name"] or not p["email"]:
            continue
        if p["confidence"] < config.MIN_HUNTER_CONFIDENCE:
            log.append(f"dropped {p['first_name']} ({p['position']}): email confidence {p['confidence']}")
            continue
        bucket = _role_bucket(p["position"])
        if bucket is None:
            continue
        senior = 0 if any(w in p["position"].lower() for w in ("chief", "head", "vp", "founder", "director")) else 1
        ranked.append((bucket, senior, -p["confidence"], p))

    if not ranked:
        return {"found": False, "why": "No contact with a security, engineering-leadership or founder title "
                                       "and a confident email. Hand to the Growth Lead to source one in Apollo."}

    ranked.sort(key=lambda x: x[:3])
    bucket, _, _, best = ranked[0]
    role_name = config.POC_TITLE_PRIORITY[bucket][0]
    reasons = {
        "security_compliance_owner": "owns security/compliance, so SOC 2 / ISO 27001 lands on their desk",
        "cto": "no dedicated security/compliance owner found; at Series A-C the CTO usually owns the audit",
        "founder_ceo": "no security or engineering leader found; at this size the founder signs off on certifications",
        "coo_ops": "operations leader; owns vendor questionnaires and audits when there is no security hire",
    }
    best.update(found=True, role_bucket=role_name,
                why=f"{best['first_name']} {best['last_name']} ({best['position']}): {reasons[role_name]}. "
                    f"Source: {best['source']}, email confidence {best['confidence']}.",
                alternatives=[f"{p['first_name']} {p['last_name']} - {p['position']}" for *_, p in ranked[1:4]])
    return best
